"""Two-stage matching: retrieve candidates, then apply business rules.

    Stage 1  hard filters + relevance retrieval  ->  a broad candidate pool
    Stage 2  business compatibility + reranking  ->  the top K shown

Stage 1 asks Qdrant first (cosine similarity over the product embedding) and
PostgreSQL second. PostgreSQL is not just the outage fallback: Qdrant can only
return rows it has vectors for, so listings that were never indexed (created
while Ollama was down) or whose vector is stale are always pulled in by SQL, and
when the vector answer is thin (fewer than ``MIN_VECTOR_ROWS`` live rows) the
SQL pool is merged in as well. The vector call has a short deadline
(``MATCH_VECTOR_TIMEOUT_SECONDS``) so a slow embedding model degrades matching
to SQL instead of stalling it.

The hard filters are not negotiable, and the first of them is the marketplace's
central rule -- a buyer is only ever shown sellers, and a seller only buyers.
"""

import asyncio
import logging
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.connection import Connection
from models.enums import ConnectionStatus, EmbeddingStatus, RFQRole, RFQStatus
from models.match import MatchResult, MatchSearch
from models.rfq import RFQ
from models.user import User
from schemas.common import DeadlineOut, Location, Money, Quantity
from schemas.match import Counterparty, MatchCandidate, MatchResponse, MatchScore
from core.config import settings
from services import categories, match_scoring, product_synonyms, qdrant_index
from services.embeddings import embed_one
from services.query_extractor import is_broad_product
from services.rfq_indexing import build_match_text

logger = logging.getLogger(__name__)

# How many rows one SQL batch hands to stage 2. Business rules reorder heavily,
# so the pool has to be considerably wider than the page being returned.
CANDIDATE_POOL = 200

# SQL batches continue while they keep producing matches, up to this many rows.
# ``total`` is exact below it and a lower bound above it.
MAX_POOL = 1000

# Fewer live rows than this from the vector index and the SQL pool is merged in.
MIN_VECTOR_ROWS = 10

# Embedding + vector search must answer within this, or SQL takes over.
# Overridable with a MATCH_VECTOR_TIMEOUT_SECONDS setting.
VECTOR_TIMEOUT_SECONDS = 1.0

# Below this, a candidate is noise rather than a weak match.
MIN_SCORE = 0.05

# Price and quantity only mean something between comparable products. Below this
# relevance, "cheaper" is comparing a box price against a cable budget, and a
# stock level against an unrelated requirement -- so both are left unscored
# rather than being awarded full marks for a meaningless comparison.
COMPARABLE_FLOOR = 0.35

# Category as a multiplier on product fit. A red chilli LED light is not a red
# chilli, however many words the two share.
CATEGORY_MISMATCH_FACTOR = 0.3
# Near-identical products in different categories are usually one listing filed
# in the wrong place; they keep most of their score.
CATEGORY_MISMATCH_HIGH_RELEVANCE = 0.92
CATEGORY_MISMATCH_FACTOR_HIGH_RELEVANCE = 0.85
# One side used a free-text category we cannot map: a mild, not a hard, penalty.
CUSTOM_CATEGORY_MISMATCH_FACTOR = 0.8
CATEGORY_MATCH_BOOST = 1.05

_UNKNOWN_CATEGORY = "General Goods"

# Commercial terms and product fit, by dimension.
_PRODUCT_DIMS = ("relevance", "attributes", "category")
_COMMERCIAL_DIMS = ("price", "quantity", "location", "deadline")

MATCHING_PRESETS: dict[str, dict[str, float]] = {
    # Each preset covers every dimension and sums to 1. What moves is the split
    # between product fit (relevance + attributes + category) and commercial
    # terms, and inside product fit the share that the named specs carry.
    "quality_first": {
        "relevance": 0.30, "attributes": 0.30, "category": 0.10,
        "price": 0.12, "quantity": 0.08, "location": 0.06, "deadline": 0.04,
    },
    "price_first": {
        "relevance": 0.18, "attributes": 0.10, "category": 0.07,
        "price": 0.35, "quantity": 0.16, "location": 0.09, "deadline": 0.05,
    },
    "fast_delivery": {
        "relevance": 0.18, "attributes": 0.10, "category": 0.07,
        "location": 0.30, "deadline": 0.20, "price": 0.09, "quantity": 0.06,
    },
    "balanced": match_scoring.WEIGHTS,
}

# The gate constants below are calibrated so the default weights reproduce the
# original formula: total = F * (0.35 + 0.65 * C), F = rel * (0.35 + 0.65 * attr).
_DEFAULT_COMMERCIAL_SHARE = sum(match_scoring.WEIGHTS[d] for d in _COMMERCIAL_DIMS)
_DEFAULT_ATTRIBUTE_SHARE = match_scoring.WEIGHTS["attributes"] / (
    match_scoring.WEIGHTS["attributes"] + match_scoring.WEIGHTS["relevance"]
)
_BASE_GATE = 0.65


def _vector_timeout() -> float:
    value = getattr(settings, "MATCH_VECTOR_TIMEOUT_SECONDS", None)
    try:
        return float(value) if value is not None else VECTOR_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return VECTOR_TIMEOUT_SECONDS


def _active_filters(rfq: RFQ, requester_id: uuid.UUID, now: datetime) -> list[Any]:
    """The non-negotiable filters, shared by every retrieval path."""
    return [
        # The central rule: match against the opposite side of the market.
        RFQ.role == rfq.role.counterpart,
        RFQ.status == RFQStatus.ACTIVE,
        RFQ.user_id != requester_id,
        or_(RFQ.expires_at.is_(None), RFQ.expires_at > now),
    ]


async def _vector_query(
    rfq: RFQ, requester_id: uuid.UUID
) -> Optional[dict[uuid.UUID, float]]:
    # Must be the same projection the index was built from, or the query and
    # the corpus sit in different parts of the space.
    try:
        vector = await asyncio.wait_for(embed_one(build_match_text(rfq)), timeout=_vector_timeout())
    except (asyncio.TimeoutError, TimeoutError, Exception):
        return None
    if vector is None:
        return None

    async def query(threshold: float):
        return await qdrant_index.search(
            vector,
            target_role=rfq.role.counterpart.value,
            exclude_user_id=requester_id,
            limit=CANDIDATE_POOL,
            # Cut at the floor inside the search. Anything below it is a
            # different product, and padding the table with those is what made
            # every query look like it returned the same rows.
            score_threshold=threshold,
        )

    hits = await query(settings.RELEVANCE_FLOOR)
    if hits is None:
        return None
    if not hits:
        # Nothing cleared the bar. Rather than a dead end, drop to the fallback
        # tier and show the closest things there are.
        hits = await query(settings.RELEVANCE_FALLBACK_FLOOR)
        if hits is None:
            return None
    return {rfq_id: score for rfq_id, score in hits}


async def _vector_candidates(
    rfq: RFQ, requester_id: uuid.UUID
) -> Optional[dict[uuid.UUID, float]]:
    """Ids and cosine scores from Qdrant, or None if it cannot answer in time."""
    try:
        return await asyncio.wait_for(
            _vector_query(rfq, requester_id), timeout=_vector_timeout()
        )
    except TimeoutError:
        logger.warning("vector retrieval exceeded %.1fs; using SQL", _vector_timeout())
        return None


async def _load_active(
    db: AsyncSession,
    ids: list[uuid.UUID],
    rfq: RFQ,
    requester_id: uuid.UUID,
) -> Sequence[RFQ]:
    """Read the rows back, re-applying the hard filters.

    The index can lag the database, so its answer is treated as a suggestion.
    Status and expiry are confirmed against the source of truth before anything
    is shown, and only rows whose vector is current (``indexed``) are trusted:
    a pending or stale row's similarity describes an older version of it, so
    such rows are scored lexically via the SQL path instead.
    """
    if not ids:
        return []
    now = datetime.now(UTC)
    rows = (
        await db.scalars(
            select(RFQ)
            .options(selectinload(RFQ.user).selectinload(User.profile))
            .where(
                RFQ.id.in_(ids),
                RFQ.embedding_status == EmbeddingStatus.INDEXED,
                *_active_filters(rfq, requester_id, now),
            )
        )
    ).all()
    return rows


# --- SQL retrieval ------------------------------------------------------------

# Number + quantity unit glued together ("500kg", "20t", "1000pcs"): an order
# size, never a product specification.
_QUANTITY_TOKEN_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:kg|kgs|g|gm|gms|gram|grams|t|mt|ton|tons|tonne|tonnes|qtl|"
    r"quintal|quintals|pc|pcs|piece|pieces|no|nos|unit|units|dozen|doz|dz|l|ltr|"
    r"litre|litres|liter|liters|ml|lb|lbs|k|lakh|lakhs|lac|cr|crore|crores|bag|bags|"
    r"box|boxes|carton|cartons|bora|boras|bori|pallet|pallets|sack|sacks|roll|rolls|set|sets)$"
)

# Distinguishing words that are not alphanumeric codes.
_SPEC_WORDS = frozenset(
    {"c", "organic", "seamless", "combed", "lightning", "fuji", "alphonso", "316l"}
)

# product_details keys that describe the order or its packing, not the product.
_NON_SPEC_KEYS = frozenset(
    {
        "name", "product", "product_name", "title", "packaging", "pack_count",
        "pack_weight", "pack_weight_kg", "total_weight_kg", "total_weight_tons",
        "quantity", "moq", "min_order", "notes",
    }
)

_MAX_SQL_TERMS = 16


def _product_name(rfq: RFQ) -> str:
    details = rfq.product_details or {}
    for key in ("name", "product", "product_name", "title"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _is_spec_token(token: str) -> bool:
    if token in _SPEC_WORDS:
        return True
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    return has_digit and has_alpha and not _QUANTITY_TOKEN_RE.match(token)


def critical_spec_tokens(product_details: Optional[dict[str, Any]]) -> set[str]:
    """Distinguishing tokens the requester put in the structured product data.

    Read from ``product_details`` (the product name and string or boolean
    attributes), never from the title: "Need 500kg basmati rice" carries an
    order size in its title, not a sub-variant. Codes such as "316l" or "cat6"
    and marker words such as "organic" count; quantities such as "500kg" do not.
    Measurements stored as ``{"value", "unit"}`` are compared numerically by
    ``attribute_score`` and are not duplicated here.
    """
    details = product_details or {}
    tokens: set[str] = set()
    for key, value in details.items():
        if not isinstance(key, str) or key.endswith("__must_match"):
            continue
        if key in ("name", "product", "product_name"):
            text = value if isinstance(value, str) else ""
        elif key in _NON_SPEC_KEYS:
            continue
        elif value is True:
            text = key.replace("_", " ")
        elif isinstance(value, str):
            text = value
        elif isinstance(value, (list, tuple)):
            text = " ".join(str(v) for v in value if isinstance(v, (str, int, float)))
        else:
            continue
        tokens.update(t for t in match_scoring.tokenize(text) if _is_spec_token(t))
    return tokens


def _search_terms(rfq: RFQ) -> dict[str, int]:
    """Words to rank the SQL pool by, with their weight.

    Product-name words (and their local-language variants) weigh 2, other
    title and spec words 1. Quantities and bare numbers are dropped.
    """
    def usable(token: str) -> bool:
        return not token.isdigit() and not _QUANTITY_TOKEN_RE.match(token)

    name_tokens = {t for t in match_scoring.tokenize(_product_name(rfq)) if usable(t)}
    other = {t for t in match_scoring.tokenize(rfq.title) if usable(t)}
    other |= critical_spec_tokens(rfq.product_details)

    terms: dict[str, int] = {}
    for term in product_synonyms.expand_terms(name_tokens):
        terms[term] = 2
    for term in product_synonyms.expand_terms(other):
        terms.setdefault(term, 1)
    ordered = sorted(terms.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    return dict(ordered[:_MAX_SQL_TERMS])


def _term_pattern(term: str) -> str:
    """A POSIX word-start regex for one term: 'cable' also finds 'cables'."""
    stem = term[:-1] if term.endswith("y") and len(term) > 3 else term  # battery/batteries
    stem = re.escape(stem)
    if len(term) <= 2:
        return rf"\m{stem}\M"
    return rf"\m{stem}"


def _category_exclusions(canonical: str) -> list[str]:
    """Lowercased category spellings that normalise to a *different* canonical one."""
    known = set(categories.CANONICAL_CATEGORIES)
    names = {c.lower() for c in known if c != canonical}
    names |= {
        alias.lower()
        for alias, target in categories.CATEGORY_ALIASES.items()
        if target != canonical and target in known
    }
    return sorted(names)


def _sql_query(rfq: RFQ, requester_id: uuid.UUID, *, unindexed_only: bool = False):
    now = datetime.now(UTC)
    query = (
        select(RFQ)
        .options(selectinload(RFQ.user).selectinload(User.profile))
        .where(*_active_filters(rfq, requester_id, now))
    )
    if unindexed_only:
        query = query.where(RFQ.embedding_status != EmbeddingStatus.INDEXED)

    # Category as a filter, by canonical name: rows filed under a *different*
    # known category are excluded, while free-text categories we cannot map
    # stay in and are scored with a milder penalty in stage 2.
    canonical = categories.normalize_category(rfq.category)
    if canonical in categories.CANONICAL_CATEGORIES:
        query = query.where(
            func.lower(func.trim(RFQ.category)).not_in(_category_exclusions(canonical))
        )

    # Rank by how many of the requester's product words each row contains.
    # Newest-first inside a tag used to push an exact but older seller out of
    # a 200-row pool; relevance ordering keeps it at the top however many
    # newer near-misses exist.
    terms = _search_terms(rfq)
    if terms:
        text_rank = sum(
            (
                case(
                    (
                        or_(
                            RFQ.title.op("~*")(_term_pattern(term)),
                            func.coalesce(RFQ.search_text, "").op("~*")(_term_pattern(term)),
                        ),
                        weight,
                    ),
                    else_=0,
                )
                for term, weight in terms.items()
            ),
            literal(0),
        )
    else:
        text_rank = literal(0)

    order = [text_rank.desc()]
    if rfq.search_tags:
        order.append(RFQ.search_tags.overlap(rfq.search_tags).desc())
    order += [RFQ.created_at.desc(), RFQ.id]
    return query.order_by(*order)


async def _sql_candidates(
    db: AsyncSession,
    rfq: RFQ,
    requester_id: uuid.UUID,
    *,
    limit: int = CANDIDATE_POOL,
    offset: int = 0,
    unindexed_only: bool = False,
) -> Sequence[RFQ]:
    """One relevance-ordered page of SQL candidates."""
    query = _sql_query(rfq, requester_id, unindexed_only=unindexed_only)
    return (await db.scalars(query.limit(limit).offset(offset))).all()


# --- stage 2 ------------------------------------------------------------------


def _resolve_weights(matching_preferences: Optional[dict[str, Any]]) -> dict[str, float]:
    """Profile preferences -> a clean weight vector (Pillar 4).

    A named preset wins; otherwise custom weights are sanitised (unknown keys
    ignored, negatives clamped to 0, renormalised, defaults when unusable).
    """
    if not isinstance(matching_preferences, dict):
        return match_scoring.WEIGHTS
    preset = matching_preferences.get("preset")
    if isinstance(preset, str) and preset in MATCHING_PRESETS:
        return MATCHING_PRESETS[preset]
    custom = matching_preferences.get("weights")
    if isinstance(custom, dict):
        return match_scoring.sanitise_weights(custom)
    return match_scoring.WEIGHTS


def _gates(weights: dict[str, float]) -> tuple[float, float]:
    """(attribute gate, commercial gate) implied by a weight vector.

    The attribute gate is how much of product fit the named specs decide; the
    commercial gate is how much of the total price/quantity/location/deadline
    decide. Both scale with the weights relative to the defaults, so a
    ``quality_first`` profile lets specs dominate and a ``price_first`` one
    lets commercial terms move the ranking further.
    """
    def clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    total = sum(max(0.0, weights.get(d, 0.0)) for d in match_scoring.WEIGHTS) or 1.0
    commercial_share = sum(max(0.0, weights.get(d, 0.0)) for d in _COMMERCIAL_DIMS) / total
    rel = max(0.0, weights.get("relevance", 0.0))
    attr = max(0.0, weights.get("attributes", 0.0))
    attribute_share = attr / (rel + attr) if (rel + attr) > 0 else _DEFAULT_ATTRIBUTE_SHARE

    attribute_gate = clamp(_BASE_GATE * attribute_share / _DEFAULT_ATTRIBUTE_SHARE, 0.2, 0.9)
    commercial_gate = clamp(_BASE_GATE * commercial_share / _DEFAULT_COMMERCIAL_SHARE, 0.15, 0.9)
    return attribute_gate, commercial_gate


def _category_factor(
    requester_category: Optional[str], candidate_category: Optional[str], relevance: float
) -> tuple[float, Optional[float]]:
    """(multiplier on product fit, category score for the breakdown)."""
    left = categories.normalize_category(requester_category)
    right = categories.normalize_category(candidate_category)
    if left == _UNKNOWN_CATEGORY or right == _UNKNOWN_CATEGORY:
        return 1.0, None
    if left == right:
        return CATEGORY_MATCH_BOOST, 1.0
    known = categories.CANONICAL_CATEGORIES
    if left in known and right in known:
        if relevance >= CATEGORY_MISMATCH_HIGH_RELEVANCE:
            return CATEGORY_MISMATCH_FACTOR_HIGH_RELEVANCE, 0.0
        return CATEGORY_MISMATCH_FACTOR, 0.0
    # A free-text category on one side: fall back to token overlap.
    overlap = match_scoring.category_score(requester_category or "", candidate_category or "")
    if overlap >= 0.999:
        return CATEGORY_MATCH_BOOST, 1.0
    return CUSTOM_CATEGORY_MISMATCH_FACTOR, round(overlap, 4)


def _finite(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(1.0, number))


def _score(
    rfq: RFQ,
    candidate: RFQ,
    similarity: Optional[float] = None,
    matching_preferences: Optional[dict[str, Any]] = None,
) -> tuple[MatchScore, bool]:
    """Stage 2. Apply the compatibility rules to one candidate.

    Incorporates the 4 Pillars of Universal Dynamic B2B Matching:
    - Pillar 1: Key-Agnostic & Inverted Attribute Semantic Alignment
    - Pillar 2: Dynamic Asymmetric Specificity Check (Specifiers like 'Type-C' vs generic)
    - Pillar 3: Two-Tier Multiplicative Gating (Feasibility x Commercial Utility)
    - Pillar 4: User-Centric Dynamic Priority Vectors (Profile Matching Preferences)
    """
    # Price, quantity and deadline are directional: whoever is buying supplies
    # the target, the requirement and the need-by date; whoever is selling
    # supplies the ask, the stock and the ready-by date.
    if rfq.role is RFQRole.BUYER:
        buyer, seller = rfq, candidate
    else:
        buyer, seller = candidate, rfq

    active_weights = _resolve_weights(matching_preferences)
    attribute_gate, commercial_gate = _gates(active_weights)

    if similarity is not None:
        relevance = _finite(similarity) or 0.0
        floor = 0.0
    else:
        relevance = match_scoring.relevance_score(
            rfq.search_tags, build_match_text(rfq),
            candidate.search_tags, build_match_text(candidate),
        )
        floor = COMPARABLE_FLOOR

    # Dynamic Asymmetric Specificity Check (Pillar 2): when the requester's
    # structured product data names a distinguishing sub-type ('type-c',
    # '316l', 'organic', 'fuji', 'cat6') and the candidate never mentions it,
    # a generic product must not masquerade as the specific one.
    required_specs = critical_spec_tokens(rfq.product_details)
    if required_specs:
        cand_tokens_all = match_scoring.tokenize(
            f"{candidate.title or ''} {candidate.search_text or ''}"
        )
        if required_specs - cand_tokens_all:
            relevance = max(0.10, relevance * 0.40)

    comparable = relevance >= floor

    attr_score = match_scoring.attribute_score(
        rfq.product_details or {}, candidate.product_details or {}
    )
    category_factor, category = _category_factor(rfq.category, candidate.category, relevance)

    scores: dict[str, Optional[float]] = {
        "relevance": relevance,
        "category": category,
        "attributes": attr_score,
        "price": match_scoring.price_score(
            buyer.price_amount, seller.price_amount,
            buyer.price_currency, seller.price_currency,
            buyer.price_per_unit or buyer.quantity_unit,
            seller.price_per_unit or seller.quantity_unit,
        ) if comparable else None,
        "quantity": match_scoring.quantity_score(
            buyer.quantity_value, buyer.quantity_unit,
            seller.quantity_value, seller.quantity_unit,
            needed_attributes=buyer.product_details or {},
            available_attributes=seller.product_details or {},
        ) if comparable else None,
        "location": match_scoring.location_score(
            {
                "city": rfq.location_city, "state": rfq.location_state,
                "country": rfq.location_country,
                "latitude": rfq.latitude, "longitude": rfq.longitude,
            },
            {
                "city": candidate.location_city, "state": candidate.location_state,
                "country": candidate.location_country,
                "latitude": candidate.latitude, "longitude": candidate.longitude,
            },
        ),
        # Buyer's need-by first, seller's ready-by second, whoever searched.
        "deadline": match_scoring.deadline_score(buyer.deadline_at, seller.deadline_at),
    }
    scores = {k: _finite(v) for k, v in scores.items()}

    keep = comparable

    # Two-Tier Multiplicative Gating (Pillar 3):
    # Tier 1: Feasibility (F) = does the product, its specs and its category match?
    # Tier 2: Commercial (C) = price, quantity, location, deadline.
    # Commercial terms (cheap price, close location) CANNOT compensate for an
    # incorrect product or missing spec.
    if attr_score is not None:
        feasibility = relevance * ((1 - attribute_gate) + attribute_gate * attr_score)
    else:
        feasibility = relevance
    feasibility = min(1.0, feasibility * category_factor)

    commercial_scores = {
        k: v for k, v in scores.items() if k in _COMMERCIAL_DIMS and v is not None
    }
    if commercial_scores:
        commercial_utility = match_scoring.blend(commercial_scores, weights=active_weights)
        base_total = feasibility * ((1 - commercial_gate) + commercial_gate * commercial_utility)
    else:
        base_total = feasibility

    # Core Product Gatekeeping:
    if relevance < settings.RELEVANCE_FLOOR:
        base_total = min(base_total, relevance * category_factor)

    # Distinct commodity check: if the requester named a specific product
    # ("tomatoes") and the candidate is a different commodity ("Fuji apples")
    # with no product word in common -- after plurals and local names are
    # normalised, so "tamatar" and "tomato" are one word -- it is not a match
    # unless the vector index is highly confident (similarity >= 0.85).
    req_prod = _product_name(rfq)
    if req_prod and not is_broad_product(req_prod):
        req_tokens = match_scoring.tokenize(req_prod)
        cand_name = _product_name(candidate) or candidate.title or ""
        cand_tokens = match_scoring.tokenize(cand_name)
        is_strong_semantic = similarity is not None and similarity >= 0.85
        if req_tokens and cand_tokens and not (req_tokens & cand_tokens) and not is_strong_semantic:
            keep = False

    # Review & rating reputation adjustment for future match ranking
    profile = candidate.user.profile if candidate.user else None
    if profile and profile.average_rating is not None and profile.total_reviews > 0:
        avg = float(profile.average_rating)
        base_total += (avg - 3.5) * 0.04

    # Whatever the weights, the total is a probability-like score in [0, 1].
    total = round(_finite(base_total) or 0.0, 4)
    return MatchScore(total=total, **scores), keep


async def _connections_for(
    db: AsyncSession, viewer_id: uuid.UUID, rfq_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Connection]:
    """This viewer's connection request, if any, against each listing.

    One query for the whole page rather than one per card, and scoped to the
    viewer as sender: someone else's request to the same listing must not unlock
    anything here.
    """
    if not rfq_ids:
        return {}
    rows = (
        await db.scalars(
            select(Connection).where(
                Connection.rfq_id.in_(rfq_ids), Connection.sender_id == viewer_id
            )
        )
    ).all()
    return {row.rfq_id: row for row in rows}


def _counterparty(candidate: RFQ, connection: Optional[Connection]) -> Counterparty:
    """The visible slice of the other side.

    Contact details are attached only for an accepted connection. Everything
    else on this object is deliberately safe to show to anyone who can see the
    listing at all.
    """
    profile = candidate.user.profile if candidate.user else None
    accepted = connection is not None and connection.status is ConnectionStatus.ACCEPTED

    avg_rating = float(profile.average_rating) if profile and profile.average_rating is not None else None
    tot_reviews = profile.total_reviews if profile else 0
    t_score = profile.trust_score if profile else 20
    is_gst = bool(profile and profile.gst_number and getattr(profile.kyc_status, "value", str(profile.kyc_status)) == "verified")

    return Counterparty(
        company_name=profile.company_name if profile else None,
        city=profile.city if profile else None,
        state=profile.state if profile else None,
        country=profile.country if profile else None,
        member_since=candidate.user.created_at if candidate.user else None,
        connection_id=connection.id if connection else None,
        connection_status=connection.status if connection else None,
        contact_name=(profile.name if profile else None) if accepted else None,
        # Email is contact detail like phone: hidden until they accept, or
        # the card becomes a scrapeable lead list (see schemas/match.py).
        email=(candidate.user.email if candidate.user else None) if accepted else None,
        phone=(profile.phone if profile else None) if accepted else None,
        address=(profile.address if profile else None) if accepted else None,
        average_rating=avg_rating,
        total_reviews=tot_reviews,
        trust_score=t_score,
        gst_verified=is_gst,
    )


def _to_candidate(
    requester: RFQ,
    candidate: RFQ,
    score: MatchScore,
    rank: int,
    connection: Optional[Connection] = None,
) -> MatchCandidate:

    quantity = (
        Quantity(value=candidate.quantity_value, unit=candidate.quantity_unit or "units")
        if candidate.quantity_value is not None
        else None
    )
    price = (
        Money(
            amount=candidate.price_amount,
            currency=candidate.price_currency,
            per_unit=candidate.price_per_unit,
        )
        if candidate.price_amount is not None
        else None
    )
    location = (
        Location(
            city=candidate.location_city,
            state=candidate.location_state,
            country=candidate.location_country,
            latitude=candidate.latitude,
            longitude=candidate.longitude,
            raw=candidate.location_raw,
        )
        if any((candidate.location_city, candidate.location_state, candidate.location_country))
        else None
    )
    raw_distance = match_scoring.distance_km(
        {"latitude": requester.latitude, "longitude": requester.longitude},
        {"latitude": candidate.latitude, "longitude": candidate.longitude},
    )
    distance = round(raw_distance, 1) if raw_distance is not None else None

    is_cross_border = bool(
        requester.location_country
        and candidate.location_country
        and requester.location_country.strip().lower() != candidate.location_country.strip().lower()
    )
    logistics = match_scoring.estimate_logistics(distance, is_cross_border=is_cross_border)

    # A seller's deadline is when the goods can be dispatched, so the arrival
    # estimate adds transit. A buyer's deadline is when they need the goods --
    # adding transit to it would describe nothing.
    est_delivery = None
    if (
        candidate.role is RFQRole.SELLER
        and candidate.deadline_at is not None
        and logistics
    ):
        transit_days = logistics.get("transit_days_max", 0)
        est_delivery = candidate.deadline_at + timedelta(days=transit_days)

    deadline = (
        DeadlineOut(
            date=candidate.deadline_at,
            raw=candidate.deadline_raw,
            excludes_transport=True,
            estimated_delivery_at=est_delivery,
        )
        if candidate.deadline_at is not None
        else None
    )

    return MatchCandidate(
        rfq_id=candidate.id,
        role=candidate.role,
        rank=rank,
        title=candidate.title,
        category=candidate.category,
        description=candidate.description,
        quantity=quantity,
        price=price,
        location=location,
        deadline=deadline,
        product_details=candidate.product_details,
        search_tags=candidate.search_tags,
        distance_km=distance,
        logistics=logistics,
        counterparty=_counterparty(candidate, connection),
        score=score,
    )



async def match_for_rfq(
    db: AsyncSession,
    user: User,
    rfq: RFQ,
    *,
    limit: int = 10,
    offset: int = 0,
) -> MatchResponse:
    """Match a saved RFQ against the other side of the market."""
    return await run_match(
        db, user, rfq, rfq_id=rfq.id, source="rfq", limit=limit, offset=offset
    )


async def _collect(
    db: AsyncSession,
    rfq: RFQ,
    requester_id: uuid.UUID,
    matching_preferences: Optional[dict[str, Any]],
) -> tuple[list[tuple[RFQ, MatchScore]], str]:
    """Stage 1 + stage 2: every candidate that passes the gates, scored.

    Returns the scored list and which retrieval path produced it.
    """
    seen: set[uuid.UUID] = set()
    scored: list[tuple[RFQ, MatchScore]] = []

    def consider(rows: Iterable[RFQ], similarities: dict[uuid.UUID, float]) -> int:
        kept = 0
        for candidate in rows:
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            score, keep = _score(
                rfq, candidate, similarities.get(candidate.id),
                matching_preferences=matching_preferences,
            )
            if keep and score.total >= MIN_SCORE:
                scored.append((candidate, score))
                kept += 1
        return kept

    path = "sql"
    vector_hits = await _vector_candidates(rfq, requester_id)
    if vector_hits is not None:
        rows = await _load_active(db, list(vector_hits), rfq, requester_id)
        consider(rows, vector_hits)
        # Rows without a current vector are invisible to Qdrant; SQL is the
        # only way they can be found.
        consider(
            await _sql_candidates(db, rfq, requester_id, unindexed_only=True), {}
        )
        if len(rows) >= MIN_VECTOR_ROWS:
            return scored, "vector"
        # Empty index, stale ids, or a thin answer: top up from SQL.
        path = "vector+sql"

    offset = 0
    while offset < MAX_POOL:
        batch = await _sql_candidates(
            db, rfq, requester_id, limit=min(CANDIDATE_POOL, MAX_POOL - offset), offset=offset
        )
        kept = consider(batch, {})
        # The pool is relevance-ordered, so a batch that produced nothing means
        # the remainder will not either.
        if len(batch) < CANDIDATE_POOL or kept == 0:
            break
        offset += CANDIDATE_POOL
    return scored, path


async def run_match(
    db: AsyncSession,
    user: User,
    rfq: RFQ,
    *,
    rfq_id: Optional[uuid.UUID] = None,
    conversation_id: Optional[uuid.UUID] = None,
    query: Optional[str] = None,
    source: str = "rfq",
    limit: int = 10,
    offset: int = 0,
) -> MatchResponse:
    """Run both stages for any RFQ-shaped thing.

    ``rfq`` may be a transient object that was never persisted -- direct search
    builds one from a parsed query so it can reuse the whole pipeline without
    committing an RFQ the user never asked to create. In that case ``rfq_id`` is
    None and the search is recorded against the conversation instead.

    ``total`` counts every candidate that passed the gates. SQL retrieval keeps
    reading relevance-ordered batches while they still produce matches, up to
    ``MAX_POOL`` rows, so the count is exact below that and a lower bound above.
    """
    # Load user's profile matching preferences if available (Pillar 4)
    user_prefs = None
    if user and user.profile and getattr(user.profile, "matching_preferences", None):
        user_prefs = user.profile.matching_preferences

    scored, path = await _collect(db, rfq, user.id, user_prefs)
    # Tie-break on recency so equal scores return in a stable, sensible order.
    scored.sort(key=lambda pair: (pair[1].total, pair[0].created_at), reverse=True)

    page = scored[offset : offset + limit]

    search = MatchSearch(
        user_id=user.id,
        rfq_id=rfq_id,
        conversation_id=conversation_id,
        query=query or rfq.search_text or rfq.title,
        requester_role=rfq.role,
        target_role=rfq.role.counterpart,
        filters={"limit": limit, "offset": offset, "source": source, "retrieval": path},
        shown_rfq_ids=[candidate.id for candidate, _ in page],
        last_cursor=offset + len(page),
    )
    db.add(search)
    await db.flush()

    # One lookup for the whole page: which of these have I already asked to be
    # put in touch with, and did they say yes?
    existing = await _connections_for(db, user.id, [c.id for c, _ in page])

    results = []
    for index, (candidate, score) in enumerate(page):
        rank = offset + index + 1
        db.add(
            MatchResult(
                search_id=search.id,
                rfq_id=candidate.id,
                rank=rank,
                score=score.total,
                semantic_score=score.relevance,
                score_breakdown=score.model_dump(exclude={"total"}),
            )
        )
        results.append(
            _to_candidate(rfq, candidate, score, rank, existing.get(candidate.id))
        )

    await db.commit()

    return MatchResponse(
        search_id=search.id,
        requester_role=rfq.role,
        target_role=rfq.role.counterpart,
        results=results,
        total=len(scored),
        limit=limit,
        offset=offset,
    )

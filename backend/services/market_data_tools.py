"""Read-only marketplace data tools for the AI chat.

Why this exists: the assistant used to be handed a keyword-gated block of
pre-fetched aggregates and then free-wrote its answer, which let it invent
numbers (and count one user's listing posted five times as five "market" data
points). Now the model *looks things up* through these tools, and every number
in its answer must trace back to a tool result (see ``answer_verifier``).

Every tool is ``async def tool(db, *, user, **args) -> dict`` returning::

    {"tool": name, "args": {...}, "ok": bool, "data": {...},
     "facts": [{"id": "F1", "label": ..., "value": ..., "unit": ...}],
     "notes": [str]}

``facts`` are the atomic, citable values. Nothing here knows a price from
"general knowledge": an empty result is reported as an empty result.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import RFQRole, RFQStatus
from models.rfq import RFQ
from models.user import User
from services import product_synonyms

logger = logging.getLogger(__name__)

# Rows read per query before dedup; a safety cap, not a statistic.
_ROW_CAP = 5000
# Fewer distinct sellers/buyers than this and a price range is anecdote.
MIN_DISTINCT_USERS = 3

# Words that describe the request, not the product; matching a listing on them
# alone would pull in unrelated rows.
_BOILERPLATE_WORDS = {
    "role", "category", "product", "quantity", "target", "price", "location", "deadline",
    "attributes", "notes", "buyer", "seller", "units", "procurement", "listing", "listings",
    "bottle", "bottles", "pcs", "piece", "pieces", "kg", "box", "boxes", "tin", "tins",
    "pack", "packs", "food", "beverages", "order", "orders", "supply", "trade", "material",
    "goods", "item", "items", "typical", "market", "condition", "conditions", "viable",
    "viability", "fair", "high", "cheap", "expensive", "quote", "quotes",
}


# --------------------------------------------------------------------------- helpers


class _Facts:
    """Builds the citable ``facts`` list with sequential ids."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, label: str, value: Any, unit: str = "") -> None:
        self.items.append({"id": f"F{len(self.items) + 1}", "label": label, "value": value, "unit": unit})


def _result(
    tool: str,
    args: dict[str, Any],
    *,
    ok: bool = True,
    data: Optional[dict[str, Any]] = None,
    facts: Optional[_Facts] = None,
    notes: Optional[list[str]] = None,
) -> dict[str, Any]:
    return {
        "tool": tool,
        "args": {k: v for k, v in args.items() if v is not None},
        "ok": ok,
        "data": data or {},
        "facts": facts.items if facts else [],
        "notes": notes or [],
    }


def _error(tool: str, args: dict[str, Any], note: str) -> dict[str, Any]:
    return _result(tool, args, ok=False, notes=[note])


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_uuid(value: Any) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_role(value: Any) -> Optional[RFQRole]:
    text = (_clean_str(value) or "").lower()
    for role in RFQRole:
        if text == role.value:
            return role
    return None


def _pattern(term: str) -> str:
    return rf"\m{re.escape(term)}" if re.match(r"^\w", term) else re.escape(term)


def product_clauses(term: str) -> list[Any]:
    """SQL clauses matching listings of ``term`` (synonym-aware).

    Moved here from the old chat aggregates so the chat tools and anything else
    share one product filter: whole-term match on title/search text, every
    spelling of a known local trade name ("pyaz" -> onion), and an AND over the
    meaningful tokens so "red onion" does not match every "red" listing.
    """
    clean = (term or "").strip()
    if not clean:
        return []
    clauses: list[Any] = [RFQ.title.op("~*")(_pattern(clean))]
    if clean.lower() not in _BOILERPLATE_WORDS:
        clauses.append(RFQ.search_text.op("~*")(_pattern(clean)))
        variants = product_synonyms.variants(clean) - {clean.lower()}
        if variants:
            alt = "|".join(re.escape(v) for v in sorted(variants))
            clauses.append(RFQ.title.op("~*")(rf"\m(?:{alt})"))
            clauses.append(RFQ.search_text.op("~*")(rf"\m(?:{alt})"))

    tokens = [
        t for t in re.split(r"[^\w]+", clean.lower())
        if len(t) >= 3 and t not in _BOILERPLATE_WORDS
    ]
    if len(tokens) > 1:
        per_token = []
        for tok in tokens:
            alt = "|".join(re.escape(v) for v in sorted(product_synonyms.variants(tok)))
            pat = rf"\m(?:{alt})"
            per_token.append(or_(RFQ.title.op("~*")(pat), RFQ.search_text.op("~*")(pat)))
        clauses.append(and_(*per_token))
    return clauses


def active_conditions(now: Optional[datetime] = None) -> list[Any]:
    """ACTIVE and not expired: one visibility rule for every count and price."""
    now = now or datetime.now(timezone.utc)
    return [RFQ.status == RFQStatus.ACTIVE, or_(RFQ.expires_at.is_(None), RFQ.expires_at > now)]


def _title_key(title: Optional[str]) -> str:
    return " ".join((title or "").lower().split())


def dedup_key(row: Any) -> tuple[Any, ...]:
    """Same user + same title + same qty + same price is one listing posted repeatedly."""
    return (
        row.user_id,
        _title_key(row.title),
        _num(row.quantity_value),
        _num(row.price_amount),
        (row.price_per_unit or "").strip().lower(),
    )


async def _distinct_listings(
    db: AsyncSession,
    user: User,
    *,
    product: Optional[str] = None,
    category: Optional[str] = None,
    role: Optional[str] = None,
    city: Optional[str] = None,
    exclude_rfq_id: Optional[str] = None,
    include_mine: bool = False,
    priced_only: bool = False,
) -> tuple[list[Any], int]:
    """Active, unexpired, de-duplicated listings and how many duplicates were dropped."""
    conditions = active_conditions()
    if product:
        clauses = product_clauses(product)
        if clauses:
            conditions.append(or_(*clauses))
    if category:
        conditions.append(RFQ.category.ilike(f"%{category.strip()}%"))
    parsed_role = _parse_role(role)
    if parsed_role is not None:
        conditions.append(RFQ.role == parsed_role)
    if city:
        conditions.append(RFQ.location_city.ilike(f"%{city.strip()}%"))
    excluded = _parse_uuid(exclude_rfq_id)
    if excluded is not None:
        conditions.append(RFQ.id != excluded)
    if not include_mine and user is not None:
        conditions.append(RFQ.user_id != user.id)
    if priced_only:
        conditions.append(RFQ.price_amount.is_not(None))

    rows = (
        await db.execute(
            select(
                RFQ.id, RFQ.user_id, RFQ.title, RFQ.role, RFQ.category, RFQ.quantity_value,
                RFQ.quantity_unit, RFQ.price_amount, RFQ.price_currency, RFQ.price_per_unit,
                RFQ.location_city, RFQ.created_at,
            )
            .where(*conditions)
            .order_by(RFQ.created_at.desc())
            .limit(_ROW_CAP)
        )
    ).all()

    seen: set[tuple[Any, ...]] = set()
    distinct: list[Any] = []
    for row in rows:
        key = dedup_key(row)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(row)
    return distinct, len(rows) - len(distinct)


def _sample(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "title": row.title,
        "role": row.role.value,
        "quantity": _num(row.quantity_value),
        "quantity_unit": row.quantity_unit,
        "price": _num(row.price_amount),
        "currency": row.price_currency if row.price_amount is not None else None,
        "price_per_unit": row.price_per_unit,
        "city": row.location_city,
    }


def normalise_prices(rows: list[Any]) -> dict[str, Any]:
    """Put priced listings on one comparable basis.

    Currencies are converted to one display currency, mass units are restated
    per tonne, and the most common unit basis wins; listings that cannot be
    converted or sit on another basis are counted as excluded rather than
    silently dropped. ``included`` is a list of ``(role, amount)``.
    """
    from services import currency as currency_service
    from services import unit_converter

    if not rows:
        return {"currency": None, "unit_basis": None, "included": [], "excluded": 0, "converted": 0}

    currencies = [str(r.price_currency or "INR").upper() for r in rows]
    display = "INR" if "INR" in currencies else max(set(currencies), key=currencies.count)

    normalised: list[tuple[str, Any, Decimal, bool]] = []
    for r in rows:
        source = str(r.price_currency or "INR").upper()
        amount = currency_service.convert(Decimal(r.price_amount), source, display)
        if amount is None:
            continue
        unit = (r.price_per_unit or "unit").strip().lower()
        if unit_converter.is_mass_unit(unit):
            amount = amount / unit_converter.to_tonnes(Decimal(1), unit)
            basis = "tonne"
        else:
            basis = unit
        normalised.append((basis, r.role, amount, source != display))

    if not normalised:
        return {"currency": display, "unit_basis": None, "included": [], "excluded": len(rows), "converted": 0}

    bases = [n[0] for n in normalised]
    basis = max(set(bases), key=bases.count)
    included = [n for n in normalised if n[0] == basis]
    return {
        "currency": display,
        "unit_basis": basis,
        "included": [(n[1], float(n[2])) for n in included],
        "excluded": len(rows) - len(included),
        "converted": sum(1 for n in included if n[3]),
    }


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..1) of an already sorted list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _round(value: float) -> float:
    return round(value, 2)


def _summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "median": _round(_percentile(ordered, 0.5)),
        "p25": _round(_percentile(ordered, 0.25)),
        "p75": _round(_percentile(ordered, 0.75)),
        "min": _round(ordered[0]),
        "max": _round(ordered[-1]),
    }


def _query_args(**kw: Any) -> dict[str, Any]:
    return {k: v for k, v in kw.items() if v not in (None, "", False)}


# --------------------------------------------------------------------------- tools


async def get_listing(db: AsyncSession, *, user: User, rfq_id: Any = None, **_: Any) -> dict[str, Any]:
    """One listing: only if it is active or belongs to the requesting user."""
    args = {"rfq_id": str(rfq_id) if rfq_id else None}
    rid = _parse_uuid(rfq_id)
    if rid is None:
        return _error("get_listing", args, "rfq_id must be a valid listing id")
    rfq = await db.get(RFQ, rid)
    is_mine = bool(rfq and user is not None and rfq.user_id == user.id)
    if rfq is None or not (is_mine or rfq.status == RFQStatus.ACTIVE):
        return _error("get_listing", args, "listing not found or not visible to you")

    facts = _Facts()
    facts.add("title", rfq.title)
    facts.add("role", rfq.role.value)
    if rfq.quantity_value is not None:
        facts.add("quantity", _num(rfq.quantity_value), rfq.quantity_unit or "")
    if rfq.price_amount is not None:
        facts.add("listed price", _num(rfq.price_amount), f"{rfq.price_currency}/{rfq.price_per_unit or 'unit'}")
    if rfq.location_city:
        facts.add("city", rfq.location_city)
    data = {
        "id": str(rfq.id),
        "title": rfq.title,
        "role": rfq.role.value,
        "status": rfq.status.value,
        "category": rfq.category,
        "product": (rfq.product_details or {}).get("name"),
        "quantity": _num(rfq.quantity_value),
        "quantity_unit": rfq.quantity_unit,
        "price": _num(rfq.price_amount),
        "currency": rfq.price_currency if rfq.price_amount is not None else None,
        "price_per_unit": rfq.price_per_unit,
        "city": rfq.location_city,
        "deadline": rfq.deadline_at.isoformat() if rfq.deadline_at else None,
        "created_at": rfq.created_at.isoformat() if rfq.created_at else None,
        "is_mine": is_mine,
    }
    return _result("get_listing", args, data=data, facts=facts)


async def search_listings(
    db: AsyncSession,
    *,
    user: User,
    product: Optional[str] = None,
    category: Optional[str] = None,
    role: Optional[str] = None,
    city: Optional[str] = None,
    exclude_rfq_id: Optional[str] = None,
    include_mine: bool = False,
    limit: int = 10,
    **_: Any,
) -> dict[str, Any]:
    """Distinct active listings plus counts; duplicates are removed and reported."""
    args = _query_args(
        product=product, category=category, role=role, city=city,
        exclude_rfq_id=exclude_rfq_id, include_mine=include_mine, limit=limit,
    )
    limit = max(1, min(int(limit or 10), 25))
    rows, dupes = await _distinct_listings(
        db, user, product=product, category=category, role=role, city=city,
        exclude_rfq_id=exclude_rfq_id, include_mine=include_mine,
    )
    by_role = {r.value: sum(1 for x in rows if x.role == r) for r in RFQRole}
    users = len({x.user_id for x in rows})

    facts = _Facts()
    facts.add("distinct active listings", len(rows))
    facts.add("distinct users behind them", users)
    facts.add("duplicate listings removed", dupes)
    facts.add("buyer listings", by_role.get("buyer", 0))
    facts.add("seller listings", by_role.get("seller", 0))

    notes: list[str] = []
    if not rows:
        notes.append("No matching active listings on the platform (own listings excluded unless include_mine).")
    if dupes:
        notes.append(f"{dupes} duplicate listing(s) (same user, title, quantity and price) were removed.")
    if not include_mine:
        notes.append("The requesting user's own listings are excluded.")
    data = {
        "distinct_listings": len(rows),
        "distinct_users": users,
        "duplicates_removed": dupes,
        "by_role": by_role,
        "samples": [_sample(r) for r in rows[:limit]],
    }
    return _result("search_listings", args, data=data, facts=facts, notes=notes)


async def market_price_stats(
    db: AsyncSession,
    *,
    user: User,
    product: Optional[str] = None,
    category: Optional[str] = None,
    role: Optional[str] = None,
    city: Optional[str] = None,
    exclude_rfq_id: Optional[str] = None,
    **_: Any,
) -> dict[str, Any]:
    """Price distribution of distinct listings on the platform; n=0 is a valid answer."""
    args = _query_args(product=product, category=category, role=role, city=city, exclude_rfq_id=exclude_rfq_id)
    rows, dupes = await _distinct_listings(
        db, user, product=product, category=category, role=role, city=city,
        exclude_rfq_id=exclude_rfq_id, include_mine=False, priced_only=True,
    )
    facts = _Facts()
    notes: list[str] = []
    if dupes:
        notes.append(f"{dupes} duplicate listing(s) (same user, title, quantity and price) were removed.")
    if not rows:
        notes.append("No priced active listings found on the platform for this query.")
        facts.add("priced listings", 0)
        return _result(
            "market_price_stats", args,
            data={"n_listings": 0, "n_distinct_users": 0, "duplicates_removed": dupes}, facts=facts, notes=notes,
        )

    norm = normalise_prices(rows)
    amounts = [a for _, a in norm["included"]]
    if not amounts:
        notes.append("Listings exist but none could be converted to a comparable price basis.")
        facts.add("priced listings", 0)
        return _result(
            "market_price_stats", args,
            data={"n_listings": 0, "n_distinct_users": 0, "duplicates_removed": dupes,
                  "excluded_price_count": norm["excluded"]},
            facts=facts, notes=notes,
        )

    overall = _summary(amounts)
    users = len({r.user_id for r in rows})
    data: dict[str, Any] = {
        "n_listings": overall["n"],
        "n_distinct_users": users,
        "median": overall["median"],
        "p25": overall["p25"],
        "p75": overall["p75"],
        "min": overall["min"],
        "max": overall["max"],
        "currency": norm["currency"],
        "unit_basis": norm["unit_basis"],
        "duplicates_removed": dupes,
        "excluded_price_count": norm["excluded"],
        "converted_currency_count": norm["converted"],
        "by_role": {
            r.value: _summary([a for role_, a in norm["included"] if role_ == r])
            for r in RFQRole
            if any(role_ == r for role_, _ in norm["included"])
        },
    }
    if users < MIN_DISTINCT_USERS:
        data["sample_warning"] = (
            f"only {users} distinct user(s) behind these prices; too few to call a market range"
        )
        notes.append(data["sample_warning"])
    if norm["excluded"]:
        notes.append(f"{norm['excluded']} listing(s) priced on a different unit basis are left out.")

    unit = f"{norm['currency']}/{norm['unit_basis']}"
    facts.add("priced listings", overall["n"])
    facts.add("distinct users", users)
    facts.add("median price", overall["median"], unit)
    facts.add("25th percentile", overall["p25"], unit)
    facts.add("75th percentile", overall["p75"], unit)
    facts.add("lowest price", overall["min"], unit)
    facts.add("highest price", overall["max"], unit)
    for role_name, stats in data["by_role"].items():
        facts.add(f"{role_name} listings priced", stats["n"])
        facts.add(f"{role_name} median price", stats["median"], unit)
    return _result("market_price_stats", args, data=data, facts=facts, notes=notes)


async def find_matches(
    db: AsyncSession, *, user: User, rfq_id: Any = None, limit: int = 5, **_: Any
) -> dict[str, Any]:
    """Counterparties the platform's matcher ranks for a listing (read-only, nothing is saved)."""
    from services import match_service

    args = {"rfq_id": str(rfq_id) if rfq_id else None, "limit": limit}
    rid = _parse_uuid(rfq_id)
    if rid is None:
        return _error("find_matches", args, "rfq_id must be a valid listing id")
    rfq = await db.get(RFQ, rid)
    if rfq is None or not (rfq.user_id == user.id or rfq.status == RFQStatus.ACTIVE):
        return _error("find_matches", args, "listing not found or not visible to you")

    limit = max(1, min(int(limit or 5), 10))
    from models.user import UserProfile

    # Queried, not read off ``user.profile``: a lazy load inside the async session raises.
    prefs = (
        await db.execute(select(UserProfile.matching_preferences).where(UserProfile.user_id == user.id))
    ).scalar_one_or_none() or None
    scored, path = await match_service._collect(db, rfq, user.id, prefs)
    scored.sort(key=lambda pair: (pair[1].total, pair[0].created_at), reverse=True)

    facts = _Facts()
    facts.add("matching counterparties", len(scored))
    samples = []
    for cand, score in scored[:limit]:
        sample = _sample(cand)
        sample["match_score_pct"] = round(score.total * 100)
        samples.append(sample)
    notes = [] if scored else ["The matcher found no counterparties for this listing."]
    return _result(
        "find_matches", args,
        data={"total_matches": len(scored), "retrieval": path, "matches": samples},
        facts=facts, notes=notes,
    )


async def get_my_rfqs(
    db: AsyncSession, *, user: User, status: Optional[str] = None, limit: int = 20, **_: Any
) -> dict[str, Any]:
    """The requesting user's own listings, optionally by status."""
    args = _query_args(status=status, limit=limit)
    conditions: list[Any] = [RFQ.user_id == user.id]
    if status:
        try:
            conditions.append(RFQ.status == RFQStatus((status or "").lower()))
        except ValueError:
            return _error("get_my_rfqs", args, f"unknown status '{status}'")
    limit = max(1, min(int(limit or 20), 50))
    rows = (
        await db.execute(
            select(
                RFQ.id, RFQ.user_id, RFQ.title, RFQ.role, RFQ.category, RFQ.quantity_value, RFQ.quantity_unit,
                RFQ.price_amount, RFQ.price_currency, RFQ.price_per_unit, RFQ.location_city, RFQ.created_at,
                RFQ.status,
            ).where(*conditions).order_by(RFQ.created_at.desc()).limit(limit)
        )
    ).all()
    facts = _Facts()
    facts.add("your listings returned", len(rows))
    items = []
    for r in rows:
        item = _sample(r)
        item["status"] = r.status.value
        items.append(item)
    notes = [] if rows else ["You have no listings matching that filter."]
    return _result("get_my_rfqs", args, data={"count": len(rows), "rfqs": items}, facts=facts, notes=notes)


async def estimate_freight(
    db: AsyncSession,
    *,
    user: User,
    origin: Optional[str] = None,
    destination: Optional[str] = None,
    weight_kg: Any = None,
    mode: Optional[str] = None,
    **_: Any,
) -> dict[str, Any]:
    """Route distance only; the platform has no verified freight rates.

    ``logistics_service.estimate_freight_rates`` prices freight from typed-in
    constants, not carrier data, so its numbers must never reach a user as
    fact. Weight and mode are accepted so the model can ask, but the answer is
    always "no verified rates; request a quote". Unknown places are refused.
    """
    from services import logistics_service
    from services.match_scoring import haversine_km

    args = _query_args(origin=origin, destination=destination, weight_kg=weight_kg, mode=mode)
    if not origin or not destination:
        return _error("estimate_freight", args, "origin and destination are both required")

    try:
        o_lat, o_lon = logistics_service._get_city_coords(origin, "")
        d_lat, d_lon = logistics_service._get_city_coords(destination, "")
    except logistics_service.UnknownLocationError as exc:
        return _error("estimate_freight", args, f"{exc}; distance not available for places the platform cannot locate")

    distance = max(50.0, haversine_km(o_lat, o_lon, d_lat, d_lon) * 1.25)
    facts = _Facts()
    facts.add("approximate route distance", round(distance), "km")
    data: dict[str, Any] = {
        "origin": origin, "destination": destination, "distance_km": round(distance),
        "freight_rates_available": False,
    }
    notes = [
        "Distance is straight-line x1.25, not a driving route.",
        "No verified freight rates on the platform; ask a logistics partner or carrier for a quote.",
    ]
    return _result("estimate_freight", args, data=data, facts=facts, notes=notes)


async def company_profile(db: AsyncSession, *, user: User, user_id: Any = None, **_: Any) -> dict[str, Any]:
    """Public verification and trust facts for a marketplace user (no contact details)."""
    from models.user import UserProfile

    args = {"user_id": str(user_id) if user_id else None}
    uid = _parse_uuid(user_id)
    if uid is None:
        return _error("company_profile", args, "user_id must be a valid user id")
    # Only counterparties the caller can actually see: someone with an active listing, or themselves.
    visible = uid == user.id or (
        await db.scalar(select(RFQ.id).where(RFQ.user_id == uid, *active_conditions()).limit(1))
    ) is not None
    if not visible:
        return _error("company_profile", args, "user not found or not visible to you")
    profile = (await db.execute(select(UserProfile).where(UserProfile.user_id == uid))).scalars().first()
    if profile is None:
        return _error("company_profile", args, "no profile on record")

    kyc = getattr(profile.kyc_status, "value", str(profile.kyc_status))
    facts = _Facts()
    facts.add("KYC status", kyc)
    facts.add("trust score", profile.trust_score)
    if profile.total_reviews:
        facts.add("average rating", float(profile.average_rating or 0))
        facts.add("review count", profile.total_reviews)
    data = {
        "company_name": profile.company_name,
        "kyc_status": kyc,
        "gst_on_file": bool(profile.gst_number),
        "trust_score": profile.trust_score,
        "average_rating": float(profile.average_rating) if profile.average_rating is not None else None,
        "total_reviews": profile.total_reviews,
        "city": profile.city,
    }
    return _result("company_profile", args, data=data, facts=facts)


# --------------------------------------------------------------------------- registry

_TOOLS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "get_listing": get_listing,
    "search_listings": search_listings,
    "market_price_stats": market_price_stats,
    "find_matches": find_matches,
    "get_my_rfqs": get_my_rfqs,
    "estimate_freight": estimate_freight,
    "company_profile": company_profile,
}

READ_TOOL_NAMES = frozenset(_TOOLS)

# Argument names each tool accepts; run_tool drops anything else.
_ALLOWED_ARGS: dict[str, set[str]] = {
    "get_listing": {"rfq_id"},
    "search_listings": {"product", "category", "role", "city", "exclude_rfq_id", "include_mine", "limit"},
    "market_price_stats": {"product", "category", "role", "city", "exclude_rfq_id"},
    "find_matches": {"rfq_id", "limit"},
    "get_my_rfqs": {"status", "limit"},
    "estimate_freight": {"origin", "destination", "weight_kg", "mode"},
    "company_profile": {"user_id"},
}


def _fn(name: str, description: str, properties: dict[str, Any], required: Optional[list[str]] = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


_S = {"type": "string"}

READ_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn(
        "get_listing",
        "Look up one marketplace listing (RFQ) by id: title, buyer/seller role, product, quantity, listed price, city, "
        "deadline. Call this first when the user asks about a specific listing.",
        {"rfq_id": {**_S, "description": "Listing UUID"}}, ["rfq_id"],
    ),
    _fn(
        "search_listings",
        "Count and sample ACTIVE marketplace listings (buyers and sellers) for a product/category/city. Duplicates "
        "are removed and the user's own listings excluded by default. Use for 'how many sellers of X', 'who sells X'. "
        "Returns zero counts when nothing exists, which is a real answer.",
        {
            "product": {**_S, "description": "Product name, e.g. 'onion'"},
            "category": _S,
            "role": {**_S, "enum": ["buyer", "seller"]},
            "city": _S,
            "exclude_rfq_id": {**_S, "description": "Listing id to leave out (the one being analysed)"},
            "include_mine": {"type": "boolean"},
            "limit": {"type": "integer"},
        },
    ),
    _fn(
        "market_price_stats",
        "Price statistics (median, 25th/75th percentile, min, max, count, distinct users) of ACTIVE platform listings "
        "for a product. The only valid source of platform price comparisons; may report sample_warning when few users.",
        {
            "product": _S, "category": _S, "role": {**_S, "enum": ["buyer", "seller"]}, "city": _S,
            "exclude_rfq_id": {**_S, "description": "Listing id to leave out (the one being analysed)"},
        },
    ),
    _fn(
        "find_matches",
        "Best matching counterparties on the platform for one listing (uses the platform matcher).",
        {"rfq_id": _S, "limit": {"type": "integer"}}, ["rfq_id"],
    ),
    _fn(
        "get_my_rfqs",
        "List the current user's own RFQs, optionally filtered by status (draft, active, closed, expired).",
        {"status": _S, "limit": {"type": "integer"}},
    ),
    _fn(
        "estimate_freight",
        "Approximate route distance between two places. The platform has no verified freight rates, so this "
        "never returns a price; tell the user to request a quote from a logistics partner.",
        {"origin": _S, "destination": _S, "weight_kg": {"type": "number"}, "mode": {**_S, "enum": ["road", "ocean", "air"]}},
        ["origin", "destination"],
    ),
    _fn(
        "company_profile",
        "Public verification facts of a marketplace user: KYC status, trust score, rating.",
        {"user_id": _S}, ["user_id"],
    ),
]


async def run_tool(db: AsyncSession, user: User, name: str, args: Any) -> dict[str, Any]:
    """Validate and execute one read tool. Never raises: errors become ``ok=False`` plus a note."""
    fn = _TOOLS.get(name)
    if fn is None:
        return _error(str(name), {}, f"unknown tool '{name}'")
    if isinstance(args, str):
        import json

        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        return _error(name, {}, "arguments must be an object")
    clean = {k: v for k, v in args.items() if k in _ALLOWED_ARGS[name] and v is not None}
    try:
        return await fn(db, user=user, **clean)
    except Exception as exc:
        logger.warning("read tool %s failed: %s", name, exc)
        try:
            await db.rollback()
        except Exception:
            pass
        return _error(name, clean, f"tool failed: {type(exc).__name__}")

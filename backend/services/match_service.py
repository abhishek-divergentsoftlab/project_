"""Two-stage matching: retrieve candidates, then apply business rules.

    Stage 1  hard filters + relevance retrieval  ->  a broad candidate pool
    Stage 2  business compatibility + reranking  ->  the top K shown

Stage 1 is currently a PostgreSQL query using tag overlap and category. It is
the seam where Qdrant goes: replace ``_fetch_candidates`` with a vector search
carrying the same hard filters and nothing downstream changes.

The hard filters are not negotiable, and the first of them is the marketplace's
central rule -- a buyer is only ever shown sellers, and a seller only buyers.
"""

import uuid
from datetime import UTC, datetime
from typing import Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.connection import Connection
from models.enums import ConnectionStatus, RFQStatus
from models.match import MatchResult, MatchSearch
from models.rfq import RFQ
from models.user import User
from schemas.common import DeadlineOut, Location, Money, Quantity
from schemas.match import Counterparty, MatchCandidate, MatchResponse, MatchScore
from core.config import settings
from services import match_scoring, qdrant_index
from services.embeddings import embed_one
from services.rfq_indexing import build_match_text

# How many rows stage 1 hands to stage 2. Business rules reorder heavily, so the
# pool has to be considerably wider than the page being returned.
CANDIDATE_POOL = 200

# Below this, a candidate is noise rather than a weak match.
MIN_SCORE = 0.05

# Price and quantity only mean something between comparable products. Below this
# relevance, "cheaper" is comparing a box price against a cable budget, and a
# stock level against an unrelated requirement -- so both are left unscored
# rather than being awarded full marks for a meaningless comparison.
COMPARABLE_FLOOR = 0.35


async def _fetch_candidates(
    db: AsyncSession, rfq: RFQ, requester_id: uuid.UUID
) -> tuple[Sequence[RFQ], dict[uuid.UUID, float]]:
    """Stage 1. Vector retrieval when it is available, SQL when it is not.

    Returns the candidate rows and, when the vector path ran, the cosine
    similarity for each. Rows always come back from PostgreSQL: Qdrant supplies
    the ids and the ranking, never the data.
    """
    vector_hits = await _vector_candidates(rfq, requester_id)
    if vector_hits is not None:
        rows = await _load_active(db, list(vector_hits), rfq, requester_id)
        return rows, vector_hits

    return await _sql_candidates(db, rfq, requester_id), {}


async def _vector_candidates(
    rfq: RFQ, requester_id: uuid.UUID
) -> Optional[dict[uuid.UUID, float]]:
    """Ids and cosine scores from Qdrant, or None if it cannot answer."""
    # Must be the same projection the index was built from, or the query and
    # the corpus sit in different parts of the space.
    vector = await embed_one(build_match_text(rfq))
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


async def _load_active(
    db: AsyncSession,
    ids: list[uuid.UUID],
    rfq: RFQ,
    requester_id: uuid.UUID,
) -> Sequence[RFQ]:
    """Read the rows back, re-applying the hard filters.

    The index can lag the database, so its answer is treated as a suggestion.
    Status and expiry are confirmed against the source of truth before anything
    is shown.
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
                RFQ.role == rfq.role.counterpart,
                RFQ.status == RFQStatus.ACTIVE,
                RFQ.user_id != requester_id,
                or_(RFQ.expires_at.is_(None), RFQ.expires_at > now),
            )
        )
    ).all()
    return rows


async def _sql_candidates(
    db: AsyncSession, rfq: RFQ, requester_id: uuid.UUID
) -> Sequence[RFQ]:
    """Fallback when Qdrant or the embedding model is unavailable."""
    now = datetime.now(UTC)

    query = (
        select(RFQ)
        .options(selectinload(RFQ.user).selectinload(User.profile))
        .where(
            # The central rule: match against the opposite side of the market.
            RFQ.role == rfq.role.counterpart,
            RFQ.status == RFQStatus.ACTIVE,
            RFQ.user_id != requester_id,
            or_(RFQ.expires_at.is_(None), RFQ.expires_at > now),
        )
    )

    # Tag overlap ORDERS the pool, it does not filter it.
    #
    # Filtering on it was excluding real matches: a buyer asking for "type c
    # cables" tagged theirs `type c cable`, which does not array-overlap a
    # seller's `usb type-c cable`, so only products sharing the incidental tag
    # `white` were ever retrieved. Stage 2 compares tags properly, by token, so
    # stage 1 only has to make sure the right rows are in the pool.
    if rfq.search_tags:
        query = query.order_by(
            RFQ.search_tags.overlap(rfq.search_tags).desc(), RFQ.created_at.desc()
        )
    else:
        query = query.order_by(RFQ.created_at.desc())

    # Recall is bounded by the pool size. That is acceptable while the whole of
    # stage 1 is a placeholder: Qdrant replaces this with a real ranked
    # retrieval, at which point the ordering above becomes the vector search.
    return (await db.scalars(query.limit(CANDIDATE_POOL))).all()


def _score(
    rfq: RFQ, candidate: RFQ, similarity: Optional[float] = None
) -> tuple[MatchScore, bool]:
    """Stage 2. Apply the compatibility rules to one candidate.

    Returns the score and whether the candidate is worth showing at all.
    """
    # Price and quantity are directional: whoever is buying supplies the target
    # and the requirement, whoever is selling supplies the ask and the stock.
    if rfq.role.value == "buyer":
        buyer, seller = rfq, candidate
    else:
        buyer, seller = candidate, rfq

    if similarity is not None:
        # Cosine similarity from the vector index. Retrieval already applied the
        # threshold, so anything that arrives here has cleared the semantic bar
        # and does not need a second gate.
        relevance = similarity
        floor = 0.0
    else:
        # Lexical fallback; its scores sit on a different scale, so it carries
        # its own floor.
        relevance = match_scoring.relevance_score(
            rfq.search_tags, build_match_text(rfq),
            candidate.search_tags, build_match_text(candidate),
        )
        floor = COMPARABLE_FLOOR

    category = match_scoring.category_score(rfq.category, candidate.category)
    comparable = relevance >= floor

    scores: dict[str, Optional[float]] = {
        "relevance": relevance,
        "category": category,
        "attributes": match_scoring.attribute_score(
            rfq.product_details or {}, candidate.product_details or {}
        ),
        "price": match_scoring.price_score(
            buyer.price_amount, seller.price_amount, buyer.price_currency, seller.price_currency
        ) if comparable else None,
        "quantity": match_scoring.quantity_score(
            buyer.quantity_value, buyer.quantity_unit,
            seller.quantity_value, seller.quantity_unit,
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
        "deadline": match_scoring.deadline_score(rfq.deadline_at, candidate.deadline_at),
    }

    # Same product only. The old rule also kept anything sharing a category,
    # which is why a search for cables always returned the same GaN chargers --
    # every Electronics listing qualified no matter what was asked for.
    keep = comparable
    return MatchScore(total=match_scoring.blend(scores), **scores), keep


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

    return Counterparty(
        company_name=profile.company_name if profile else None,
        city=profile.city if profile else None,
        state=profile.state if profile else None,
        country=profile.country if profile else None,
        member_since=candidate.user.created_at if candidate.user else None,
        connection_id=connection.id if connection else None,
        connection_status=connection.status if connection else None,
        contact_name=(profile.name if profile else None) if accepted else None,
        email=(candidate.user.email if candidate.user else None) if accepted else None,
        phone=(profile.phone if profile else None) if accepted else None,
        address=(profile.address if profile else None) if accepted else None,
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
    deadline = (
        DeadlineOut(date=candidate.deadline_at, raw=candidate.deadline_raw)
        if candidate.deadline_at is not None
        else None
    )

    raw_distance = match_scoring.distance_km(
        {"latitude": requester.latitude, "longitude": requester.longitude},
        {"latitude": candidate.latitude, "longitude": candidate.longitude},
    )
    distance = round(raw_distance, 1) if raw_distance is not None else None

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
    """
    candidates, similarities = await _fetch_candidates(db, rfq, user.id)

    scored = []
    for candidate in candidates:
        score, keep = _score(rfq, candidate, similarities.get(candidate.id))
        if keep and score.total >= MIN_SCORE:
            scored.append((candidate, score))
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
        filters={"limit": limit, "offset": offset, "source": source},
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

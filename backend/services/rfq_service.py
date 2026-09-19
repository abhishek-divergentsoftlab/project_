"""RFQ persistence.

Everything that writes an RFQ goes through here, because two invariants have to
hold on every single write:

1. the nested wire format and the flat typed columns stay in agreement, and
2. ``search_text`` / ``search_tags`` / ``embedding_status`` are recomputed, so
   the row can never be silently out of step with the vector index.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import RFQStatus
from models.rfq import RFQ
from models.user import User
from schemas.common import DeadlineOut, Location, Money, Quantity
from schemas.rfq import RFQCreate, RFQOut, RFQUpdate
from services import locations, qdrant_index
from services.embeddings import embed_one
from services.rfq_indexing import build_match_text, refresh_index_fields

# How long a listing stays matchable when the RFQ carries no delivery deadline.
DEFAULT_TTL_DAYS = 30


class RFQError(Exception):
    """Domain-level rejection, translated to a 4xx by the router."""


async def index_rfq(db: AsyncSession, rfq: RFQ) -> bool:
    """Embed one RFQ and push it to Qdrant. Best effort, never fatal.

    A failure leaves ``embedding_status`` as STALE/FAILED with the reason, which
    is exactly what ``scripts/index_vectors.py`` looks for, so the row is picked
    up on the next run instead of being quietly lost.
    """
    from models.enums import EmbeddingStatus

    # Product-only, to match what the index was built from.
    text = build_match_text(rfq)
    if not text:
        return False

    vector = await embed_one(text)
    if vector is None:
        rfq.embedding_status = EmbeddingStatus.FAILED
        rfq.embedding_error = "embedding model unavailable"
        ok = False
    else:
        ok = await qdrant_index.upsert(
            [(rfq.id, vector, qdrant_index.build_payload(rfq))]
        )
        if ok:
            rfq.embedding_status = EmbeddingStatus.INDEXED
            rfq.embedded_at = datetime.now(UTC)
            rfq.embedding_error = None
        else:
            rfq.embedding_status = EmbeddingStatus.FAILED
            rfq.embedding_error = "qdrant upsert failed"

    await db.commit()
    # updated_at carries a server-side onupdate, so committing here expires it.
    # Without this refresh the caller's next attribute read is lazy IO from
    # synchronous code, which raises MissingGreenlet.
    await db.refresh(rfq)
    return ok


def _apply_common_fields(rfq: RFQ, payload: RFQCreate | RFQUpdate) -> None:
    """Flatten the nested value objects onto typed columns."""
    if payload.quantity is not None:
        rfq.quantity_value = payload.quantity.value
        rfq.quantity_unit = payload.quantity.unit

    if payload.minimum_order is not None:
        rfq.min_order_value = payload.minimum_order.value
        rfq.min_order_unit = payload.minimum_order.unit

    if payload.price_target is not None:
        rfq.price_amount = payload.price_target.amount
        rfq.price_currency = payload.price_target.currency
        rfq.price_per_unit = payload.price_target.per_unit

    if payload.location is not None:
        location = payload.location
        rfq.location_city = location.city
        rfq.location_state = location.state
        rfq.location_country = location.country
        rfq.latitude = location.latitude
        rfq.longitude = location.longitude
        rfq.location_raw = location.raw
        _geocode(rfq)

    if payload.deadline is not None:
        # Resolved to an instant here, once, rather than re-interpreted on read.
        rfq.deadline_at = payload.deadline.resolve()
        rfq.deadline_raw = payload.deadline.raw


def _geocode(rfq: RFQ) -> None:
    """Fill in coordinates from the city name when the caller sent none.

    The new-RFQ form has a city field and no map, so every hand-made RFQ arrived
    without coordinates and could only ever be compared by string equality --
    "is it the same city, yes or no". With a known city the match can be scored
    on actual distance, and the card can say how far away the goods are.

    Only the blanks are filled: coordinates that were supplied are authoritative,
    and an unrecognised city is simply left as text.
    """
    if rfq.latitude is not None and rfq.longitude is not None:
        return
    if not rfq.location_city:
        return

    city = locations.find_city(rfq.location_city)
    if city is None:
        return

    # "Hamburg, India" is not the Hamburg in the gazetteer. Attaching German
    # coordinates to it would be worse than leaving it ungeocoded -- the new-RFQ
    # form used to default the country to India for every listing.
    stated = (rfq.location_country or "").strip().lower()
    if stated and stated != city.country.lower():
        return

    rfq.latitude = locations.as_decimal(city.latitude)
    rfq.longitude = locations.as_decimal(city.longitude)
    # Canonical spelling and the administrative levels that go with it, so
    # "indore" and "Indore" are not two different places.
    rfq.location_city = city.name
    if not rfq.location_state:
        rfq.location_state = city.region
    if not rfq.location_country:
        rfq.location_country = city.country


async def create_rfq(db: AsyncSession, user: User, payload: RFQCreate) -> RFQ:
    if payload.status not in (RFQStatus.DRAFT, RFQStatus.ACTIVE):
        raise RFQError("a new RFQ may only be created as 'draft' or 'active'")
    if not user.can_post_as(payload.role):
        raise RFQError(f"this account is registered as '{user.role.value}' and cannot post a {payload.role.value} RFQ")

    rfq = RFQ(
        user_id=user.id,
        role=payload.role,
        status=payload.status,
        category=payload.category,
        title=payload.title,
        description=payload.description,
        product_details=payload.product_details or {},
    )
    _apply_common_fields(rfq, payload)

    # A listing outlives its delivery deadline by nothing; without one it gets
    # a default TTL so the index does not fill with abandoned rows.
    rfq.expires_at = rfq.deadline_at or datetime.now(UTC) + timedelta(days=DEFAULT_TTL_DAYS)

    refresh_index_fields(rfq)

    db.add(rfq)
    await db.commit()
    await db.refresh(rfq)

    # Indexed inline so a new RFQ is findable immediately. This belongs in a
    # background job once there is a queue; until then, failure is tracked on
    # the row rather than raised at the user.
    await index_rfq(db, rfq)
    return rfq


async def update_rfq(db: AsyncSession, rfq: RFQ, payload: RFQUpdate) -> RFQ:
    # exclude_unset distinguishes "set this to null" from "do not touch".
    provided = payload.model_dump(exclude_unset=True)

    for field in ("category", "title", "description"):
        if field in provided:
            setattr(rfq, field, provided[field])

    if "product_details" in provided and payload.product_details is not None:
        rfq.product_details = payload.product_details

    if "status" in provided and payload.status is not None:
        rfq.status = payload.status

    _apply_common_fields(rfq, payload)

    if "deadline" in provided and payload.deadline is not None:
        rfq.expires_at = rfq.deadline_at

    # Any edit can change what the embedding should encode, so it is always
    # recomputed rather than guessed at field by field.
    refresh_index_fields(rfq)

    await db.commit()
    await db.refresh(rfq)

    await index_rfq(db, rfq)
    return rfq


def _owned_by(user_id: uuid.UUID) -> Select:
    return select(RFQ).where(RFQ.user_id == user_id)


async def get_rfq(db: AsyncSession, rfq_id: uuid.UUID) -> Optional[RFQ]:
    return await db.get(RFQ, rfq_id)


async def list_rfqs(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: Optional[RFQStatus] = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[Sequence[RFQ], int]:
    query = _owned_by(user_id)
    if status is not None:
        query = query.where(RFQ.status == status)

    total = await db.scalar(
        select(func.count()).select_from(query.subquery())
    )
    rows = await db.scalars(
        query.order_by(RFQ.created_at.desc()).limit(limit).offset(offset)
    )
    return rows.all(), int(total or 0)


async def expire_due_rfqs(db: AsyncSession) -> int:
    """Flip past-due active RFQs to EXPIRED. Intended for a scheduled job.

    Matching also filters on ``expires_at`` directly, so a delayed sweep can
    never leak a stale RFQ into results -- this just keeps status honest.
    """
    now = datetime.now(UTC)
    due = await db.scalars(
        select(RFQ).where(
            RFQ.status == RFQStatus.ACTIVE,
            RFQ.expires_at.is_not(None),
            RFQ.expires_at <= now,
        )
    )
    count = 0
    for rfq in due:
        rfq.status = RFQStatus.EXPIRED
        count += 1
    if count:
        await db.commit()
    return count


def to_out(rfq: RFQ, pending_connections: int = 0) -> RFQOut:
    """Reassemble the nested wire format from the flat columns."""
    quantity = (
        Quantity(value=rfq.quantity_value, unit=rfq.quantity_unit or "units")
        if rfq.quantity_value is not None
        else None
    )
    minimum_order = (
        Quantity(value=rfq.min_order_value, unit=rfq.min_order_unit or "units")
        if rfq.min_order_value is not None
        else None
    )
    price = (
        Money(
            amount=rfq.price_amount,
            currency=rfq.price_currency,
            per_unit=rfq.price_per_unit,
        )
        if rfq.price_amount is not None
        else None
    )
    location = None
    if any(
        value is not None
        for value in (
            rfq.location_city,
            rfq.location_state,
            rfq.location_country,
            rfq.latitude,
            rfq.longitude,
            rfq.location_raw,
        )
    ):
        location = Location(
            city=rfq.location_city,
            state=rfq.location_state,
            country=rfq.location_country,
            latitude=rfq.latitude,
            longitude=rfq.longitude,
            raw=rfq.location_raw,
        )
    deadline = (
        DeadlineOut(date=rfq.deadline_at, raw=rfq.deadline_raw)
        if rfq.deadline_at is not None
        else None
    )

    return RFQOut(
        id=rfq.id,
        user_id=rfq.user_id,
        role=rfq.role,
        status=rfq.status,
        category=rfq.category,
        title=rfq.title,
        description=rfq.description,
        quantity=quantity,
        minimum_order=minimum_order,
        price_target=price,
        location=location,
        deadline=deadline,
        product_details=rfq.product_details,
        search_tags=rfq.search_tags,
        pending_connections=pending_connections,
        embedding_status=rfq.embedding_status,
        expires_at=rfq.expires_at,
        created_at=rfq.created_at,
        updated_at=rfq.updated_at,
    )


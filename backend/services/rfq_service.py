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

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import RFQStatus
from models.rfq import RFQ
from models.user import User
from schemas.common import DeadlineOut, Location, Money, Quantity
from schemas.rfq import RFQCreate, RFQOut, RFQUpdate
from services import locations, qdrant_index
from services.categories import normalize_category
from services.embeddings import embed_one
from services.moderation_service import AIContentModerator
from services.rfq_indexing import build_match_text, refresh_index_fields

# How long a listing stays matchable when the RFQ carries no delivery deadline.
DEFAULT_TTL_DAYS = 30


class RFQError(Exception):
    """Domain-level rejection, translated to a 4xx by the router."""


class RFQStateError(RFQError):
    """The RFQ's lifecycle state does not allow the operation (HTTP 409)."""


# The only legal status changes. Everything else -- reopening a closed RFQ,
# un-expiring, moving active back to draft -- is refused.
ALLOWED_TRANSITIONS: dict[RFQStatus, frozenset[RFQStatus]] = {
    RFQStatus.DRAFT: frozenset({RFQStatus.ACTIVE, RFQStatus.CLOSED}),
    RFQStatus.ACTIVE: frozenset({RFQStatus.CLOSED, RFQStatus.EXPIRED}),
    RFQStatus.EXPIRED: frozenset(),
    RFQStatus.CLOSED: frozenset(),
}

# States whose content can no longer be edited.
IMMUTABLE_STATUSES = frozenset({RFQStatus.CLOSED, RFQStatus.EXPIRED})


def is_past_expiry(rfq: RFQ, now: Optional[datetime] = None) -> bool:
    expires_at = rfq.expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= (now or datetime.now(UTC))


def effective_status(rfq: RFQ, now: Optional[datetime] = None) -> RFQStatus:
    """The status a reader should see: an ACTIVE row past ``expires_at`` is
    EXPIRED even if the periodic sweep has not flipped it yet."""
    if rfq.status == RFQStatus.ACTIVE and is_past_expiry(rfq, now):
        return RFQStatus.EXPIRED
    return rfq.status


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

    # AI Safety & Prohibited Items Moderation
    mod_check = await AIContentModerator.audit_and_verify(
        db,
        user_id=user.id,
        action="rfq_create",
        title=payload.title,
        description=payload.description,
        category=payload.category,
    )
    if not mod_check.is_safe:
        raise RFQError(mod_check.reason or "Listing contains prohibited items")

    rfq = RFQ(
        user_id=user.id,
        role=payload.role,
        status=payload.status,
        category=normalize_category(payload.category),
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
    """Edit an RFQ's content.

    Closed and expired RFQs are immutable (``RFQStateError``). A ``status`` on
    the internal ``RFQUpdate`` is not applied directly: it is routed through
    ``publish_rfq`` / ``close_rfq`` so the transition table always holds.
    """
    # exclude_unset distinguishes "set this to null" from "do not touch".
    provided = payload.model_dump(exclude_unset=True)
    target_status = provided.pop("status", None)
    content_fields = {k for k in provided}

    if target_status is not None and not content_fields:
        if target_status == RFQStatus.CLOSED:
            return await close_rfq(db, rfq)
        if target_status == RFQStatus.ACTIVE:
            return await publish_rfq(db, rfq)
        if target_status == rfq.status:
            return rfq
        raise RFQStateError(
            f"cannot move an RFQ from {rfq.status.value} to {RFQStatus(target_status).value}"
        )
    if target_status is not None:
        raise RFQError("status cannot be changed together with other fields")

    await expire_due_rfqs(db, rfq_ids=[rfq.id])
    if rfq.status in IMMUTABLE_STATUSES:
        raise RFQStateError(f"a {rfq.status.value} RFQ can no longer be edited")

    # AI Safety & Prohibited Items Moderation on update.
    if any(k in provided for k in ("title", "description", "category", "product_details")):
        new_title = provided.get("title", rfq.title)
        new_desc = provided.get("description", rfq.description)
        new_cat = provided.get("category", rfq.category)
        mod_check = await AIContentModerator.audit_and_verify(
            db,
            user_id=rfq.user_id,
            action="rfq_update",
            title=new_title,
            description=new_desc,
            category=new_cat,
        )
        if not mod_check.is_safe:
            raise RFQError(mod_check.reason or "Listing contains prohibited items")

    if "title" in provided and provided["title"] is not None:
        rfq.title = provided["title"]
    if "category" in provided and provided["category"] is not None:
        rfq.category = normalize_category(provided["category"])
    if "description" in provided:
        rfq.description = provided["description"]

    if "product_details" in provided and payload.product_details is not None:
        rfq.product_details = payload.product_details

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


async def publish_rfq(db: AsyncSession, rfq: RFQ) -> RFQ:
    """draft -> active. Idempotent for an already-active, unexpired RFQ.

    A draft may have been written long ago: its expiry is recomputed at
    publish time (deadline, or a fresh default TTL), and a draft whose
    deadline has already passed is refused rather than published dead.
    """
    await expire_due_rfqs(db, rfq_ids=[rfq.id])
    if rfq.status == RFQStatus.ACTIVE:
        return rfq
    if RFQStatus.ACTIVE not in ALLOWED_TRANSITIONS[rfq.status]:
        raise RFQStateError(f"only a draft can be published; this RFQ is {rfq.status.value}")

    now = datetime.now(UTC)
    deadline_at = rfq.deadline_at
    if deadline_at is not None and deadline_at.tzinfo is None:
        deadline_at = deadline_at.replace(tzinfo=UTC)
    if deadline_at is not None and deadline_at <= now:
        raise RFQStateError("the RFQ deadline has already passed; set a new deadline before publishing")

    rfq.status = RFQStatus.ACTIVE
    rfq.expires_at = deadline_at or now + timedelta(days=DEFAULT_TTL_DAYS)
    refresh_index_fields(rfq)
    await db.commit()
    await db.refresh(rfq)
    await index_rfq(db, rfq)
    return rfq


async def close_rfq(db: AsyncSession, rfq: RFQ) -> RFQ:
    """draft/active -> closed (terminal). Idempotent for an already-closed RFQ.

    Never blocked by moderation: closing only takes a listing off the market.
    """
    await expire_due_rfqs(db, rfq_ids=[rfq.id])
    if rfq.status == RFQStatus.CLOSED:
        return rfq
    if RFQStatus.CLOSED not in ALLOWED_TRANSITIONS[rfq.status]:
        raise RFQStateError(f"a {rfq.status.value} RFQ cannot be closed")

    rfq.status = RFQStatus.CLOSED
    await db.commit()
    await db.refresh(rfq)
    await qdrant_index.delete([rfq.id])
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
    # Lazily keep status honest for the owner's own rows, so "My RFQs" shows
    # 'expired' without waiting for the periodic sweep.
    await expire_due_rfqs(db, user_id=user_id)

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


async def expire_due_rfqs(
    db: AsyncSession,
    *,
    user_id: Optional[uuid.UUID] = None,
    rfq_ids: Optional[Sequence[uuid.UUID]] = None,
) -> int:
    """Flip past-due active RFQs to EXPIRED. Returns how many were flipped.

    Called by the periodic scheduler with no arguments (global sweep), and
    lazily -- scoped to one owner or a few ids -- before RFQ reads and state
    transitions. A single conditional UPDATE, so it is idempotent and safe to
    run concurrently: a row already EXPIRED/CLOSED never matches again.

    Matching and the catalog also filter on ``expires_at`` directly, so a
    delayed sweep can never leak a stale RFQ into results.
    """
    now = datetime.now(UTC)
    stmt = (
        update(RFQ)
        .where(
            RFQ.status == RFQStatus.ACTIVE,
            RFQ.expires_at.is_not(None),
            RFQ.expires_at <= now,
        )
        .values(status=RFQStatus.EXPIRED)
        .returning(RFQ.id)
        .execution_options(synchronize_session="fetch")
    )
    if user_id is not None:
        stmt = stmt.where(RFQ.user_id == user_id)
    if rfq_ids is not None:
        ids = list(rfq_ids)
        if not ids:
            return 0
        stmt = stmt.where(RFQ.id.in_(ids))
    expired_ids = list((await db.execute(stmt)).scalars().all())
    if expired_ids:
        await db.commit()
        # synchronize_session already set status on loaded rows; refresh the
        # few explicitly targeted ones so server-side updated_at is current.
        if rfq_ids is not None:
            for rfq_id in expired_ids:
                obj = await db.get(RFQ, rfq_id)
                if obj is not None:
                    await db.refresh(obj)
    return len(expired_ids)


def to_out(rfq: RFQ, pending_connections: int = 0, is_saved: bool = False) -> RFQOut:
    """Reassemble the nested wire format from the flat columns."""
    quantity = (
        Quantity.from_stored(rfq.quantity_value, rfq.quantity_unit)
        if rfq.quantity_value is not None
        else None
    )
    minimum_order = (
        Quantity.from_stored(rfq.min_order_value, rfq.min_order_unit)
        if rfq.min_order_value is not None
        else None
    )
    price = (
        Money.from_stored(rfq.price_amount, rfq.price_currency, rfq.price_per_unit)
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
        status=effective_status(rfq),
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
        is_saved=is_saved,
        created_at=rfq.created_at,
        updated_at=rfq.updated_at,
    )


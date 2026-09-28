"""Service for managing saved (bookmarked) RFQs for later access."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import RFQStatus
from models.rfq import RFQ
from models.saved_rfq import SavedRFQ
from models.user import User
from schemas.marketplace import CatalogListOut


class SavedRFQNotFoundError(Exception):
    """Raised when the target RFQ to save cannot be found."""


def _publicly_visible(now: datetime) -> list[Any]:
    """The catalog's visibility rule: active and not past expires_at."""
    return [
        RFQ.status == RFQStatus.ACTIVE,
        or_(RFQ.expires_at.is_(None), RFQ.expires_at > now),
    ]


async def save_rfq(db: AsyncSession, user_id: uuid.UUID, rfq_id: uuid.UUID) -> SavedRFQ:
    """Save / bookmark an RFQ for a user. Idempotent and race-safe.

    Someone else's RFQ can only be bookmarked while it is publicly listed
    (active, unexpired). A draft, closed or expired RFQ is reported as not
    found, so the endpoint is not an existence oracle for private drafts.
    """
    rfq = await db.get(RFQ, rfq_id)
    if not rfq:
        raise SavedRFQNotFoundError(f"RFQ {rfq_id} does not exist")
    if rfq.user_id != user_id:
        now = datetime.now(UTC)
        expires_at = rfq.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if rfq.status != RFQStatus.ACTIVE or (expires_at is not None and expires_at <= now):
            raise SavedRFQNotFoundError(f"RFQ {rfq_id} does not exist")

    # ON CONFLICT DO NOTHING: two simultaneous saves must not race into the
    # unique constraint and surface as a 500.
    await db.execute(
        pg_insert(SavedRFQ)
        .values(id=uuid.uuid4(), user_id=user_id, rfq_id=rfq_id)
        .on_conflict_do_nothing(index_elements=[SavedRFQ.user_id, SavedRFQ.rfq_id])
    )
    await db.commit()
    saved = await db.scalar(
        select(SavedRFQ).where(SavedRFQ.user_id == user_id, SavedRFQ.rfq_id == rfq_id)
    )
    assert saved is not None
    return saved


async def unsave_rfq(db: AsyncSession, user_id: uuid.UUID, rfq_id: uuid.UUID) -> bool:
    """Remove an RFQ from a user's saved list. Returns True if removed, False if wasn't saved."""
    stmt = select(SavedRFQ).where(
        SavedRFQ.user_id == user_id,
        SavedRFQ.rfq_id == rfq_id,
    )
    existing = await db.scalar(stmt)
    if not existing:
        return False

    await db.delete(existing)
    await db.commit()
    return True


async def get_saved_rfq_ids(db: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Return the RFQ IDs the user has saved *that /rfqs/saved would list*.

    Same visibility as the saved list (active, unexpired, not the user's own),
    so the badge count and the list agree. Bookmarks of listings that were
    closed or expired are kept (they reappear if never deleted) but hidden.
    """
    stmt = (
        select(SavedRFQ.rfq_id)
        .join(RFQ, RFQ.id == SavedRFQ.rfq_id)
        .where(
            SavedRFQ.user_id == user_id,
            RFQ.user_id != user_id,
            *_publicly_visible(datetime.now(UTC)),
        )
    )
    rows = await db.scalars(stmt)
    return set(rows.all())


async def list_saved_rfqs(
    db: AsyncSession,
    viewer: User,
    *,
    limit: int = 24,
    offset: int = 0,
) -> CatalogListOut:
    """Retrieve the viewer's saved RFQs with full catalog projection."""
    from services.catalog_service import get_catalog

    return await get_catalog(
        db,
        viewer,
        saved_only=True,
        limit=limit,
        offset=offset,
    )

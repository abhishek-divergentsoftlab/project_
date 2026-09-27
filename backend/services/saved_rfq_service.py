"""Service for managing saved (bookmarked) RFQs for later access."""

import uuid
from typing import Optional, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.rfq import RFQ
from models.saved_rfq import SavedRFQ
from models.user import User
from schemas.marketplace import CatalogListOut


class SavedRFQNotFoundError(Exception):
    """Raised when the target RFQ to save cannot be found."""


async def save_rfq(db: AsyncSession, user_id: uuid.UUID, rfq_id: uuid.UUID) -> SavedRFQ:
    """Save / bookmark an RFQ for a user. Idempotent."""
    # Ensure RFQ exists
    rfq = await db.get(RFQ, rfq_id)
    if not rfq:
        raise SavedRFQNotFoundError(f"RFQ {rfq_id} does not exist")

    # Check if already saved
    stmt = select(SavedRFQ).where(
        SavedRFQ.user_id == user_id,
        SavedRFQ.rfq_id == rfq_id,
    )
    existing = await db.scalar(stmt)
    if existing:
        return existing

    saved = SavedRFQ(user_id=user_id, rfq_id=rfq_id)
    db.add(saved)
    await db.commit()
    await db.refresh(saved)
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
    """Return all RFQ IDs saved by the given user for fast O(1) set membership checks."""
    stmt = select(SavedRFQ.rfq_id).where(SavedRFQ.user_id == user_id)
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

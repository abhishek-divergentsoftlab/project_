"""Who is the buyer and who is the seller on a deal-room connection.

The connection itself only knows sender and receiver. The side of the market
comes from the listing the connection was raised against: the owner of a buyer
RFQ is the buyer, the owner of a seller listing is the seller, and the other
participant takes the opposite role. Escrow, logistics and dispute rules all
depend on this, so it lives in one place.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from models.connection import Connection
from models.enums import RFQRole
from models.rfq import RFQ


async def resolve_buyer_seller(
    db: AsyncSession, conn: Connection, rfq_id: Optional[uuid.UUID] = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Return ``(buyer_id, seller_id)`` for a connection."""
    rfq = await db.get(RFQ, rfq_id or conn.rfq_id)
    if rfq is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Referenced RFQ listing not found")

    owner = rfq.user_id
    other = conn.receiver_id if conn.sender_id == owner else conn.sender_id
    if rfq.role == RFQRole.BUYER:
        return owner, other
    return other, owner

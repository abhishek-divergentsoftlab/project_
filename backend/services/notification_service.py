"""Notification service.

Provides helpers to create, list, and manage notifications.  Notifications are
created on key business events and surfaced in the UI notification bell.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.notification import Notification

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def create_notification(
    db: AsyncSession,
    *,
    user_id: UUID,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
) -> Notification:
    """Insert a single notification row and flush (but not commit)."""
    notif = Notification(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        link=link,
    )
    db.add(notif)
    await db.flush()
    return notif


# ---------------------------------------------------------------------------
# Convenience creators for common events
# ---------------------------------------------------------------------------

async def notify_connection_request(
    db: AsyncSession, *, receiver_id: UUID, sender_name: str, rfq_title: str | None, connection_id: UUID
) -> None:
    """A new connection request was received."""
    body = f"{sender_name} sent you a connection request"
    if rfq_title:
        body += f' on "{rfq_title}"'
    await create_notification(
        db,
        user_id=receiver_id,
        type="connection_request",
        title="New Connection Request",
        body=body,
        link=f"/messages?connection={connection_id}",
    )


async def notify_connection_accepted(
    db: AsyncSession, *, sender_id: UUID, accepter_name: str, connection_id: UUID
) -> None:
    """A connection request was accepted."""
    await create_notification(
        db,
        user_id=sender_id,
        type="connection_accepted",
        title="Connection Accepted",
        body=f"{accepter_name} accepted your connection request",
        link=f"/messages?connection={connection_id}",
    )


async def notify_new_message(
    db: AsyncSession, *, receiver_id: UUID, sender_name: str, connection_id: UUID, preview: str | None = None
) -> None:
    """A new message was received in a connection chat."""
    body = f"New message from {sender_name}"
    if preview:
        body += f': "{preview[:80]}"'
    await create_notification(
        db,
        user_id=receiver_id,
        type="new_message",
        title="New Message",
        body=body,
        link=f"/messages?connection={connection_id}",
    )


async def notify_quotation_received(
    db: AsyncSession, *, receiver_id: UUID, sender_name: str, connection_id: UUID, quote_number: str
) -> None:
    """A new quotation was received."""
    await create_notification(
        db,
        user_id=receiver_id,
        type="quotation_received",
        title="New Quotation",
        body=f"{sender_name} sent quotation #{quote_number}",
        link=f"/messages?connection={connection_id}",
    )


async def notify_quotation_accepted(
    db: AsyncSession, *, receiver_id: UUID, accepter_name: str, connection_id: UUID, quote_number: str
) -> None:
    """A quotation was accepted."""
    await create_notification(
        db,
        user_id=receiver_id,
        type="quotation_accepted",
        title="Quotation Accepted",
        body=f"{accepter_name} accepted quotation #{quote_number}",
        link=f"/messages?connection={connection_id}",
    )


async def notify_connection_rejected(
    db: AsyncSession, *, sender_id: UUID, rejecter_name: str, connection_id: UUID
) -> None:
    """A connection request was declined."""
    await create_notification(
        db,
        user_id=sender_id,
        type="connection_rejected",
        title="Connection Declined",
        body=f"{rejecter_name} declined your connection request",
        link=f"/messages?connection={connection_id}",
    )


async def notify_escrow_funded(
    db: AsyncSession, *, seller_id: UUID, connection_id: UUID, currency: str, amount: Any
) -> None:
    """The buyer deposited the order value into escrow."""
    await create_notification(
        db,
        user_id=seller_id,
        type="escrow_funded",
        title="Escrow Funded",
        body=f"The buyer deposited {currency} {amount:,.2f} into escrow for your order",
        link=f"/messages?connection={connection_id}",
    )


async def notify_milestone_release_requested(
    db: AsyncSession, *, buyer_id: UUID, connection_id: UUID, milestone_title: str
) -> None:
    """The seller asked the buyer to release a milestone."""
    await create_notification(
        db,
        user_id=buyer_id,
        type="escrow_release_requested",
        title="Milestone Release Requested",
        body=f"The supplier requested release of '{milestone_title}'",
        link=f"/messages?connection={connection_id}",
    )


async def notify_escrow_released(
    db: AsyncSession, *, seller_id: UUID, connection_id: UUID, currency: str, amount: Any, milestone_title: str
) -> None:
    """The buyer released a milestone payment to the seller."""
    await create_notification(
        db,
        user_id=seller_id,
        type="escrow_released",
        title="Escrow Payment Released",
        body=f"{currency} {amount:,.2f} released for '{milestone_title}'",
        link=f"/messages?connection={connection_id}",
    )


async def notify_dispute_raised(
    db: AsyncSession, *, user_id: UUID, connection_id: UUID, dispute_title: str
) -> None:
    """A formal dispute was opened against this user's deal."""
    await create_notification(
        db,
        user_id=user_id,
        type="dispute_raised",
        title="Formal Dispute Opened",
        body=f"A dispute was opened: '{dispute_title}'. Escrow releases are frozen.",
        link=f"/messages?connection={connection_id}",
    )


async def notify_dispute_resolved(
    db: AsyncSession, *, user_id: UUID, connection_id: UUID, dispute_title: str, resolution: str
) -> None:
    """A dispute on this user's deal was resolved."""
    await create_notification(
        db,
        user_id=user_id,
        type="dispute_resolved",
        title="Dispute Resolved",
        body=f"Dispute '{dispute_title}' resolved: {resolution.replace('_', ' ')}",
        link=f"/messages?connection={connection_id}",
    )


async def notify_shipment_dispatched(
    db: AsyncSession, *, buyer_id: UUID, connection_id: UUID, carrier_name: str, tracking_number: str
) -> None:
    """The seller dispatched the order."""
    await create_notification(
        db,
        user_id=buyer_id,
        type="shipment_dispatched",
        title="Order Dispatched",
        body=f"Your order was dispatched with {carrier_name} (tracking {tracking_number})",
        link=f"/messages?connection={connection_id}",
    )


async def notify_shipment_delivered(
    db: AsyncSession, *, seller_id: UUID, connection_id: UUID, tracking_number: str
) -> None:
    """The buyer confirmed delivery of the shipment."""
    await create_notification(
        db,
        user_id=seller_id,
        type="shipment_delivered",
        title="Delivery Confirmed",
        body=f"The buyer confirmed delivery of shipment {tracking_number}",
        link=f"/messages?connection={connection_id}",
    )


async def notify_safely(db: AsyncSession, notifier: Any, /, **kwargs: Any) -> None:
    """Run one ``notify_*`` helper and commit, never failing the caller.

    Notifications are a side channel: the business event has already been
    committed, so a failure here is logged and rolled back rather than turned
    into an error response.
    """
    try:
        await notifier(db, **kwargs)
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Notification %s failed (non-fatal)", getattr(notifier, "__name__", notifier), exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

async def list_notifications(
    db: AsyncSession, user_id: UUID, *, limit: int = 30, offset: int = 0, unread_only: bool = False
) -> tuple[list[dict[str, Any]], int]:
    """Return notifications for *user_id*, newest first."""
    base = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        base = base.where(Notification.is_read == False)  # noqa: E712

    total = await db.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0

    rows = await db.execute(
        base.order_by(desc(Notification.created_at)).limit(limit).offset(offset)
    )
    items = [
        {
            "id": str(n.id),
            "type": n.type,
            "title": n.title,
            "body": n.body,
            "link": n.link,
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in rows.scalars()
    ]
    return items, total


async def unread_count(db: AsyncSession, user_id: UUID) -> int:
    """Return the number of unread notifications."""
    return await db.scalar(
        select(func.count()).where(
            Notification.user_id == user_id,
            Notification.is_read == False,  # noqa: E712
        )
    ) or 0


async def mark_read(db: AsyncSession, user_id: UUID, notification_id: UUID) -> bool:
    """Mark a single notification as read.  Returns True if found."""
    result = await db.execute(
        update(Notification)
        .where(Notification.id == notification_id, Notification.user_id == user_id)
        .values(is_read=True)
    )
    await db.commit()
    return result.rowcount > 0


async def mark_all_read(db: AsyncSession, user_id: UUID) -> int:
    """Mark all of a user's notifications as read.  Returns count affected."""
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.is_read == False)  # noqa: E712
        .values(is_read=True)
    )
    await db.commit()
    return result.rowcount

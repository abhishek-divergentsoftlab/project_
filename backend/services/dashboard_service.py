"""Dashboard analytics service.

Aggregates business metrics for the authenticated user: RFQ counts, connection
stats, quotation pipeline state, and a chronological activity feed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select, or_, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from models.connection import Connection, ConnectionMessage
from models.enums import ConnectionStatus, QuotationStatus, RFQStatus
from models.quotation import Quotation
from models.rfq import RFQ
from models.user import UserProfile

logger = logging.getLogger(__name__)


async def get_dashboard_stats(db: AsyncSession, user_id: UUID) -> dict[str, Any]:
    """Return aggregated business metrics for *user_id*."""

    # --- RFQ counts by status ---
    rfq_counts = await db.execute(
        select(
            func.count().label("total"),
            func.count().filter(RFQ.status == RFQStatus.ACTIVE).label("active"),
            func.count().filter(RFQ.status == RFQStatus.DRAFT).label("draft"),
            func.count().filter(RFQ.status == RFQStatus.CLOSED).label("closed"),
            func.count().filter(RFQ.status == RFQStatus.EXPIRED).label("expired"),
        ).where(RFQ.user_id == user_id)
    )
    rfq = rfq_counts.one()

    # --- Connection counts ---
    conn_counts = await db.execute(
        select(
            func.count().label("total"),
            func.count().filter(Connection.status == ConnectionStatus.PENDING).label("pending"),
            func.count().filter(Connection.status == ConnectionStatus.ACCEPTED).label("accepted"),
            func.count().filter(Connection.status == ConnectionStatus.REJECTED).label("rejected"),
        ).where(
            or_(Connection.sender_id == user_id, Connection.receiver_id == user_id)
        )
    )
    conn = conn_counts.one()

    # Pending received (action required by this user)
    pending_received = await db.scalar(
        select(func.count()).where(
            Connection.receiver_id == user_id,
            Connection.status == ConnectionStatus.PENDING,
        )
    ) or 0

    # --- Quotation pipeline ---
    quote_counts = await db.execute(
        select(
            func.count().label("total"),
            func.count().filter(Quotation.status == QuotationStatus.PENDING).label("pending"),
            func.count().filter(Quotation.status == QuotationStatus.ACCEPTED).label("accepted"),
            func.count().filter(Quotation.status.in_([
                QuotationStatus.DISPATCHED,
                QuotationStatus.DELIVERED,
                QuotationStatus.RECEIVED,
            ])).label("in_transit"),
            func.count().filter(Quotation.status == QuotationStatus.COMPLETED).label("completed"),
        ).where(
            or_(Quotation.sender_id == user_id, Quotation.receiver_id == user_id)
        )
    )
    quote = quote_counts.one()

    # --- Message count ---
    msg_count = await db.scalar(
        select(func.count()).select_from(ConnectionMessage).where(
            ConnectionMessage.sender_id == user_id
        )
    ) or 0

    # --- Profile completeness ---
    profile = await db.scalar(
        select(UserProfile).where(UserProfile.user_id == user_id)
    )
    profile_fields = ["name", "company_name", "phone", "address", "city", "country"]
    filled = sum(1 for f in profile_fields if profile and getattr(profile, f, None)) if profile else 0
    profile_completeness = round(filled / len(profile_fields) * 100)

    return {
        "rfqs": {
            "total": rfq.total,
            "active": rfq.active,
            "draft": rfq.draft,
            "closed": rfq.closed,
            "expired": rfq.expired,
        },
        "connections": {
            "total": conn.total,
            "pending": conn.pending,
            "accepted": conn.accepted,
            "rejected": conn.rejected,
            "pending_received": pending_received,
        },
        "quotations": {
            "total": quote.total,
            "pending": quote.pending,
            "accepted": quote.accepted,
            "in_transit": quote.in_transit,
            "completed": quote.completed,
        },
        "messages_sent": msg_count,
        "profile_completeness": profile_completeness,
        "trust_score": profile.trust_score if profile else 0,
        "average_rating": float(profile.average_rating) if profile and profile.average_rating else None,
        "total_reviews": profile.total_reviews if profile else 0,
    }


async def get_activity_feed(
    db: AsyncSession, user_id: UUID, limit: int = 20
) -> list[dict[str, Any]]:
    """Return a chronological activity feed for the dashboard."""
    activities: list[dict[str, Any]] = []

    # Recent connections (last 20)
    conn_result = await db.execute(
        select(Connection)
        .where(or_(Connection.sender_id == user_id, Connection.receiver_id == user_id))
        .order_by(desc(Connection.updated_at))
        .limit(limit)
    )
    for conn in conn_result.scalars():
        direction = "sent" if conn.sender_id == user_id else "received"
        if conn.status == ConnectionStatus.PENDING:
            if direction == "received":
                title = "New connection request"
                body = "You have a new connection request to review"
                icon = "connection_received"
            else:
                title = "Connection request sent"
                body = "Your connection request is pending"
                icon = "connection_sent"
        elif conn.status == ConnectionStatus.ACCEPTED:
            title = "Connection accepted"
            body = "A connection has been accepted — you can now message"
            icon = "connection_accepted"
        else:
            title = "Connection declined"
            body = "A connection request was declined"
            icon = "connection_rejected"

        activities.append({
            "id": str(conn.id),
            "type": "connection",
            "icon": icon,
            "title": title,
            "body": body,
            "link": f"/messages?connection={conn.id}",
            "timestamp": conn.updated_at.isoformat() if conn.updated_at else conn.created_at.isoformat(),
        })

    # Recent quotations
    quote_result = await db.execute(
        select(Quotation)
        .where(or_(Quotation.sender_id == user_id, Quotation.receiver_id == user_id))
        .order_by(desc(Quotation.updated_at))
        .limit(limit)
    )
    for q in quote_result.scalars():
        is_sender = q.sender_id == user_id
        status_label = q.status.value if hasattr(q.status, "value") else str(q.status)
        if status_label == "pending":
            title = "Quotation sent" if is_sender else "New quotation received"
            icon = "quote_new"
        elif status_label == "accepted":
            title = "Quotation accepted"
            icon = "quote_accepted"
        elif status_label == "dispatched":
            title = "Order dispatched"
            icon = "quote_dispatched"
        elif status_label == "completed":
            title = "Deal completed"
            icon = "quote_completed"
        else:
            title = f"Quotation {status_label}"
            icon = "quote_update"

        activities.append({
            "id": str(q.id),
            "type": "quotation",
            "icon": icon,
            "title": title,
            "body": f"Quote #{q.quote_number} — {q.quantity} {q.quantity_unit} at {q.currency} {q.unit_price}",
            "link": f"/messages?connection={q.connection_id}",
            "timestamp": q.updated_at.isoformat() if q.updated_at else q.created_at.isoformat(),
        })

    # Sort by timestamp descending
    activities.sort(key=lambda a: a["timestamp"], reverse=True)
    return activities[:limit]

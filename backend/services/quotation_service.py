from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.connection import Connection
from models.enums import ConnectionStatus, QuotationStatus, RFQRole, RFQStatus
from models.quotation import Quotation
from models.rfq import RFQ
from models.user import User
from schemas.quotation import QuotationCreate, QuotationOut
from services.websocket_manager import ws_manager

logger = logging.getLogger(__name__)

# A quote in any of these states is a live order: while one exists on a
# connection, no other quote may be created or accepted there (one PO per deal).
ACTIVE_ORDER_STATUSES = (
    QuotationStatus.ACCEPTED,
    QuotationStatus.DISPATCHED,
    QuotationStatus.DELIVERED,
)


async def _get_accepted_connection(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> Connection:
    if lock:
        # Serializes quote creation/acceptance on one connection, so two
        # concurrent requests cannot both pass the "no live order" check.
        conn = (
            await db.execute(
                select(Connection)
                .where(Connection.id == connection_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().first()
    else:
        conn = await db.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    if user_id not in (conn.sender_id, conn.receiver_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized")

    if conn.status is not ConnectionStatus.ACCEPTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Quotation and deal negotiation requires an accepted connection",
        )
    return conn


async def _ensure_no_live_order(
    db: AsyncSession, connection_id: uuid.UUID, exclude_id: Optional[uuid.UUID] = None
) -> None:
    stmt = select(Quotation.quote_number, Quotation.status).where(
        Quotation.connection_id == connection_id,
        Quotation.status.in_(ACTIVE_ORDER_STATUSES),
    )
    if exclude_id is not None:
        stmt = stmt.where(Quotation.id != exclude_id)
    live = (await db.execute(stmt.limit(1))).first()
    if live is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Quotation {live.quote_number} is already {live.status.value} on this deal; "
            "complete or close that order before issuing another",
        )


async def _ensure_rfq_open(db: AsyncSession, rfq_id: uuid.UUID) -> RFQ:
    rfq = await db.get(RFQ, rfq_id)
    if rfq is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFQ not found")
    expired = rfq.expires_at is not None and rfq.expires_at < datetime.now(UTC)
    if rfq.status is not RFQStatus.ACTIVE or expired:
        state = "expired" if expired else rfq.status.value
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This listing is {state}; quotations can no longer be issued on it",
        )
    return rfq


def to_out(quote: Quotation, viewer_id: uuid.UUID) -> QuotationOut:
    return QuotationOut(
        id=quote.id,
        connection_id=quote.connection_id,
        sender_id=quote.sender_id,
        receiver_id=quote.receiver_id,
        rfq_id=quote.rfq_id,
        quote_number=quote.quote_number,
        version=quote.version,
        status=quote.status,
        unit_price=quote.unit_price,
        currency=quote.currency,
        quantity=quote.quantity,
        quantity_unit=quote.quantity_unit,
        total_amount=quote.total_amount,
        lead_time_days=quote.lead_time_days,
        incoterms=quote.incoterms,
        payment_terms=quote.payment_terms,
        valid_until=quote.valid_until,
        notes=quote.notes,
        purchase_order_reference=quote.purchase_order_reference,
        created_at=quote.created_at,
        updated_at=quote.updated_at,
        is_sender=(quote.sender_id == viewer_id),
    )


async def create_quotation(
    db: AsyncSession,
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: QuotationCreate,
) -> Quotation:
    conn = await _get_accepted_connection(db, connection_id, user_id, lock=True)
    await _ensure_rfq_open(db, conn.rfq_id)
    await _ensure_no_live_order(db, connection_id)

    receiver_id = conn.receiver_id if conn.sender_id == user_id else conn.sender_id

    # Existing quotes for this connection to determine version and archive pending quotes
    existing_quotes = (
        await db.scalars(
            select(Quotation)
            .where(Quotation.connection_id == connection_id)
            .order_by(Quotation.version.desc())
        )
    ).all()

    # Supersede any currently pending quote as COUNTERED
    for q in existing_quotes:
        if q.status is QuotationStatus.PENDING:
            q.status = QuotationStatus.COUNTERED

    next_version = (existing_quotes[0].version + 1) if existing_quotes else 1
    quote_number = f"QT-{str(connection_id)[:8].upper()}-v{next_version}"

    valid_days = payload.valid_days or 14
    valid_until = datetime.now(UTC) + timedelta(days=valid_days)
    total_amount = (payload.unit_price * payload.quantity).quantize(Decimal("0.0001"))

    quote = Quotation(
        connection_id=connection_id,
        sender_id=user_id,
        receiver_id=receiver_id,
        rfq_id=conn.rfq_id,
        quote_number=quote_number,
        version=next_version,
        status=QuotationStatus.PENDING,
        unit_price=payload.unit_price,
        currency=payload.currency,
        quantity=payload.quantity,
        quantity_unit=payload.quantity_unit,
        total_amount=total_amount,
        lead_time_days=payload.lead_time_days,
        incoterms=payload.incoterms,
        payment_terms=payload.payment_terms,
        valid_until=valid_until,
        notes=payload.notes,
    )

    db.add(quote)
    await db.commit()
    await db.refresh(quote)

    # Real-time event push to deal room
    out = to_out(quote, user_id)
    await ws_manager.broadcast(
        connection_id,
        "quote_created",
        out.model_dump(mode="json"),
    )

    try:
        from models.user import User
        from services import notification_service
        sender_user = await db.get(User, user_id)
        sender_profile = getattr(sender_user, "profile", None) if sender_user else None
        sender_name = (
            (getattr(sender_profile, "company_name", None) or getattr(sender_profile, "name", None))
            if sender_profile else "Someone"
        ) or "Someone"
        await notification_service.notify_quotation_received(
            db,
            receiver_id=receiver_id,
            sender_name=sender_name,
            connection_id=connection_id,
            quote_number=quote_number,
        )
        await db.commit()
    except Exception:
        logger.warning("Quotation notification failed (non-fatal)", exc_info=True)
        await db.rollback()

    return quote


async def list_quotations(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> Sequence[Quotation]:
    await _get_accepted_connection(db, connection_id, user_id)

    quotes = (
        await db.scalars(
            select(Quotation)
            .where(Quotation.connection_id == connection_id)
            .order_by(Quotation.created_at.desc())
        )
    ).all()
    return quotes


async def accept_quotation(
    db: AsyncSession, connection_id: uuid.UUID, quote_id: uuid.UUID, user_id: uuid.UUID
) -> Quotation:
    await _get_accepted_connection(db, connection_id, user_id, lock=True)

    quote = await db.get(Quotation, quote_id, populate_existing=True)
    if quote is None or quote.connection_id != connection_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quotation not found")

    if quote.receiver_id != user_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You cannot accept your own quotation"
        )

    await _ensure_no_live_order(db, connection_id, exclude_id=quote.id)

    if quote.status is not QuotationStatus.PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot accept quotation in {quote.status.value} status",
        )

    now = datetime.now(UTC)
    if quote.valid_until and quote.valid_until < now:
        quote.status = QuotationStatus.EXPIRED
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, "This quotation has expired")

    quote.status = QuotationStatus.ACCEPTED
    po_ref = f"PO-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    quote.purchase_order_reference = po_ref

    await db.commit()
    await db.refresh(quote)

    out = to_out(quote, user_id)
    await ws_manager.broadcast(
        connection_id,
        "quote_accepted",
        out.model_dump(mode="json"),
    )

    try:
        from models.user import User
        from services import notification_service
        accepter_user = await db.get(User, user_id)
        accepter_profile = getattr(accepter_user, "profile", None) if accepter_user else None
        accepter_name = (
            (getattr(accepter_profile, "company_name", None) or getattr(accepter_profile, "name", None))
            if accepter_profile else "Someone"
        ) or "Someone"
        await notification_service.notify_quotation_accepted(
            db,
            receiver_id=quote.sender_id,
            accepter_name=accepter_name,
            connection_id=connection_id,
            quote_number=quote.quote_number,
        )
        await db.commit()
    except Exception:
        logger.warning("Quotation-accepted notification failed (non-fatal)", exc_info=True)
        await db.rollback()

    return quote


async def reject_quotation(
    db: AsyncSession, connection_id: uuid.UUID, quote_id: uuid.UUID, user_id: uuid.UUID, reason: str | None = None
) -> Quotation:
    await _get_accepted_connection(db, connection_id, user_id)

    quote = await db.get(Quotation, quote_id)
    if quote is None or quote.connection_id != connection_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quotation not found")

    if quote.receiver_id != user_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You cannot decline your own quotation"
        )

    if quote.status is not QuotationStatus.PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot decline quotation in {quote.status.value} status",
        )

    quote.status = QuotationStatus.REJECTED
    if reason:
        quote.notes = f"{quote.notes or ''}\n[Decline Reason]: {reason}".strip()

    await db.commit()
    await db.refresh(quote)

    out = to_out(quote, user_id)
    await ws_manager.broadcast(
        connection_id,
        "quote_rejected",
        out.model_dump(mode="json"),
    )

    return quote


async def get_quote_documents_context(
    db: AsyncSession, connection_id: uuid.UUID, quote_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[Quotation, User, User, Optional[RFQ]]:
    """Retrieve full context for PDF document generation with authorization checks."""
    conn = await _get_accepted_connection(db, connection_id, user_id)

    quote = await db.get(Quotation, quote_id)
    if quote is None or quote.connection_id != connection_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quotation not found")

    allowed_statuses = {
        QuotationStatus.ACCEPTED,
        QuotationStatus.DISPATCHED,
        QuotationStatus.DELIVERED,
        QuotationStatus.RECEIVED,
        QuotationStatus.COMPLETED,
    }
    if quote.status not in allowed_statuses:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Documents can only be generated for accepted quotations. Current status: {quote.status.value}",
        )

    # Fetch users with profile
    user_query = (
        select(User)
        .options(selectinload(User.profile))
        .where(User.id.in_([conn.sender_id, conn.receiver_id]))
    )

    users_list = (await db.scalars(user_query)).all()
    user_map = {u.id: u for u in users_list}

    sender_user = user_map.get(conn.sender_id)
    receiver_user = user_map.get(conn.receiver_id)

    # Fetch RFQ
    rfq = await db.get(RFQ, quote.rfq_id)

    # Determine buyer and seller
    if rfq and rfq.role == RFQRole.SELLER:
        seller = user_map.get(rfq.user_id, sender_user)
        buyer = receiver_user if seller and seller.id == sender_user.id else sender_user
    else:  # rfq.role == RFQRole.BUYER or default
        buyer = user_map.get(getattr(rfq, "user_id", None), receiver_user)
        seller = sender_user if buyer and buyer.id == receiver_user.id else receiver_user

    return quote, buyer, seller, rfq


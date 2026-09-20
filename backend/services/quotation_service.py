"""B2B Deal Room service: structured quotations, counter-offers, and Purchase Orders."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.connection import Connection
from models.enums import ConnectionStatus, QuotationStatus
from models.quotation import Quotation
from schemas.quotation import QuotationCreate, QuotationOut
from services import currency
from services.websocket_manager import ws_manager


async def _get_accepted_connection(
    db: AsyncSession, connection_id: uuid.UUID, user_id: uuid.UUID
) -> Connection:
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
    conn = await _get_accepted_connection(db, connection_id, user_id)

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
    total_amount = Decimal(str(round(payload.unit_price * payload.quantity, 4)))

    quote = Quotation(
        connection_id=connection_id,
        sender_id=user_id,
        receiver_id=receiver_id,
        rfq_id=conn.rfq_id,
        quote_number=quote_number,
        version=next_version,
        status=QuotationStatus.PENDING,
        unit_price=payload.unit_price,
        currency=currency.normalize_currency(payload.currency) or payload.currency.upper(),
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
    await _get_accepted_connection(db, connection_id, user_id)

    quote = await db.get(Quotation, quote_id)
    if quote is None or quote.connection_id != connection_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quotation not found")

    if quote.receiver_id != user_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You cannot accept your own quotation"
        )

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

"""B2B Deal Room endpoints: formal quotations, counter-offers, and Purchase Orders."""

import uuid
from typing import Optional

from fastapi import APIRouter, status

from api.deps import CurrentUser, DbSession
from schemas.quotation import QuotationAction, QuotationCreate, QuotationOut
from services import quotation_service

router = APIRouter(prefix="/connections/{connection_id}/quotes", tags=["quotations"])


@router.post("", response_model=QuotationOut, status_code=status.HTTP_201_CREATED)
async def create_quote(
    connection_id: uuid.UUID,
    payload: QuotationCreate,
    current_user: CurrentUser,
    db: DbSession,
) -> QuotationOut:
    """Issue a formal quotation or counter-offer for an active connection."""
    quote = await quotation_service.create_quotation(
        db, connection_id, current_user.id, payload
    )
    return quotation_service.to_out(quote, current_user.id)


@router.get("", response_model=list[QuotationOut])
async def list_quotes(
    connection_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> list[QuotationOut]:
    """Retrieve full chronological quote and counter-offer negotiation history."""
    quotes = await quotation_service.list_quotations(db, connection_id, current_user.id)
    return [quotation_service.to_out(q, current_user.id) for q in quotes]


@router.post("/{quote_id}/accept", response_model=QuotationOut)
async def accept_quote(
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> QuotationOut:
    """Accept the received quotation. Generates a confirmed Purchase Order (PO)."""
    quote = await quotation_service.accept_quotation(
        db, connection_id, quote_id, current_user.id
    )
    return quotation_service.to_out(quote, current_user.id)


@router.post("/{quote_id}/reject", response_model=QuotationOut)
async def reject_quote(
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    payload: Optional[QuotationAction],
    current_user: CurrentUser,
    db: DbSession,
) -> QuotationOut:
    """Decline the received quotation."""
    reason = payload.reason if payload else None
    quote = await quotation_service.reject_quotation(
        db, connection_id, quote_id, current_user.id, reason=reason
    )
    return quotation_service.to_out(quote, current_user.id)

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


@router.get("/{quote_id}/po-pdf")
async def download_purchase_order_pdf(
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
):
    """Generate and download official B2B Purchase Order (PO) PDF."""
    from fastapi import Response
    from services import document_generator

    quote, buyer, seller, rfq = await quotation_service.get_quote_documents_context(
        db, connection_id, quote_id, current_user.id
    )
    pdf_bytes = document_generator.generate_purchase_order_pdf(
        quotation=quote, buyer=buyer, seller=seller, rfq=rfq
    )
    po_ref = quote.purchase_order_reference or f"PO-{str(quote.id)[:8].upper()}"
    filename = f"Purchase_Order_{po_ref}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{quote_id}/invoice-pdf")
async def download_commercial_invoice_pdf(
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
):
    """Generate and download official B2B Commercial Tax Invoice PDF."""
    from fastapi import Response
    from services import document_generator

    quote, buyer, seller, rfq = await quotation_service.get_quote_documents_context(
        db, connection_id, quote_id, current_user.id
    )
    pdf_bytes = document_generator.generate_commercial_invoice_pdf(
        quotation=quote, buyer=buyer, seller=seller, rfq=rfq
    )
    filename = f"Commercial_Invoice_{quote.quote_number}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


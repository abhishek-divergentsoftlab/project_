"""API router for multi-modal logistics, freight estimation, and carrier shipment dispatch."""

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import CurrentUser, DbSession
from models.quotation import Quotation
from models.shipment import Shipment
from models.user import User
from schemas.logistics import (
    FreightEstimateRequest,
    FreightEstimateResponse,
    ShipmentCreatePayload,
    ShipmentOut,
    ShipmentStatusUpdatePayload,
)
from services import document_generator, logistics_service

router = APIRouter(prefix="/logistics", tags=["logistics"])


@router.post(
    "/estimate",
    response_model=FreightEstimateResponse,
    summary="Estimate multi-modal freight rates and Incoterms landed cost breakdown",
)
async def estimate_freight(payload: FreightEstimateRequest) -> FreightEstimateResponse:
    """Calculate multi-modal rates (Road, Ocean, Air, Courier) and Incoterms cost allocation."""
    return logistics_service.estimate_freight_rates(payload)


@router.get(
    "/connections/{connection_id}/shipment",
    response_model=Optional[ShipmentOut],
    summary="Get active shipment record for Deal Room connection",
)
async def get_connection_shipment(
    connection_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> Optional[ShipmentOut]:
    """Retrieve active shipment, carrier tracking, and chronological milestones."""
    shipment = await logistics_service.get_or_create_connection_shipment(
        db, connection_id, current_user.id
    )
    if shipment is None:
        return None
    return ShipmentOut.model_validate(shipment)


@router.post(
    "/connections/{connection_id}/dispatch",
    response_model=ShipmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record order dispatch with carrier tracking details",
)
async def dispatch_order(
    connection_id: uuid.UUID,
    payload: ShipmentCreatePayload,
    current_user: CurrentUser,
    db: DbSession,
) -> ShipmentOut:
    """Create shipment record, record dispatch checkpoint, and optionally request escrow milestone release."""
    shipment = await logistics_service.create_shipment_dispatch(
        db, connection_id, current_user.id, payload
    )
    return ShipmentOut.model_validate(shipment)


@router.post(
    "/shipments/{shipment_id}/events",
    response_model=ShipmentOut,
    summary="Append tracking event and update shipment checkpoint status",
)
async def update_shipment_status(
    shipment_id: uuid.UUID,
    payload: ShipmentStatusUpdatePayload,
    current_user: CurrentUser,
    db: DbSession,
) -> ShipmentOut:
    """Append tracking checkpoint (e.g. in_transit, customs_cleared, delivered)."""
    shipment = await logistics_service.append_shipment_event(
        db, shipment_id, current_user.id, payload
    )
    return ShipmentOut.model_validate(shipment)


@router.get(
    "/shipments/{shipment_id}/waybill.pdf",
    summary="Download official vector PDF Consignment Note / Air Waybill",
)
async def download_waybill_pdf(
    shipment_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> Response:
    """Generate and return official vector PDF Waybill with SHA-256 seal."""
    shipment = await db.get(Shipment, shipment_id)
    if shipment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shipment not found")

    if current_user.id not in (shipment.sender_id, shipment.receiver_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized to download this waybill")

    sender = await db.get(User, shipment.sender_id, options=[selectinload(User.profile)])
    receiver = await db.get(User, shipment.receiver_id, options=[selectinload(User.profile)])
    if not sender or not receiver:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Parties not found")

    quotation = None
    if shipment.quotation_id:
        quotation = await db.get(Quotation, shipment.quotation_id)

    pdf_bytes = document_generator.generate_waybill_pdf(shipment, sender, receiver, quotation)
    filename = f"Waybill_{shipment.tracking_number}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Waybill-Tracking": shipment.tracking_number,
        },
    )

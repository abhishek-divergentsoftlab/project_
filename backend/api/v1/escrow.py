"""API endpoints for Milestone Escrow payments, fund releases, and formal deal disputes."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status

from api.deps import CurrentUser, DbSession
from schemas.escrow import (
    DealDisputeCreatePayload,
    DealDisputeOut,
    DealDisputeResolvePayload,
    EscrowAccountOut,
    EscrowDepositPayload,
    MilestoneReleaseApprovePayload,
    MilestoneReleaseRequestPayload,
)
from services import escrow_service

router = APIRouter(prefix="/connections", tags=["escrow"])


@router.get("/{connection_id}/escrow", response_model=EscrowAccountOut)
async def get_connection_escrow(
    connection_id: Annotated[uuid.UUID, Path(description="Connection UUID")],
    current_user: CurrentUser,
    db: DbSession,
) -> EscrowAccountOut:
    """Retrieve the active escrow vault, milestone breakdown, and balances for a deal room."""
    escrow = await escrow_service.get_or_create_escrow_for_connection(
        db=db,
        connection_id=connection_id,
        user_id=current_user.id,
    )
    return EscrowAccountOut.model_validate(escrow)


@router.post("/{connection_id}/escrow/fund", response_model=EscrowAccountOut)
async def fund_connection_escrow(
    connection_id: Annotated[uuid.UUID, Path(description="Connection UUID")],
    payload: EscrowDepositPayload,
    current_user: CurrentUser,
    db: DbSession,
) -> EscrowAccountOut:
    """Buyer deposits funds into the platform Escrow Vault, securing the quotation order."""
    escrow = await escrow_service.fund_escrow(
        db=db,
        connection_id=connection_id,
        user_id=current_user.id,
        payload=payload,
    )
    return EscrowAccountOut.model_validate(escrow)


@router.post(
    "/{connection_id}/escrow/milestones/{milestone_id}/request-release",
    response_model=EscrowAccountOut,
)
async def request_milestone_payout(
    connection_id: Annotated[uuid.UUID, Path(description="Connection UUID")],
    milestone_id: Annotated[uuid.UUID, Path(description="Milestone UUID")],
    payload: MilestoneReleaseRequestPayload,
    current_user: CurrentUser,
    db: DbSession,
) -> EscrowAccountOut:
    """Supplier requests milestone payment release upon providing delivery/dispatch proof."""
    escrow = await escrow_service.request_milestone_release(
        db=db,
        connection_id=connection_id,
        milestone_id=milestone_id,
        user_id=current_user.id,
        payload=payload,
    )
    return EscrowAccountOut.model_validate(escrow)


@router.post(
    "/{connection_id}/escrow/milestones/{milestone_id}/release",
    response_model=EscrowAccountOut,
)
async def release_milestone_funds(
    connection_id: Annotated[uuid.UUID, Path(description="Connection UUID")],
    milestone_id: Annotated[uuid.UUID, Path(description="Milestone UUID")],
    payload: MilestoneReleaseApprovePayload,
    current_user: CurrentUser,
    db: DbSession,
) -> EscrowAccountOut:
    """Buyer approves and releases escrow milestone funds to the supplier."""
    escrow = await escrow_service.release_milestone_funds(
        db=db,
        connection_id=connection_id,
        milestone_id=milestone_id,
        user_id=current_user.id,
        payload=payload,
    )
    return EscrowAccountOut.model_validate(escrow)


@router.post(
    "/{connection_id}/disputes",
    response_model=DealDisputeOut,
    status_code=status.HTTP_201_CREATED,
)
async def open_connection_dispute(
    connection_id: Annotated[uuid.UUID, Path(description="Connection UUID")],
    payload: DealDisputeCreatePayload,
    current_user: CurrentUser,
    db: DbSession,
) -> DealDisputeOut:
    """Raise a formal dispute: immediately freezes escrow fund releases and alerts counterparties."""
    dispute = await escrow_service.raise_dispute(
        db=db,
        connection_id=connection_id,
        user_id=current_user.id,
        payload=payload,
    )
    return DealDisputeOut.model_validate(dispute)


@router.post(
    "/{connection_id}/disputes/{dispute_id}/resolve",
    response_model=DealDisputeOut,
)
async def resolve_connection_dispute(
    connection_id: Annotated[uuid.UUID, Path(description="Connection UUID")],
    dispute_id: Annotated[uuid.UUID, Path(description="Dispute UUID")],
    payload: DealDisputeResolvePayload,
    current_user: CurrentUser,
    db: DbSession,
) -> DealDisputeOut:
    """Resolve an open dispute with mutual agreement, escrow release, or buyer refund."""
    dispute = await escrow_service.resolve_dispute(
        db=db,
        connection_id=connection_id,
        dispute_id=dispute_id,
        user_id=current_user.id,
        payload=payload,
    )
    return DealDisputeOut.model_validate(dispute)

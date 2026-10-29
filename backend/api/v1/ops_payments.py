"""Operator confirmation of escrow deposits.

Every route here depends on OperatorUser (see api/deps.py), so only accounts
listed in settings.OPERATOR_EMAILS can reach it (anyone else gets a 404).

A buyer's deposit is only a report; these routes are what make a vault
``funded`` (or send it back to ``pending_deposit``).
"""

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Path

from api.deps import DbSession, OperatorUser
from schemas.escrow import EscrowAccountOut, EscrowDepositConfirmPayload, EscrowDepositRejectPayload
from services import escrow_service

router = APIRouter(prefix="/ops/payments", tags=["ops"])


@router.get("/escrow/reported", response_model=list[EscrowAccountOut])
async def list_reported_deposits(_operator: OperatorUser, db: DbSession) -> list[EscrowAccountOut]:
    """Vaults whose buyer reported a deposit that is waiting for confirmation."""
    rows = await escrow_service.list_escrows_by_status(db, "deposit_reported")
    return [EscrowAccountOut.model_validate(r) for r in rows]


@router.post("/escrow/{escrow_id}/confirm-deposit", response_model=EscrowAccountOut)
async def confirm_deposit(
    escrow_id: Annotated[uuid.UUID, Path(description="Escrow vault UUID")],
    _operator: OperatorUser,
    db: DbSession,
    payload: Optional[EscrowDepositConfirmPayload] = None,
) -> EscrowAccountOut:
    """The reported payment arrived: deposit_reported -> funded."""
    escrow = await escrow_service.confirm_deposit(db, escrow_id)
    return EscrowAccountOut.model_validate(escrow)


@router.post("/escrow/{escrow_id}/reject-deposit", response_model=EscrowAccountOut)
async def reject_deposit(
    escrow_id: Annotated[uuid.UUID, Path(description="Escrow vault UUID")],
    payload: EscrowDepositRejectPayload,
    _operator: OperatorUser,
    db: DbSession,
) -> EscrowAccountOut:
    """The reported payment was not found: deposit_reported -> pending_deposit."""
    escrow = await escrow_service.reject_deposit(db, escrow_id, payload.reason)
    return EscrowAccountOut.model_validate(escrow)

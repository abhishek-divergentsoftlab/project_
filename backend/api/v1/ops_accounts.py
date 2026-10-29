"""Operator actions on accounts (suspend, reinstate).

Every route here depends on OperatorUser (see api/deps.py), so only accounts
listed in settings.OPERATOR_EMAILS can reach it.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from api.deps import DbSession, OperatorUser
from core.config import settings
from models.enums import UserStatus
from services import auth_service

router = APIRouter(prefix="/ops/accounts", tags=["ops"])


class AccountStatusOut(BaseModel):
    id: uuid.UUID
    email: str
    status: UserStatus


async def _load(db: DbSession, user_id: uuid.UUID):
    target = await auth_service.get_user(db, user_id)
    if target is None or target.status is UserStatus.DELETED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return target


@router.post("/{user_id}/suspend", response_model=AccountStatusOut)
async def suspend_account(user_id: uuid.UUID, operator: OperatorUser, db: DbSession) -> AccountStatusOut:
    """Block the account from logging in and kill every token it holds (token_version bump)."""
    target = await _load(db, user_id)
    if target.id == operator.id or target.email.lower() in settings.OPERATOR_EMAILS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Operator accounts cannot be suspended here")
    target.status = UserStatus.SUSPENDED
    target.token_version = (target.token_version or 0) + 1
    await db.commit()
    return AccountStatusOut(id=target.id, email=target.email, status=target.status)


@router.post("/{user_id}/reinstate", response_model=AccountStatusOut)
async def reinstate_account(user_id: uuid.UUID, operator: OperatorUser, db: DbSession) -> AccountStatusOut:
    """Lift a suspension. The user logs in again; tokens from before it stay dead."""
    target = await _load(db, user_id)
    if target.status is not UserStatus.SUSPENDED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Account is not suspended")
    target.status = UserStatus.ACTIVE
    target.token_version = (target.token_version or 0) + 1
    await db.commit()
    return AccountStatusOut(id=target.id, email=target.email, status=target.status)

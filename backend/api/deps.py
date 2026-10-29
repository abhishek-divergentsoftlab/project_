"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.security import TokenError
from db.session import get_db
from models.user import User
from services import auth_service

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    db: DbSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> User:
    if credentials is None:
        raise _UNAUTHORIZED

    try:
        # Demands type=access (a refresh token is rejected), then checks revocation,
        # token_version and that the account is still active.
        user, _ = await auth_service.user_from_access_token(db, credentials.credentials)
    except TokenError as exc:
        raise _UNAUTHORIZED from exc

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_operator_user(user: CurrentUser) -> User:
    # 404 rather than 403: the /ops surface is not advertised to other users.
    if user.email.lower() not in settings.OPERATOR_EMAILS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return user


OperatorUser = Annotated[User, Depends(get_operator_user)]


async def get_optional_current_user(
    db: DbSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> User | None:
    if credentials is None:
        return None
    try:
        user, _ = await auth_service.user_from_access_token(db, credentials.credentials)
    except TokenError:
        return None
    return user


CurrentUserOptional = Annotated[User | None, Depends(get_optional_current_user)]

"""Authentication endpoints."""

import uuid

from fastapi import APIRouter, HTTPException, status

from api.deps import CurrentUser, DbSession
from core.security import TokenError, decode_token
from schemas.auth import LoginRequest, RefreshRequest, SignupRequest, TokenPair
from schemas.user import UserOut
from services import auth_service
from services.auth_service import AuthError

router = APIRouter()


@router.post("/signup", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, db: DbSession) -> TokenPair:
    try:
        user = await auth_service.signup(db, payload)
    except AuthError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return auth_service.issue_tokens(user)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: DbSession) -> TokenPair:
    try:
        user = await auth_service.authenticate(db, payload.email, payload.password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return auth_service.issue_tokens(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    """Exchange a refresh token for a fresh pair.

    The old refresh token is not revoked here -- doing that needs a jti denylist
    in Redis, which arrives with the rest of the hardening work.
    """
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
        user_id = uuid.UUID(claims["sub"])
    except (TokenError, ValueError, KeyError) as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid refresh token"
        ) from exc

    user = await auth_service.get_user(db, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")

    return auth_service.issue_tokens(user)


@router.get("/me", response_model=UserOut)
async def me(current_user: CurrentUser) -> UserOut:
    return UserOut.model_validate(current_user)

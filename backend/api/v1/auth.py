"""Authentication endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.deps import CurrentUser, DbSession
from core import rate_limit
from core.config import settings
from core.rate_limit import RateLimit
from core.security import TokenError, decode_token
from schemas.auth import LoginRequest, RefreshRequest, SignupRequest, TokenPair
from schemas.user import UserOut
from services import auth_service
from services.auth_service import AuthError

router = APIRouter()


@router.post(
    "/signup",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimit("signup", "RATE_LIMIT_SIGNUP", per="ip"))],
)
async def signup(payload: SignupRequest, db: DbSession) -> TokenPair:
    try:
        user = await auth_service.signup(db, payload)
    except AuthError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An account with this email cannot be registered. Please try a different email or log in.",
        ) from exc
    return auth_service.issue_tokens(user)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, request: Request, db: DbSession) -> TokenPair:
    # Only failures are counted: a user who logs in successfully many times is
    # never throttled, a password guesser is after RATE_LIMIT_LOGIN failures
    # for one email, or RATE_LIMIT_LOGIN_IP failures across many emails.
    ip = rate_limit.client_ip(request)
    per_account = f"{ip}|{payload.email}"
    rate_limit.ensure_not_blocked("login", per_account, settings.RATE_LIMIT_LOGIN)
    rate_limit.ensure_not_blocked("login-ip", ip, settings.RATE_LIMIT_LOGIN_IP)
    try:
        user = await auth_service.authenticate(db, payload.email, payload.password)
    except AuthError as exc:
        rate_limit.record("login", per_account, settings.RATE_LIMIT_LOGIN)
        rate_limit.record("login-ip", ip, settings.RATE_LIMIT_LOGIN_IP)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return auth_service.issue_tokens(user)


@router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[Depends(RateLimit("refresh", "RATE_LIMIT_REFRESH", per="ip"))],
)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    """Exchange a refresh token for a fresh pair.

    The old refresh token is revoked immediately, enforcing single-use rotation.
    """
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
        user_id = uuid.UUID(claims["sub"])
    except (TokenError, ValueError, KeyError) as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid refresh token"
        ) from exc

    # Revoke old refresh token on rotation
    if claims.get("jti"):
        from core.security import revoke_jti
        revoke_jti(claims["jti"])

    user = await auth_service.get_user(db, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")

    return auth_service.issue_tokens(user)


@router.post("/logout", status_code=status.HTTP_200_OK, summary="Revoke session and refresh token")
async def logout(payload: RefreshRequest | None = None) -> dict[str, str]:
    if payload and payload.refresh_token:
        try:
            claims = decode_token(payload.refresh_token, expected_type="refresh")
            if claims.get("jti"):
                from core.security import revoke_jti
                revoke_jti(claims["jti"])
        except Exception:
            pass
    return {"message": "Successfully logged out"}


@router.get("/me", response_model=UserOut)
async def me(current_user: CurrentUser) -> UserOut:
    return UserOut.model_validate(current_user)

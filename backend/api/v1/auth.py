"""Authentication endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials

from api.deps import CurrentUser, DbSession, bearer_scheme
from core import rate_limit
from core.config import settings
from core.rate_limit import RateLimit
from core.security import (
    TokenError,
    decode_token,
    family_of,
    prune_revoked_tokens,
    revoke_family,
    revoke_token,
)
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
    # The email-only bucket does not depend on the (spoofable) client address,
    # so rotating X-Forwarded-For or source IPs cannot reset it.
    ip = rate_limit.client_ip(request)
    per_account = f"{ip}|{payload.email}"
    rate_limit.ensure_not_blocked("login", per_account, settings.RATE_LIMIT_LOGIN)
    rate_limit.ensure_not_blocked("login-ip", ip, settings.RATE_LIMIT_LOGIN_IP)
    rate_limit.ensure_not_blocked("login-email", payload.email, settings.RATE_LIMIT_LOGIN_EMAIL)
    try:
        user = await auth_service.authenticate(db, payload.email, payload.password)
    except AuthError as exc:
        rate_limit.record("login", per_account, settings.RATE_LIMIT_LOGIN)
        rate_limit.record("login-ip", ip, settings.RATE_LIMIT_LOGIN_IP)
        rate_limit.record("login-email", payload.email, settings.RATE_LIMIT_LOGIN_EMAIL)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return auth_service.issue_tokens(user)


@router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[Depends(RateLimit("refresh", "RATE_LIMIT_REFRESH", per="ip"))],
)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    """Exchange a refresh token for a fresh pair.

    The old refresh token is spent immediately (single-use rotation). Presenting
    a spent token again is treated as theft: the whole token family is revoked.
    """
    try:
        return await auth_service.rotate_refresh_token(db, payload.refresh_token)
    except (TokenError, ValueError, KeyError) as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid refresh token"
        ) from exc


@router.post("/logout", status_code=status.HTTP_200_OK, summary="Revoke session and refresh token")
async def logout(
    db: DbSession,
    payload: RefreshRequest | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> dict[str, str]:
    """Revokes the presented refresh token's family and the presented access token."""
    if payload and payload.refresh_token:
        try:
            claims = decode_token(payload.refresh_token, expected_type="refresh")
            await revoke_family(db, family_of(claims))
            await revoke_token(db, claims, reason="logout")
        except Exception:  # noqa: BLE001 - logout never fails the client
            pass
    if credentials is not None:
        try:
            claims = decode_token(credentials.credentials, expected_type="access")
            await revoke_token(db, claims, reason="logout")
        except Exception:  # noqa: BLE001
            pass
    try:
        await prune_revoked_tokens(db)
    except Exception:  # noqa: BLE001 - housekeeping only
        pass
    return {"message": "Successfully logged out"}


@router.get("/me", response_model=UserOut)
async def me(current_user: CurrentUser) -> UserOut:
    return UserOut.model_validate(current_user)

"""Signup, login, and token issuance."""

import uuid
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.security import (
    TokenError,
    assert_family_not_revoked,
    assert_not_revoked,
    create_access_token,
    create_refresh_token,
    decode_token,
    family_of,
    hash_password,
    needs_rehash,
    new_family_id,
    revoke_family,
    revoke_token,
    token_version_of,
    verify_password,
)
from models.enums import UserStatus
from models.user import User, UserProfile
from schemas.auth import SignupRequest, TokenPair

# Verified against when the email is unknown, so a miss costs the same time as
# a wrong password and the endpoint does not leak which emails are registered.
_DUMMY_HASH = hash_password("not-a-real-password")


class AuthError(Exception):
    """Authentication failed. The message is safe to show to the caller."""


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    return await db.scalar(select(User).where(User.email == email.strip().lower()))


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> Optional[User]:
    return await db.get(User, user_id)


async def signup(db: AsyncSession, payload: SignupRequest) -> User:
    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        status=UserStatus.PENDING_VERIFICATION,
    )
    user.profile = UserProfile(
        name=payload.name,
        company_name=payload.company_name,
        phone=payload.phone,
        city=payload.city,
    )

    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # The unique index is the real arbiter -- checking first would race.
        raise AuthError("an account with this email already exists") from exc

    await db.refresh(user)
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    user = await get_user_by_email(db, email)

    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise AuthError("invalid email or password")

    if not verify_password(password, user.password_hash):
        raise AuthError("invalid email or password")

    if not user.is_active:
        raise AuthError(f"this account is {user.status.value}")

    # Transparently upgrade hashes when argon2 parameters change.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.last_login_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(user)
    return user


def issue_tokens(user: User, family_id: Optional[str] = None) -> TokenPair:
    """A fresh pair. Login/signup start a new token family; refresh passes the old one on."""
    subject = str(user.id)
    family = family_id or new_family_id()
    version = user.token_version or 0
    return TokenPair(
        access_token=create_access_token(subject, family, version),
        refresh_token=create_refresh_token(subject, family, version),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def user_from_access_token(db: AsyncSession, token: str) -> tuple[User, dict]:
    """Resolve an access token to its active user, or raise TokenError.

    Beyond signature/expiry this rejects a revoked jti or family (logout, refresh reuse)
    and a ``tv`` claim that no longer matches ``users.token_version`` (suspension).
    """
    claims = decode_token(token, expected_type="access")
    try:
        user_id = uuid.UUID(claims["sub"])
    except (ValueError, KeyError) as exc:
        raise TokenError("bad subject") from exc
    await assert_not_revoked(db, claims)
    user = await get_user(db, user_id)
    if user is None or not user.is_active:
        raise TokenError("inactive user")
    if token_version_of(claims) != (user.token_version or 0):
        raise TokenError("token version mismatch")
    return user, claims


async def rotate_refresh_token(db: AsyncSession, refresh_token: str) -> TokenPair:
    """Single-use refresh. Presenting a spent token revokes its whole family."""
    claims = decode_token(refresh_token, expected_type="refresh")
    family = family_of(claims)
    # Only the family here: a spent jti must reach the atomic claim below so reuse revokes the family.
    await assert_family_not_revoked(db, claims)
    try:
        user_id = uuid.UUID(claims["sub"])
    except (ValueError, KeyError) as exc:
        raise TokenError("bad subject") from exc
    user = await get_user(db, user_id)
    if user is None or not user.is_active:
        raise TokenError("inactive user")
    if token_version_of(claims) != (user.token_version or 0):
        raise TokenError("token version mismatch")
    # Atomic claim of the jti: a concurrent or replayed second use loses the insert.
    if not await revoke_token(db, claims, reason="rotated"):
        await revoke_family(db, family)
        raise TokenError("refresh token reuse detected")
    return issue_tokens(user, family)

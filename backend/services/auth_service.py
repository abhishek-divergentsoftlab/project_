"""Signup, login, and token issuance."""

import uuid
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    needs_rehash,
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
        status=UserStatus.ACTIVE,
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


def issue_tokens(user: User) -> TokenPair:
    subject = str(user.id)
    return TokenPair(
        access_token=create_access_token(subject),
        refresh_token=create_refresh_token(subject),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )

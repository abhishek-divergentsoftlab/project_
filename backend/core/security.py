"""Password hashing and JWT issuing/verification.

Access and refresh tokens are both JWTs but carry a ``type`` claim. Verification
demands the expected type, so a refresh token can never be replayed as an access
token against a protected route.

Revocation is persistent (``revoked_tokens``). ``decode_token`` only checks the
signature, expiry and type; callers that hold a DB session then call
``assert_not_revoked`` (jti or whole family) and compare ``tv`` with
``users.token_version``. Tokens issued before families/versions existed carry
neither claim: a missing ``tv`` counts as 0 and a missing ``fam`` makes the
token its own family, so they keep working until they expire.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.user import RevokedToken

TokenType = Literal["access", "refresh"]

_hasher = PasswordHasher()


class TokenError(Exception):
    """Raised when a token is malformed, expired, or of the wrong type."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """True when argon2 parameters changed since this hash was written."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def _create_token(
    subject: str,
    token_type: TokenType,
    expires_in: timedelta,
    family_id: Optional[str] = None,
    token_version: int = 0,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_in,
        "jti": str(uuid.uuid4()),
        "tv": token_version,
    }
    if family_id:
        payload["fam"] = family_id
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def new_family_id() -> str:
    return str(uuid.uuid4())


def create_access_token(
    subject: str, family_id: Optional[str] = None, token_version: int = 0
) -> str:
    return _create_token(
        subject,
        "access",
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        family_id,
        token_version,
    )


def create_refresh_token(
    subject: str, family_id: Optional[str] = None, token_version: int = 0
) -> str:
    return _create_token(
        subject,
        "refresh",
        timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
        family_id,
        token_version,
    )


def decode_token(token: str, expected_type: TokenType) -> dict:
    """Verify signature, expiry and type. Does NOT consult the revocation table."""
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={"require": ["sub", "exp", "type"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    if payload.get("type") != expected_type:
        raise TokenError(f"expected a {expected_type} token")
    return payload


# --- persistent revocation -------------------------------------------------


def family_of(claims: dict) -> Optional[str]:
    """The token family: the ``fam`` claim, else the token's own jti (pre-family tokens)."""
    return claims.get("fam") or claims.get("jti")


def token_version_of(claims: dict) -> int:
    value = claims.get("tv", 0)
    return value if isinstance(value, int) else 0


def _family_key(family_id: str) -> str:
    return f"fam:{family_id}"


def _expiry(claims: dict) -> datetime:
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(exp, UTC)
    return datetime.now(UTC) + timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)


async def is_revoked(db: AsyncSession, claims: dict) -> bool:
    """True when this token's jti, or its whole family, has been revoked."""
    keys = [k for k in (claims.get("jti"),) if k]
    family = family_of(claims)
    if family:
        keys.append(_family_key(family))
    if not keys:
        return False
    hit = await db.scalar(select(RevokedToken.jti).where(RevokedToken.jti.in_(keys)).limit(1))
    return hit is not None


async def assert_family_not_revoked(db: AsyncSession, claims: dict) -> None:
    """Like ``assert_not_revoked`` but ignores the token's own jti (refresh reuse is detected separately)."""
    family = family_of(claims)
    if family and await db.scalar(
        select(RevokedToken.jti).where(RevokedToken.jti == _family_key(family)).limit(1)
    ):
        raise TokenError("token family has been revoked")


async def assert_not_revoked(db: AsyncSession, claims: dict) -> None:
    if await is_revoked(db, claims):
        raise TokenError("token has been revoked")


async def revoke_token(db: AsyncSession, claims: dict, reason: str = "logout") -> bool:
    """Record this token's jti as unusable. False when it was already recorded (reuse)."""
    jti = claims.get("jti")
    if not jti:
        return True
    result = await db.execute(
        pg_insert(RevokedToken)
        .values(jti=jti, family_id=family_of(claims), expires_at=_expiry(claims), reason=reason)
        .on_conflict_do_nothing(index_elements=["jti"])
        .returning(RevokedToken.jti)
    )
    inserted = result.scalar_one_or_none() is not None
    await db.commit()
    return inserted


async def revoke_family(db: AsyncSession, family_id: str) -> None:
    """Kill every token (access and refresh) of a family; lasts as long as a refresh token could."""
    expires = datetime.now(UTC) + timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)
    await db.execute(
        pg_insert(RevokedToken)
        .values(jti=_family_key(family_id), family_id=family_id, expires_at=expires, reason="family")
        .on_conflict_do_nothing(index_elements=["jti"])
    )
    await db.commit()


async def prune_revoked_tokens(db: AsyncSession) -> int:
    """Delete rows whose tokens have expired anyway. Called opportunistically on logout."""
    result = await db.execute(delete(RevokedToken).where(RevokedToken.expires_at < datetime.now(UTC)))
    await db.commit()
    return result.rowcount or 0

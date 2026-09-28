"""Async engine, session factory, and the FastAPI dependency."""

import os
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import settings

_TRUTHY = {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


def pool_settings() -> dict[str, Any]:
    """Connection-pool sizing, overridable per deployment.

    The SQLAlchemy default (5 + 10 overflow) is small for a server that holds a
    session open across long AI streams; 10 + 20 leaves headroom while staying
    well under Postgres' default ``max_connections`` of 100 for one worker.
    """
    return {
        "pool_size": _env_int("DB_POOL_SIZE", 10),
        "max_overflow": _env_int("DB_MAX_OVERFLOW", 20),
        "pool_timeout": _env_int("DB_POOL_TIMEOUT", 30),
        "pool_pre_ping": _env_bool("DB_POOL_PRE_PING", True),
        "pool_recycle": _env_int("DB_POOL_RECYCLE", 1800),
    }


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    **pool_settings(),
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a session and roll back if the request raises."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

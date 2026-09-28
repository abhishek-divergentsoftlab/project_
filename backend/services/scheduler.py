"""In-process background jobs: RFQ expiry, embedding retry, match-history pruning.

Started from the FastAPI lifespan and cancelled on shutdown. Each job:

* opens its **own** DB session (never a request's),
* is wrapped in try/except so one failure is logged and retried next tick
  instead of killing the loop,
* is idempotent, so running it twice (two workers, a restart mid-run) is safe.

Disabled when ``DISABLE_SCHEDULER=1`` or when running under pytest. The job
functions are plain ``async def job(db) -> int`` so tests (and a future
Redis/arq worker) can call them directly.

Environment:
    DISABLE_SCHEDULER=1              turn the loops off
    SCHEDULER_INTERVAL_SECONDS=600   period for every job (default 10 min)
    SCHEDULER_INITIAL_DELAY_SECONDS=30  wait after startup before the first run
    EMBEDDING_RETRY_BATCH=50         max RFQs re-embedded per tick
    MATCH_HISTORY_RETENTION_DAYS=30  age after which match history is pruned
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

JobFn = Callable[[AsyncSession], Awaitable[int]]

# A PENDING/STALE row younger than this is probably still being indexed inline
# by the request that created it; leave it alone.
EMBEDDING_RETRY_MIN_AGE = timedelta(minutes=5)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def scheduler_disabled() -> bool:
    """True when background loops must not start (explicit opt-out or tests)."""
    flag = os.environ.get("DISABLE_SCHEDULER", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


# --- jobs -------------------------------------------------------------------


async def expire_rfqs_job(db: AsyncSession) -> int:
    """Flip past-due ACTIVE RFQs to EXPIRED."""
    from services import rfq_service

    return await rfq_service.expire_due_rfqs(db)


async def retry_embeddings_job(db: AsyncSession, *, limit: Optional[int] = None) -> int:
    """Re-embed RFQs whose vector never landed in Qdrant.

    Picks FAILED / PENDING / STALE rows not touched for 5 minutes, oldest first,
    and pushes each through ``rfq_service.index_rfq`` -- the same entry point
    POST /rfqs uses, which records INDEXED or FAILED (+ reason) and commits.
    Because a retry bumps ``updated_at``, rows that keep failing rotate to the
    back of the queue instead of starving the rest. Returns rows now INDEXED.
    """
    from models.enums import EmbeddingStatus
    from models.rfq import RFQ
    from services import rfq_service

    limit = limit or _env_int("EMBEDDING_RETRY_BATCH", 50)
    cutoff = datetime.now(UTC) - EMBEDDING_RETRY_MIN_AGE
    ids = (
        await db.scalars(
            select(RFQ.id)
            .where(
                RFQ.embedding_status.in_(
                    [
                        EmbeddingStatus.FAILED,
                        EmbeddingStatus.PENDING,
                        EmbeddingStatus.STALE,
                    ]
                ),
                RFQ.updated_at <= cutoff,
            )
            .order_by(RFQ.updated_at)
            .limit(limit)
        )
    ).all()

    indexed = 0
    for rfq_id in ids:
        try:
            # Loaded one at a time so a rollback after a bad row cannot leave
            # the rest of the batch as expired instances.
            rfq = await db.get(RFQ, rfq_id)
            if rfq is not None and await rfq_service.index_rfq(db, rfq):
                indexed += 1
        except Exception:  # noqa: BLE001 - one bad row must not stop the batch
            logger.exception("embedding retry failed for rfq %s", rfq_id)
            await db.rollback()
    if ids:
        logger.info("embedding retry: %d/%d RFQ(s) indexed", indexed, len(ids))
    return indexed


async def prune_match_history_job(
    db: AsyncSession, *, retention_days: Optional[int] = None
) -> int:
    """Delete match searches/results older than the retention window."""
    from models.match import MatchResult, MatchSearch

    days = retention_days or _env_int("MATCH_HISTORY_RETENTION_DAYS", 30)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    results = await db.execute(
        delete(MatchResult).where(MatchResult.created_at < cutoff)
    )
    # Results of an old search cascade away with it (ON DELETE CASCADE).
    searches = await db.execute(
        delete(MatchSearch).where(MatchSearch.created_at < cutoff)
    )
    await db.commit()
    removed = (results.rowcount or 0) + (searches.rowcount or 0)
    if removed:
        logger.info(
            "match history pruned: %d search(es), %d result(s) older than %d days",
            searches.rowcount or 0,
            results.rowcount or 0,
            days,
        )
    return removed


# --- runner -----------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    name: str
    fn: JobFn
    interval: float


def default_jobs() -> list[Job]:
    interval = float(_env_int("SCHEDULER_INTERVAL_SECONDS", 600))
    return [
        Job("expire_rfqs", expire_rfqs_job, interval),
        Job("retry_embeddings", retry_embeddings_job, interval),
        Job("prune_match_history", prune_match_history_job, interval),
    ]


async def run_job_once(
    job: Job, session_factory: async_sessionmaker[AsyncSession]
) -> Optional[int]:
    """Run one job in its own session; never raises (except cancellation)."""
    try:
        async with session_factory() as db:
            result = await job.fn(db)
        logger.debug("job %s finished: %s", job.name, result)
        return result
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - logged, retried next tick
        logger.exception("background job %s failed", job.name)
        return None


class Scheduler:
    """One asyncio task per job, each sleeping ``interval`` between runs."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        jobs: Optional[list[Job]] = None,
        initial_delay: Optional[float] = None,
    ) -> None:
        self._session_factory = session_factory
        self._jobs = jobs if jobs is not None else default_jobs()
        self._initial_delay = (
            initial_delay
            if initial_delay is not None
            else float(_env_int("SCHEDULER_INITIAL_DELAY_SECONDS", 30))
        )
        self._tasks: list[asyncio.Task] = []

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    async def _loop(self, job: Job) -> None:
        await asyncio.sleep(self._initial_delay)
        while True:
            await run_job_once(job, self._session_factory)
            await asyncio.sleep(job.interval)

    def start(self) -> None:
        if self.running:
            return
        self._tasks = [
            asyncio.create_task(self._loop(job), name=f"job:{job.name}")
            for job in self._jobs
        ]
        logger.info(
            "background scheduler started: %s",
            ", ".join(f"{j.name}/{int(j.interval)}s" for j in self._jobs),
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("background task %s ended with an error", task.get_name())
        if self._tasks:
            logger.info("background scheduler stopped")
        self._tasks = []

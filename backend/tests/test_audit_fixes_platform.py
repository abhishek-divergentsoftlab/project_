"""Platform fixes from the audit (AUDIT_REPORT.md section 3.6 / platform items).

Covers: migration 33f0b331948b indexes, one escrow per connection (and the
dedupe step that makes the unique index safe), security headers, access-log
token redaction, logging configuration, background job functions and the
scheduler lifecycle, and env-configurable pool settings.
"""

import asyncio
import importlib.util
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from db.session import SessionLocal, engine, pool_settings
from models.enums import EmbeddingStatus, RFQRole, RFQStatus
from models.match import MatchResult, MatchSearch
from models.rfq import RFQ

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "33f0b331948b_search_indexes_and_unique_escrow.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_33f0b331948b", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- migration --------------------------------------------------------------


async def test_migration_indexes_exist(db):
    rows = dict(
        (
            await db.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename IN ('rfqs', 'connection_messages', "
                    "'match_results', 'escrow_accounts')"
                )
            )
        ).all()
    )
    for column in (
        "title",
        "search_text",
        "location_city",
        "location_country",
        "category",
        "description",
    ):
        name = f"ix_rfqs_{column}_trgm"
        assert name in rows, name
        assert "gin" in rows[name] and "gin_trgm_ops" in rows[name]

    assert "WHERE (status = 'active'" in rows["ix_rfqs_active_created"]
    assert "created_at DESC" in rows["ix_rfqs_active_created"]
    assert "(sender_id)" in rows["ix_connection_messages_sender_id"]
    assert "(rfq_id)" in rows["ix_match_results_rfq_id"]
    assert rows["ix_escrow_accounts_connection_id"].startswith("CREATE UNIQUE INDEX")

    ext = await db.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='pg_trgm'"))
    assert ext == 1


async def test_catalog_search_regex_can_use_trigram_index(db):
    """The catalog's ``~* '\\mcable'`` predicate is indexable once pg_trgm exists."""
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        r[0]
        for r in (
            await db.execute(
                text("EXPLAIN SELECT id FROM rfqs WHERE title ~* '\\mcable'")
            )
        ).all()
    )
    assert "ix_rfqs_title_trgm" in plan, plan
    await db.rollback()


async def _escrow_for(accepted_pair):
    buyer, seller, conn_id, _listing = accepted_pair
    quote = (
        await seller.post(
            f"/connections/{conn_id}/quotes",
            json={
                "unit_price": 100.0,
                "currency": "USD",
                "quantity": 10.0,
                "quantity_unit": "units",
                "lead_time_days": 5,
                "incoterms": "FOB",
                "payment_terms": "Escrow",
                "valid_days": 14,
                "notes": "n",
            },
        )
    ).json()
    accepted = await buyer.post(f"/connections/{conn_id}/quotes/{quote['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    escrow = await buyer.get(f"/connections/{conn_id}/escrow")
    assert escrow.status_code == 200, escrow.text
    return buyer, conn_id, escrow.json()


_CLONE_ESCROW = """
    INSERT INTO escrow_accounts (id, connection_id, quotation_id, buyer_id, seller_id,
        currency, total_amount, funded_amount, released_amount, refunded_amount,
        status, created_at, updated_at)
    SELECT :new_id, connection_id, quotation_id, buyer_id, seller_id, currency,
        total_amount, :funded, 0, 0, status, created_at + interval '1 second', now()
    FROM escrow_accounts WHERE id = :src
"""


async def test_unique_escrow_per_connection(accepted_pair):
    _buyer, _conn_id, escrow = await _escrow_for(accepted_pair)
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(_CLONE_ESCROW),
                {"new_id": uuid.uuid4(), "funded": 0, "src": escrow["id"]},
            )
        await conn.rollback()


async def test_migration_dedupe_keeps_most_active_and_repoints_disputes(accepted_pair):
    buyer, conn_id, escrow = await _escrow_for(accepted_pair)
    original = uuid.UUID(escrow["id"])
    clone = uuid.uuid4()
    migration = _load_migration()

    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP INDEX ix_escrow_accounts_connection_id"))
            # The clone has money on it, so it must be the survivor even
            # though the original is older.
            await conn.execute(
                text(_CLONE_ESCROW), {"new_id": clone, "funded": 300, "src": original}
            )
            await conn.execute(
                text(
                    "INSERT INTO deal_disputes (connection_id, escrow_account_id, "
                    "raised_by_id, title, category, reason, severity, status) VALUES "
                    "(:c, :e, :u, 'late', 'delivery', 'late goods', 'medium', 'open')"
                ),
                {"c": conn_id, "e": original, "u": buyer.id},
            )
            for statement in migration.DEDUPE_ESCROW_SQL:
                await conn.execute(text(statement))

            left = (
                await conn.execute(
                    text("SELECT id FROM escrow_accounts WHERE connection_id = :c"),
                    {"c": conn_id},
                )
            ).scalars().all()
            assert left == [clone]
            dispute_owner = await conn.scalar(
                text("SELECT escrow_account_id FROM deal_disputes WHERE connection_id = :c"),
                {"c": conn_id},
            )
            assert dispute_owner == clone
            # The survivor keeps its own milestones only (the loser's cascaded).
            orphans = await conn.scalar(
                text("SELECT count(*) FROM escrow_milestones WHERE escrow_account_id = :e"),
                {"e": original},
            )
            assert orphans == 0
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP INDEX IF EXISTS ix_escrow_accounts_connection_id"))
            await conn.execute(text("TRUNCATE escrow_accounts CASCADE"))
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX ix_escrow_accounts_connection_id "
                    "ON escrow_accounts (connection_id)"
                )
            )


# --- HTTP hardening ---------------------------------------------------------


async def test_security_headers_present(client):
    response = await client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in response.headers["permissions-policy"]
    assert "content-security-policy" not in response.headers


async def test_media_may_be_framed_by_same_origin_only(client):
    response = await client.get("/media/certificates/does-not-exist.pdf")
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["x-content-type-options"] == "nosniff"


# --- logging ----------------------------------------------------------------


def _record(logger_name: str, msg: str, args: tuple) -> logging.LogRecord:
    return logging.LogRecord(logger_name, logging.INFO, __file__, 1, msg, args, None)


def test_access_log_token_redaction():
    from core.logging_setup import REDACTED, RedactQueryTokensFilter

    flt = RedactQueryTokensFilter()
    access = _record(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/api/v1/ws?token=eyJhbGciOi.secret.sig&x=1", "1.1", 101),
    )
    assert flt.filter(access)
    line = access.getMessage()
    assert "eyJhbGciOi" not in line
    assert f"token={REDACTED}&x=1" in line

    ws = _record(
        "uvicorn.error",
        '%s - "WebSocket %s" [accepted]',
        ("127.0.0.1:1", "/api/v1/ws/chat?access_token=abc.def&refresh_token=zzz"),
    )
    flt.filter(ws)
    assert "abc.def" not in ws.getMessage() and "zzz" not in ws.getMessage()

    plain = _record("uvicorn.access", "GET /api/v1/marketplace/catalog?q=token", ())
    flt.filter(plain)
    assert plain.getMessage().endswith("?q=token")


def test_configure_logging_routes_service_info_logs(monkeypatch):
    from core import logging_setup

    monkeypatch.setenv("LOG_LEVEL", "INFO")
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    try:
        logging_setup.configure_logging()
        logging_setup.configure_logging()  # idempotent
        ours = [h for h in root.handlers if getattr(h, "_marketplace_handler", False)]
        assert len(ours) == 1
        assert "%(asctime)s" in ours[0].formatter._fmt
        assert "%(name)s" in ours[0].formatter._fmt
        assert logging.getLogger("services.agent_router").isEnabledFor(logging.INFO)
        assert any(
            type(f).__name__ == "RedactQueryTokensFilter"
            for f in logging.getLogger("uvicorn.access").filters
        )
    finally:
        for h in list(root.handlers):
            if h not in before_handlers:
                root.removeHandler(h)
        root.setLevel(before_level)


# --- background jobs ----------------------------------------------------------


async def test_scheduler_disabled_under_tests(monkeypatch):
    from services.scheduler import scheduler_disabled

    assert scheduler_disabled()
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    assert scheduler_disabled()


async def test_expire_job_flips_past_due_rfqs(make_actor, make_rfq):
    from services.scheduler import expire_rfqs_job

    seller = await make_actor("seller")
    rfq = await make_rfq(seller, role="seller")
    async with SessionLocal() as db:
        await db.execute(
            update(RFQ)
            .where(RFQ.id == uuid.UUID(rfq["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await db.commit()

    async with SessionLocal() as db:
        assert await expire_rfqs_job(db) == 1
    async with SessionLocal() as db:
        status = await db.scalar(select(RFQ.status).where(RFQ.id == uuid.UUID(rfq["id"])))
        assert status == RFQStatus.EXPIRED
        assert await expire_rfqs_job(db) == 0


async def test_retry_embeddings_job_reindexes_old_failures(make_actor, make_rfq, monkeypatch):
    from services import scheduler

    seller = await make_actor("seller")
    old = await make_rfq(seller, role="seller", title="Old failed listing")
    fresh = await make_rfq(seller, role="seller", title="Fresh pending listing")
    long_ago = datetime.now(UTC) - timedelta(minutes=30)
    async with SessionLocal() as db:
        await db.execute(
            update(RFQ)
            .where(RFQ.id == uuid.UUID(old["id"]))
            .values(embedding_status=EmbeddingStatus.FAILED, updated_at=long_ago)
        )
        await db.execute(
            update(RFQ)
            .where(RFQ.id == uuid.UUID(fresh["id"]))
            .values(embedding_status=EmbeddingStatus.PENDING)
        )
        await db.commit()

    seen: list[uuid.UUID] = []

    async def fake_index(db, rfq):
        seen.append(rfq.id)
        rfq.embedding_status = EmbeddingStatus.INDEXED
        await db.commit()
        return True

    monkeypatch.setattr("services.rfq_service.index_rfq", fake_index)
    async with SessionLocal() as db:
        assert await scheduler.retry_embeddings_job(db) == 1
    # Only the row that has been failing for > 5 minutes is retried; the fresh
    # one may still be mid-flight in its own request.
    assert seen == [uuid.UUID(old["id"])]
    async with SessionLocal() as db:
        st = await db.scalar(select(RFQ.embedding_status).where(RFQ.id == uuid.UUID(old["id"])))
        assert st == EmbeddingStatus.INDEXED


async def test_prune_match_history_job(make_actor, make_rfq):
    from services.scheduler import prune_match_history_job

    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    listing = await make_rfq(seller, role="seller")
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        for age_days in (45, 1):
            search = MatchSearch(
                user_id=uuid.UUID(buyer.id),
                query=f"q{age_days}",
                requester_role=RFQRole.BUYER,
                target_role=RFQRole.SELLER,
                last_cursor=0,
                created_at=now - timedelta(days=age_days),
            )
            db.add(search)
            await db.flush()
            db.add(
                MatchResult(
                    search_id=search.id,
                    rfq_id=uuid.UUID(listing["id"]),
                    rank=1,
                    score=0.9,
                    created_at=now - timedelta(days=age_days),
                )
            )
        await db.commit()

    async with SessionLocal() as db:
        assert await prune_match_history_job(db) >= 2
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(MatchSearch)) == 1
        assert await db.scalar(select(func.count()).select_from(MatchResult)) == 1
        assert await db.scalar(select(MatchSearch.query)) == "q1"


async def test_scheduler_runs_jobs_and_cancels_cleanly():
    from services.scheduler import Job, Scheduler, run_job_once

    calls: list[str] = []

    async def ok(_db):
        calls.append("ok")
        return 1

    async def boom(_db):
        calls.append("boom")
        raise RuntimeError("job failure must be contained")

    # A failing job is logged, not raised.
    assert await run_job_once(Job("boom", boom, 60), SessionLocal) is None

    sched = Scheduler(SessionLocal, jobs=[Job("ok", ok, 0.01), Job("boom", boom, 0.01)], initial_delay=0)
    sched.start()
    assert sched.running
    await asyncio.sleep(0.1)
    await sched.stop()
    assert not sched.running
    assert calls.count("ok") >= 2 and calls.count("boom") >= 2


# --- pool -------------------------------------------------------------------


def test_pool_settings_from_env(monkeypatch):
    for name in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_POOL_PRE_PING"):
        monkeypatch.delenv(name, raising=False)
    defaults = pool_settings()
    assert defaults["pool_size"] == 10
    assert defaults["max_overflow"] == 20
    assert defaults["pool_timeout"] == 30
    assert defaults["pool_pre_ping"] is True

    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "4")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "7")
    monkeypatch.setenv("DB_POOL_PRE_PING", "false")
    custom = pool_settings()
    assert (custom["pool_size"], custom["max_overflow"], custom["pool_timeout"]) == (3, 4, 7)
    assert custom["pool_pre_ping"] is False

    monkeypatch.setenv("DB_POOL_SIZE", "not-a-number")
    assert pool_settings()["pool_size"] == 10


def test_engine_uses_configured_pool():
    assert engine.pool.size() == pool_settings()["pool_size"]
    assert engine.pool._max_overflow == pool_settings()["max_overflow"]

"""Test harness.

Everything runs against a dedicated ``marketplace_test`` database that is
dropped and rebuilt from the Alembic migrations at the start of each session, so
the suite can never read, corrupt or depend on the seeded development data. The
URL is set before any project module is imported, because ``db.session`` builds
its engine at import time.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator, Optional

import pytest

_ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL",
    "postgresql://marketplace:marketplace@localhost:5433/marketplace",
)
TEST_DB_NAME = os.environ.get("TEST_DB_NAME", "marketplace_test")
_TEST_URL = _ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB_NAME

# Must happen before the first `import db.session` anywhere in the process.
os.environ["DATABASE_URL"] = "postgresql+asyncpg://" + _TEST_URL.split("://", 1)[1]

import asyncpg  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app import app  # noqa: E402
from core.config import settings  # noqa: E402

# Fast, offline deterministic execution for unit and regression tests
settings.DIRECT_SEARCH_LLM = False
from db.session import SessionLocal, engine  # noqa: E402
from models.enums import RFQRole, RFQStatus, UserRole  # noqa: E402

pytest_plugins = ("pytest_asyncio",)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: talks to Qdrant or Ollama")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


async def _recreate_database() -> None:
    admin = await asyncpg.connect(_ADMIN_URL)
    try:
        # Terminate stragglers first; DROP DATABASE fails while anything is
        # still attached, and a crashed earlier run leaves connections behind.
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            TEST_DB_NAME,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
        await admin.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await admin.close()


@pytest.fixture(scope="session", autouse=True)
async def database() -> AsyncIterator[None]:
    """A schema built the same way production's is: by running the migrations.

    Using ``metadata.create_all`` here would let a broken migration pass the
    whole suite, which is the one thing this fixture exists to catch.
    """
    assert settings.DATABASE_URL.endswith(
        TEST_DB_NAME
    ), f"refusing to run against {settings.DATABASE_URL}"

    await _recreate_database()

    # Alembic's async env.py calls asyncio.run(), which cannot run inside the
    # loop pytest-asyncio already has open -- so it goes in its own process.
    # That also proves the migrations work the way an operator runs them.
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "DATABASE_URL": settings.DATABASE_URL},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    yield

    await engine.dispose()


@pytest.fixture
async def db() -> AsyncIterator[Any]:
    """A session for arranging fixtures and asserting on stored state."""
    async with SessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
async def clean_tables(database: None) -> AsyncIterator[None]:
    """Truncate between tests so order never matters.

    RESTART IDENTITY CASCADE rather than per-table deletes: the FK graph between
    users, rfqs, connections and match searches makes ordering fragile.
    """
    yield
    async with SessionLocal() as session:
        from sqlalchemy import text

        await session.execute(
            text(
                "TRUNCATE users, rfqs, connections, connection_messages, quotations, "
                "reviews, certificates, moderation_logs, "
                "conversations, messages, message_events, match_searches, "
                "match_results RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()



@pytest.fixture
async def client(database: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        yield c


@pytest.fixture(autouse=True)
def offline_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the SQL retrieval path and firewall the live vector index.

    Two reasons, and the second is the important one:

    1. Qdrant holds the development corpus, not this one, so a live vector query
       would return ids that do not exist here. Pinning ``embed_one`` to None
       exercises the documented fallback and keeps ranking deterministic.
    2. Creating an RFQ indexes it inline. Without this, every test run would
       write its throwaway rows into the real ``marketplace_rfq`` collection and
       quietly corrupt the development data the suite is supposed to leave
       alone.

    The Qdrant calls are replaced with ones that fail loudly rather than
    silently no-op, so a future change that reaches the network here shows up as
    a failing test instead of as drift in somebody's search results.
    """

    async def no_embedding(_text: str) -> None:
        return None

    async def forbidden(*_args: Any, **_kwargs: Any):
        raise AssertionError(
            "the test suite must not talk to the live Qdrant collection"
        )

    monkeypatch.setattr("services.match_service.embed_one", no_embedding)
    monkeypatch.setattr("services.rfq_service.embed_one", no_embedding)
    for call in ("upsert", "delete", "search", "ensure_collection"):
        monkeypatch.setattr(f"services.qdrant_index.{call}", forbidden)


# --- factories --------------------------------------------------------------

_PASSWORD = "correct-horse-battery"


class Actor:
    """A signed-up account plus the headers to act as it."""

    def __init__(self, client: AsyncClient, user_id: str, email: str, tokens: dict):
        self._client = client
        self.id = user_id
        self.email = email
        self.tokens = tokens

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens['access_token']}"}

    async def post(self, url: str, **kwargs: Any):
        return await self._client.post(url, headers=self.headers, **kwargs)

    async def get(self, url: str, **kwargs: Any):
        return await self._client.get(url, headers=self.headers, **kwargs)

    async def patch(self, url: str, **kwargs: Any):
        return await self._client.patch(url, headers=self.headers, **kwargs)

    async def delete(self, url: str, **kwargs: Any):
        return await self._client.delete(url, headers=self.headers, **kwargs)


@pytest.fixture
def make_actor(client: AsyncClient):
    async def _make(
        role: UserRole | str = UserRole.BUYER,
        *,
        email: Optional[str] = None,
        password: str = _PASSWORD,
        name: str = "Test Person",
        company_name: Optional[str] = None,
        phone: Optional[str] = None,
        city: Optional[str] = None,
    ) -> Actor:
        email = email or f"user-{uuid.uuid4().hex[:12]}@tests.example.com"
        body: dict[str, Any] = {
            "email": email,
            "password": password,
            "name": name,
            "role": role.value if isinstance(role, UserRole) else role,
        }
        if company_name:
            body["company_name"] = company_name
        if phone:
            body["phone"] = phone
        if city:
            body["city"] = city

        response = await client.post("/auth/signup", json=body)
        assert response.status_code == 201, response.text
        tokens = response.json()

        me = await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert me.status_code == 200, me.text
        return Actor(client, me.json()["id"], email, tokens)

    return _make


def rfq_body(
    role: RFQRole | str = RFQRole.BUYER,
    *,
    title: str = "Need 6000 red Type-C cables",
    category: str = "Electronics",
    product: str = "usb type-c cable",
    attributes: Optional[dict[str, Any]] = None,
    quantity: Optional[tuple[float, str]] = (6000, "pcs"),
    price: Optional[tuple[float, str, str]] = (200, "INR", "pcs"),
    city: Optional[str] = "Indore",
    state: Optional[str] = "Madhya Pradesh",
    country: Optional[str] = "India",
    latitude: Optional[float] = 22.7196,
    longitude: Optional[float] = 75.8577,
    deadline_days: Optional[int] = 7,
    status: RFQStatus | str = RFQStatus.ACTIVE,
) -> dict[str, Any]:
    """A complete, valid RFQ body. Override only what a test is about."""
    details: dict[str, Any] = {"name": product}
    details.update(attributes or {})

    body: dict[str, Any] = {
        "role": role.value if isinstance(role, RFQRole) else role,
        "category": category,
        "title": title,
        "product_details": details,
        "status": status.value if isinstance(status, RFQStatus) else status,
    }
    if quantity:
        body["quantity"] = {"value": quantity[0], "unit": quantity[1]}
    if price:
        body["price_target"] = {
            "amount": price[0],
            "currency": price[1],
            "per_unit": price[2],
        }
    if city or state or country:
        body["location"] = {
            "city": city,
            "state": state,
            "country": country,
            "latitude": latitude,
            "longitude": longitude,
        }
    if deadline_days is not None:
        body["deadline"] = {
            "in_days": deadline_days,
            "raw": f"within {deadline_days} days",
        }
    return body


@pytest.fixture
def make_rfq():
    async def _make(actor: Actor, **overrides: Any) -> dict[str, Any]:
        response = await actor.post("/rfqs", json=rfq_body(**overrides))
        assert response.status_code == 201, response.text
        return response.json()

    return _make


@pytest.fixture
async def accepted_pair(make_actor, make_rfq):
    """A buyer and a seller connected with mutual consent."""
    buyer = await make_actor("buyer", name="Buyer Alpha", company_name="Alpha Sourcing")
    seller = await make_actor(
        "seller", name="Seller Beta", company_name="Beta Manufacturing", city="Pune"
    )
    listing = await make_rfq(
        seller, role="seller", title="Supplying 10,000 industrial ball bearings"
    )
    conn_res = await buyer.post("/connections", json={"rfq_id": listing["id"]})
    conn_id = conn_res.json()["id"]

    # Seller accepts connection
    await seller.post(f"/connections/{conn_id}/accept")
    return buyer, seller, conn_id, listing


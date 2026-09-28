"""AUDIT PROBE (temporary): performance / query-count measurements.

Run only with:
  TEST_DB_NAME=marketplace_audit_perf .venv/bin/python -m pytest -q -s -p no:cacheprovider tests/test_zz_audit_perf.py
"""

import asyncio
import random
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import event, insert, text

from db.session import engine
from models.connection import Connection, ConnectionMessage
from models.enums import ConnectionStatus, RFQRole, RFQStatus
from models.rfq import RFQ
from tests.conftest import rfq_body

RESULTS: list[tuple[str, str, float, int]] = []

PRODUCTS = [
    ("Electronics", "usb type-c cable"), ("Electronics", "power bank 10000mah"),
    ("Agriculture", "basmati rice"), ("Agriculture", "fuji apples"),
    ("Textiles", "combed cotton yarn"), ("Industrial", "316l steel pipe"),
    ("Furniture", "office chair"), ("Packaging", "corrugated box"),
    ("Industrial", "ball bearings"), ("Agriculture", "red onions"),
]
CITIES = [
    ("Indore", "Madhya Pradesh", 22.7196, 75.8577),
    ("Mumbai", "Maharashtra", 19.0760, 72.8777),
    ("Delhi", "Delhi", 28.6139, 77.2090),
    ("Chennai", "Tamil Nadu", 13.0827, 80.2707),
    ("Kolkata", "West Bengal", 22.5726, 88.3639),
]


class Counter:
    def __init__(self) -> None:
        self.n = 0
        self.statements: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        self.n += 1
        self.statements.append(statement.split("\n")[0][:90])


@contextmanager
def count_sql():
    c = Counter()
    event.listen(engine.sync_engine, "before_cursor_execute", c)
    try:
        yield c
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", c)


async def timed(label, size, coro_fn, repeat=3):
    best = None
    q = 0
    resp = None
    for _ in range(repeat):
        with count_sql() as c:
            t0 = time.perf_counter()
            resp = await coro_fn()
            dt = (time.perf_counter() - t0) * 1000
        best = dt if best is None else min(best, dt)
        q = c.n
    RESULTS.append((label, size, round(best, 1), q))
    print(f"[PERF] {label:55s} | {size:>14s} | {best:8.1f} ms | {q:3d} SQL")
    if size == "2k rfqs" and label.startswith(("catalog newest", "match run (SQL", "dashboard stats")) or label.startswith("GET /connections/{id}"):
        for st in c.statements:
            print("    [SQL]", st)
    return resp


def _rfq_row(owner_id, i, now):
    cat, prod = PRODUCTS[i % len(PRODUCTS)]
    city, state, lat, lon = CITIES[i % len(CITIES)]
    role = RFQRole.SELLER if i % 3 else RFQRole.BUYER
    price = Decimal(50 + (i % 400))
    title = f"{'Supplying' if role is RFQRole.SELLER else 'Need'} {1000 + i} {prod} lot {i}"
    search_text = (
        f"Role: {role.value}\nCategory: {cat}\nProduct: {prod}\nQuantity: {1000 + i} pcs\n"
        f"Target price: {price} INR per pcs\nLocation: {city}, {state}, India\n"
        f"Attributes:\n- Colour: {'red' if i % 2 else 'white'}\n- Grade: A{i % 5}"
    )
    return dict(
        id=uuid.uuid4(), user_id=owner_id, role=role, status=RFQStatus.ACTIVE,
        category=cat, title=title, description=f"Bulk {prod} for industrial buyers, batch {i}",
        quantity_value=Decimal(1000 + i), quantity_unit="pcs",
        price_amount=price, price_currency="INR", price_per_unit="pcs",
        location_city=city, location_state=state, location_country="India",
        latitude=Decimal(str(lat)), longitude=Decimal(str(lon)),
        deadline_at=now + timedelta(days=7 + i % 30),
        expires_at=now + timedelta(days=30),
        product_details={"name": prod, "colour": "red" if i % 2 else "white", "grade": f"A{i % 5}"},
        search_tags=[prod, cat.lower(), "red" if i % 2 else "white"],
        search_text=search_text,
        created_at=now - timedelta(minutes=i),
        updated_at=now - timedelta(minutes=i),
    )


async def _bulk_rfqs(db, owner_ids, start, count):
    now = datetime.now(UTC)
    rows = [_rfq_row(owner_ids[i % len(owner_ids)], i, now) for i in range(start, start + count)]
    for chunk in range(0, len(rows), 1000):
        await db.execute(insert(RFQ), rows[chunk: chunk + 1000])
    await db.commit()
    await db.execute(text("ANALYZE rfqs"))
    await db.commit()
    return rows


async def test_audit_perf_probe(client, db, make_actor, monkeypatch):
    viewer = await make_actor("buyer", name="Viewer", company_name="Viewer Co", city="Indore")
    sellers = [
        await make_actor("seller", name=f"Seller {k}", company_name=f"Seller Co {k}")
        for k in range(20)
    ]
    owner_ids = [uuid.UUID(s.id) for s in sellers]

    # ---------------- 2,000 RFQs ----------------
    await _bulk_rfqs(db, owner_ids, 0, 2000)

    for size in ("2k rfqs",):
        await timed("catalog newest (no q) limit 24", size,
                    lambda: viewer.get("/marketplace/catalog?limit=24"))
        await timed("catalog q='usb type-c cable'", size,
                    lambda: viewer.get("/marketplace/catalog", params={"q": "usb type-c cable"}))
        await timed("catalog q='rice' sort=trust_desc", size,
                    lambda: viewer.get("/marketplace/catalog", params={"q": "rice", "sort_by": "trust_desc"}))
        await timed("catalog limit=100", size,
                    lambda: viewer.get("/marketplace/catalog?limit=100"))
        await timed("categories summary q='cable'", size,
                    lambda: viewer.get("/marketplace/categories", params={"q": "cable"}))

    # radius filter correctness (post-pagination filtering)
    r = await viewer.get("/marketplace/catalog", params={"lat": 22.7196, "lon": 75.8577, "radius_km": 50, "limit": 24})
    body = r.json()
    print(f"[PERF] radius_km=50 around Indore: items={len(body['items'])} total={body['total']} "
          f"(expected ~{2000 // 5} Indore rows; page of 24 should be full)")
    r2 = await viewer.get("/marketplace/catalog", params={"lat": 22.7196, "lon": 75.8577, "radius_km": 50, "limit": 24, "offset": 24})
    print(f"[PERF] radius page2 items={len(r2.json()['items'])}")

    # Matching: SQL fallback path (conftest pins embed_one -> None)
    buyer_rfq = (await viewer.post("/rfqs", json=rfq_body("buyer", title="Need 6000 red Type-C cables"))).json()
    rid = buyer_rfq["id"]
    m = await timed("match run (SQL fallback, pool 200)", "2k rfqs",
                    lambda: viewer.post(f"/rfqs/{rid}/matches?limit=10"))
    print(f"[PERF] match total={m.json().get('total')}")
    await timed("match run page 2 (offset=10)", "2k rfqs",
                lambda: viewer.post(f"/rfqs/{rid}/matches?limit=10&offset=10"))

    # Matching: vector path with mocked embed/qdrant returning 200 ids
    all_ids = [row[0] for row in (await db.execute(text("SELECT id FROM rfqs WHERE role='seller' LIMIT 200"))).all()]

    async def fake_embed(_t):
        return [0.0] * 8

    async def fake_search(vector, **kw):
        return [(i, 0.9 - k * 0.001) for k, i in enumerate(all_ids)]

    monkeypatch.setattr("services.match_service.embed_one", fake_embed)
    monkeypatch.setattr("services.qdrant_index.search", fake_search)
    await timed("match run (mock vector, 200 hits)", "2k rfqs",
                lambda: viewer.post(f"/rfqs/{rid}/matches?limit=10"))

    # event-loop blocking during match scoring
    async def loop_lag(coro_fn):
        gaps = []
        stop = False

        async def ticker():
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.002)
                now = time.perf_counter()
                gaps.append((now - last) * 1000)
                last = now

        t = asyncio.create_task(ticker())
        await coro_fn()
        stop = True
        await t
        return max(gaps) if gaps else 0

    lag = await loop_lag(lambda: viewer.post(f"/rfqs/{rid}/matches?limit=10"))
    print(f"[PERF] max event-loop stall during match run: {lag:.1f} ms")

    rows_ms = (await db.execute(text("SELECT count(*) FROM match_searches"))).scalar()
    rows_mr = (await db.execute(text("SELECT count(*) FROM match_results"))).scalar()
    print(f"[PERF] after ~12 match runs: match_searches={rows_ms} match_results={rows_mr}")

    # Dashboard
    await timed("dashboard stats", "2k rfqs", lambda: viewer.get("/dashboard/stats"))
    await timed("dashboard activity", "2k rfqs", lambda: viewer.get("/dashboard/activity"))
    await timed("notifications unread-count", "-", lambda: viewer.get("/notifications/unread-count"))
    await timed("auth/me", "-", lambda: viewer.get("/auth/me"))

    # Connections list with messages (eager-load all messages to count them)
    seller_rfq_ids = [row[0] for row in (await db.execute(text(
        "SELECT DISTINCT ON (user_id) id FROM rfqs WHERE role='seller'"))).all()]
    seller_rfq_ids = [row[0] for row in (await db.execute(text(
        "SELECT id FROM rfqs WHERE role='seller' LIMIT 50"))).all()]
    owner_of = dict((await db.execute(text("SELECT id, user_id FROM rfqs WHERE role='seller'"))).all())
    conns = []
    vid = uuid.UUID(viewer.id)
    for rfq_id in seller_rfq_ids:
        conns.append(dict(id=uuid.uuid4(), sender_id=vid, receiver_id=owner_of[rfq_id], rfq_id=rfq_id,
                          status=ConnectionStatus.ACCEPTED))
    await db.execute(insert(Connection), conns)
    now = datetime.now(UTC)
    msgs = []
    for c in conns:
        for k in range(100):
            msgs.append(dict(id=uuid.uuid4(), connection_id=c["id"],
                             sender_id=vid if k % 2 else c["receiver_id"],
                             content=("Lorem ipsum negotiation message about price and incoterms " * 4),
                             created_at=now - timedelta(minutes=k), updated_at=now))
    for chunk in range(0, len(msgs), 1000):
        await db.execute(insert(ConnectionMessage), msgs[chunk: chunk + 1000])
    await db.commit()

    resp = await timed("GET /connections (50 conns x 100 msgs)", "5k messages",
                       lambda: viewer.get("/connections"))
    print(f"[PERF] /connections response bytes={len(resp.content)} (messages loaded only to count)")
    cid = conns[0]["id"]
    resp = await timed("GET /connections/{id}/messages (100 msgs)", "100 msgs",
                       lambda: viewer.get(f"/connections/{cid}/messages"))
    await timed("dashboard stats (w/ 5k messages)", "5k messages", lambda: viewer.get("/dashboard/stats"))

    # Concurrency: 40 parallel catalog requests against a default pool (5+10)
    t0 = time.perf_counter()
    rs = await asyncio.gather(*[viewer.get("/marketplace/catalog", params={"q": "cable"}) for _ in range(40)],
                              return_exceptions=True)
    dt = (time.perf_counter() - t0) * 1000
    errs = sum(1 for x in rs if isinstance(x, Exception) or x.status_code != 200)
    print(f"[PERF] 40 concurrent catalog q=cable: {dt:.0f} ms total, errors={errs}, pool={engine.pool.status()}")

    # EXPLAIN on search predicates
    async with engine.connect() as conn:
        for label, sql in [
            ("regex search", "EXPLAIN ANALYZE SELECT count(*) FROM rfqs WHERE status='active' AND (title ~* '\\mcable' OR search_text ~* '\\mcable')"),
            ("ilike city", "EXPLAIN ANALYZE SELECT count(*) FROM rfqs WHERE location_city ILIKE '%indore%'"),
            ("newest page", "EXPLAIN ANALYZE SELECT id FROM rfqs WHERE status='active' AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC LIMIT 24"),
        ]:
            plan = [r[0] for r in (await conn.execute(text(sql))).all()]
            print(f"[PLAN] {label}: " + " / ".join(p.strip() for p in plan[:4]))

    # ---------------- scale to 20,000 RFQs ----------------
    await _bulk_rfqs(db, owner_ids, 2000, 18000)
    size = "20k rfqs"
    await timed("catalog newest (no q) limit 24", size, lambda: viewer.get("/marketplace/catalog?limit=24"))
    await timed("catalog q='usb type-c cable'", size,
                lambda: viewer.get("/marketplace/catalog", params={"q": "usb type-c cable"}))
    await timed("catalog q='rice' sort=trust_desc", size,
                lambda: viewer.get("/marketplace/catalog", params={"q": "rice", "sort_by": "trust_desc"}))
    await timed("catalog q='zzzz-nomatch'", size,
                lambda: viewer.get("/marketplace/catalog", params={"q": "zzzznomatch"}))
    await timed("categories summary q='cable'", size,
                lambda: viewer.get("/marketplace/categories", params={"q": "cable"}))
    monkeypatch.setattr("services.match_service.embed_one", lambda _t: asyncio.sleep(0, None))
    await timed("match run (SQL fallback, pool 200)", size,
                lambda: viewer.post(f"/rfqs/{rid}/matches?limit=10"))
    async with engine.connect() as conn:
        plan = [r[0] for r in (await conn.execute(text(
            "EXPLAIN ANALYZE SELECT count(*) FROM rfqs WHERE status='active' AND (title ~* '\\mcable' OR search_text ~* '\\mcable')"))).all()]
        print("[PLAN] regex 20k: " + " / ".join(p.strip() for p in plan[:4]))

    # PDF generation blocking the loop
    print("\n[PERF] SUMMARY")
    for row in RESULTS:
        print("[PERF]", " | ".join(str(x) for x in row))


async def test_audit_pdf_blocking(accepted_pair):
    buyer, seller, conn_id, _listing = accepted_pair
    q = (await seller.post(f"/connections/{conn_id}/quotes", json={
        "unit_price": 250.0, "currency": "INR", "quantity": 500.0, "quantity_unit": "pcs",
        "lead_time_days": 7, "incoterms": "CIF", "payment_terms": "Escrow", "valid_days": 14,
        "notes": "x"})).json()
    await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/accept")
    gaps = []
    stop = False

    async def ticker():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            gaps.append((now - last) * 1000)
            last = now

    t = asyncio.create_task(ticker())
    t0 = time.perf_counter()
    for _ in range(5):
        r = await buyer.get(f"/connections/{conn_id}/quotes/{q['id']}/po-pdf")
        assert r.status_code == 200
    dt = (time.perf_counter() - t0) * 1000 / 5
    stop = True
    await t
    print(f"[PERF] PO PDF: {dt:.1f} ms/request, max event-loop stall {max(gaps):.1f} ms, bytes={len(r.content)}")

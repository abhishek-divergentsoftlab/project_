"""AUDIT PROBES for the matching engine. Failing tests here document defects.

Each docstring states what the test proves. Run only with an isolated DB:
TEST_DB_NAME=marketplace_audit_matching .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_zz_audit_matching.py
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from models.enums import RFQRole, RFQStatus
from services import match_scoring, match_service, query_extractor
from tests.conftest import rfq_body


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def fake_rfq(role="buyer", *, title, category="Electronics", product=None,
             attrs=None, price=None, currency="INR", qty=None, unit=None,
             city="Indore", state="Madhya Pradesh", country="India",
             lat=22.7196, lng=75.8577, deadline=None, tags=None, text=None):
    from services.rfq_indexing import build_search_tags, build_search_text
    details = {"name": product} if product else {}
    details.update(attrs or {})
    obj = SimpleNamespace(
        id=uuid.uuid4(), role=RFQRole(role), title=title, category=category,
        description=None, product_details=details,
        price_amount=Decimal(str(price)) if price is not None else None,
        price_currency=currency, price_per_unit=None,
        quantity_value=Decimal(str(qty)) if qty is not None else None,
        quantity_unit=unit, location_city=city, location_state=state,
        location_country=country,
        latitude=Decimal(str(lat)) if lat is not None else None,
        longitude=Decimal(str(lng)) if lng is not None else None,
        deadline_at=deadline, user=None, created_at=datetime.now(UTC),
    )
    obj.search_tags = tags if tags is not None else build_search_tags(obj)
    obj.search_text = text if text is not None else build_search_text(obj)
    return obj


async def _matches(actor, rfq_id, **params):
    r = await actor.post(f"/rfqs/{rfq_id}/matches", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _by_title(body):
    return {r["title"]: r for r in body["results"]}


# --------------------------------------------------------------------------
# 1. Units / price normalisation
# --------------------------------------------------------------------------

async def test_price_per_unit_kg_vs_tonne_not_normalised(make_actor, make_rfq):
    """PROVES: price_score ignores price_per_unit. Buyer wants <=30 INR/kg; seller
    asks 28,000 INR/tonne (=28/kg, cheaper). Expected price score 1.0."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer, title="Need 10 tonnes basmati rice", category="Agriculture",
                          product="basmati rice", quantity=(10, "tonne"), price=(30, "INR", "kg"))
    await make_rfq(seller, role="seller", title="Basmati rice 20 tonnes", category="Agriculture",
                   product="basmati rice", quantity=(20, "tonne"), price=(28000, "INR", "tonne"))
    body = await _matches(buyer, mine["id"])
    assert body["results"], body
    price = body["results"][0]["score"]["price"]
    assert price == 1.0, f"price score {price} for a cheaper per-kg offer"


def test_quantity_metric_ton_alias():
    """PROVES: 'metric tons' is a synonym in normalise_unit but not a mass unit,
    so 10 metric tons vs 10,000 kg is 'incomparable' (None) instead of 1.0."""
    s = match_scoring.quantity_score(Decimal(10), "metric tons", Decimal(10000), "kg")
    assert s == 1.0, s


def test_quantity_dozen_vs_pcs():
    """PROVES: count multipliers (dozen) unsupported: 24 pcs needed vs 2 dozen offered."""
    s = match_scoring.quantity_score(Decimal(24), "pcs", Decimal(2), "dozen")
    assert s == 1.0, s


def test_quantity_units_with_punctuation():
    """PROVES: 'Kgs.' (common user typing) is not recognised: 500 kg vs 500 'Kgs.'."""
    s = match_scoring.quantity_score(Decimal(500), "kg", Decimal(500), "Kgs.")
    assert s == 1.0, s


def test_zero_price_seller_gets_full_price_marks():
    """PROVES: an ask of 0 (placeholder / 'price on request') scores 1.0 on price,
    beating every real offer. Expected None (unscored)."""
    s = match_scoring.price_score(Decimal(100), Decimal(0), "INR", "INR")
    assert s is None, s


def test_price_currency_missing_on_one_side_compared_raw():
    """PROVES: when one side has no currency the amounts are compared raw
    (100 of ??? vs 90 USD -> 1.0). Expected None."""
    s = match_scoring.price_score(Decimal(100), Decimal(90), None, "USD")
    assert s is None, s


# --------------------------------------------------------------------------
# 2. Direct-search parsing (query_extractor)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("msg,qty,unit", [
    ("need 5 lakh pcs usb cable", Decimal(500000), "pcs"),
    ("need 1.5k pcs usb cables", Decimal(1500), "pcs"),
    ("need 2 dozen chairs", Decimal(24), None),
])
def test_extractor_indian_and_shorthand_multipliers(msg, qty, unit):
    """PROVES: lakh / k / dozen multipliers are not parsed; qty is lost and the
    word leaks into the product name (e.g. product='lakh usb cable')."""
    r = query_extractor.extract(msg)
    assert r.quantity_value == qty, (r.quantity_value, r.product)
    for w in ("lakh", "1.5k", "dozen"):
        assert w not in (r.product or ""), r.product


@pytest.mark.parametrize("msg,expected_unit", [
    ("need 20 metric tons basmati rice", ("tonne", "tonnes", "ton", "tons", "mt", "metric tons")),
    ("chahiye 500 kilo gehun", ("kg", "kilo", "kilos")),
])
def test_extractor_unit_aliases(msg, expected_unit):
    """PROVES: 'metric tons' and 'kilo' are not in _UNITS: unit=None and the unit
    word pollutes the product ('metric basmati rice', 'kilo gehun')."""
    r = query_extractor.extract(msg)
    assert r.quantity_unit in expected_unit, (r.quantity_unit, r.product)


@pytest.mark.parametrize("msg", [
    "mujhe 50 quintal pyaz chahiye",
    "need 100 kg aloo",
    "looking for red chilli 200 kg",
])
def test_extractor_hinglish_and_spelling_category(msg):
    """PROVES: category hints are an English keyword list; Hinglish (pyaz, aloo)
    and the common 'chilli' spelling get no category."""
    r = query_extractor.extract(msg)
    assert r.category == "Agriculture", (r.category, r.product)


def test_extractor_unknown_city_leaks_into_product():
    """PROVES: a city missing from the fixed gazetteer (Bhiwandi) becomes part
    of the product name, and the 'c' of Type-C is dropped from it."""
    r = query_extractor.extract("need 1000 pcs type c cable in Bhiwandi")
    assert "bhiwandi" not in (r.product or "").lower(), r.product


# --------------------------------------------------------------------------
# 3. Relevance / product gating
# --------------------------------------------------------------------------

def test_singularise_oes_plural():
    """PROVES: singularise('tomatoes') -> 'tomatoe' != 'tomato' (also potatoes,
    mangoes), which then trips the 'distinct commodity' drop."""
    assert match_scoring.singularise("tomatoes") == match_scoring.singularise("tomato")


@pytest.mark.parametrize("buyer_product,seller_product", [
    ("tomato", "tomato"),  # control: should PASS
    ("tomatoes", "tomato"),
    ("pyaz", "onion"),
    ("tamatar", "tomato"),
])
async def test_plural_and_hinglish_product_matches(make_actor, make_rfq, buyer_product, seller_product):
    """PROVES: in the SQL/lexical path a buyer asking for 'tomatoes' / 'pyaz' never
    sees a seller of 'tomato' / 'onion' (distinct-commodity gate drops it)."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer, title=f"Need 500 kg {buyer_product}", category="Agriculture",
                          product=buyer_product, quantity=(500, "kg"), price=(30, "INR", "kg"))
    await make_rfq(seller, role="seller", title=f"Fresh {seller_product} 2 tonnes", category="Agriculture",
                   product=seller_product, quantity=(2000, "kg"), price=(25, "INR", "kg"))
    body = await _matches(buyer, mine["id"])
    assert body["total"] >= 1, body


async def test_cross_category_leak(make_actor, make_rfq):
    """PROVES: category is computed but never used in the total; an Electronics
    'red chilli LED string light' is returned for a spice buyer."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer, title="Need red chilli", category="Agriculture",
                          product="red chilli", quantity=(500, "kg"), price=(200, "INR", "kg"))
    await make_rfq(seller, role="seller", title="Red chilli LED string light", category="Electronics",
                   product="red chilli led string light", quantity=(5000, "pcs"), price=(150, "INR", "pcs"))
    body = await _matches(buyer, mine["id"])
    leaked = [r for r in body["results"] if r["category"] == "Electronics"]
    assert not leaked or leaked[0]["score"]["total"] < 0.3, leaked[0]["score"] if leaked else None


def test_category_weight_unused_in_total():
    """PROVES: two candidates identical except category (tags pinned equal)
    get the same total, although docs say category carries 10% weight."""
    req = fake_rfq("buyer", title="Need usb cable", product="usb cable", price=100, qty=100, unit="pcs")
    tags = list(req.search_tags)
    same = fake_rfq("seller", title="Need usb cable", product="usb cable", price=100, qty=100, unit="pcs",
                    category="Electronics", tags=tags, text=req.search_text)
    other = fake_rfq("seller", title="Need usb cable", product="usb cable", price=100, qty=100, unit="pcs",
                     category="Furniture", tags=tags, text=req.search_text)
    # Fix relevance via the vector path so only the category differs.
    s1, _ = match_service._score(req, same, similarity=0.9)
    s2, _ = match_service._score(req, other, similarity=0.9)
    assert s1.category != s2.category
    assert s1.total > s2.total, (s1.total, s2.total)


def test_quantity_in_title_treated_as_critical_spec():
    """PROVES: the Pillar-2 'critical token' check treats any alnum token such as
    '500kg' in the title as a spec; a perfect basmati seller loses 60% relevance."""
    base = dict(category="Agriculture", product="basmati rice", qty=500, unit="kg", price=90)
    req_plain = fake_rfq("buyer", title="Need basmati rice", **base)
    req_qty = fake_rfq("buyer", title="Need 500kg basmati rice", **base)
    cand = fake_rfq("seller", title="Basmati rice available", category="Agriculture",
                    product="basmati rice", qty=10000, unit="kg", price=85)
    a, _ = match_service._score(req_plain, cand)
    b, _ = match_service._score(req_qty, cand)
    assert b.relevance >= a.relevance * 0.9, (a.relevance, b.relevance)


def test_overlap_rewards_underspecified_candidate():
    """PROVES: _overlap divides by the smaller set; a one-word listing 'cable'
    gets full relevance to a detailed 'hdmi 2.1 braided 8k cable' request."""
    r = match_scoring.relevance_score(
        ["hdmi braided 8k cable"], "Electronics hdmi braided 8k cable",
        ["cable"], "cable",
    )
    assert r < 0.6, r


# --------------------------------------------------------------------------
# 4. Deadline / location / preferences
# --------------------------------------------------------------------------

async def test_deadline_direction_rewards_late_seller(make_actor, make_rfq):
    """PROVES: deadline_score gives 1.0 to a seller whose date is AFTER the buyer's
    need-by date and penalises one that is earlier. Buyer needs in 7 days; seller
    A ready in 2 days should score >= seller B ready in 30 days."""
    buyer = await make_actor("buyer")
    s1 = await make_actor("seller")
    s2 = await make_actor("seller")
    mine = await make_rfq(buyer, deadline_days=7)
    await make_rfq(s1, role="seller", title="Red Type-C cables fast dispatch", deadline_days=2)
    await make_rfq(s2, role="seller", title="Red Type-C cables slow dispatch", deadline_days=30)
    res = _by_title(await _matches(buyer, mine["id"]))
    fast = res["Red Type-C cables fast dispatch"]["score"]["deadline"]
    slow = res["Red Type-C cables slow dispatch"]["score"]["deadline"]
    assert fast >= slow, (fast, slow)


def test_same_city_name_different_country():
    """PROVES: administrative match on city name alone: Hyderabad (India) vs
    Hyderabad (Pakistan) without coords scores 1.0 (local)."""
    s = match_scoring.location_score(
        {"city": "Hyderabad", "state": "Telangana", "country": "India"},
        {"city": "Hyderabad", "state": "Sindh", "country": "Pakistan"},
    )
    assert s is not None and s <= 0.3, s


async def test_negative_custom_weights_break_matching(make_actor, make_rfq):
    """PROVES: profile matching_preferences.weights accept any number; negative
    weights push the blended utility above 1 and MatchScore(total<=1) blows up."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    r = await buyer.patch("/users/me/profile", json={"matching_preferences": {
        "weights": {"price": -1, "location": 2, "quantity": 0, "deadline": 0}}})
    assert r.status_code == 200, r.text
    mine = await make_rfq(buyer, price=(100, "INR", "pcs"))
    await make_rfq(seller, role="seller", title="Red Type-C cables", price=(500, "INR", "pcs"))
    try:
        resp = await buyer.post(f"/rfqs/{mine['id']}/matches")
        status = resp.status_code
    except Exception as exc:  # ASGITransport re-raises app errors
        status = f"exception {type(exc).__name__}: {str(exc)[:120]}"
    assert status == 200, status


def test_quality_first_preset_does_not_change_spec_weight():
    """PROVES: presets only re-weight commercial dims; 'relevance'/'attributes'
    weights in quality_first are never read. A spec-mismatched candidate with
    perfect commercials scores identically under quality_first and price_first."""
    req = fake_rfq("buyer", title="Need usb cable", product="usb cable",
                   attrs={"color": "black"}, price=100, qty=100, unit="pcs")
    cand = fake_rfq("seller", title="Need usb cable", product="usb cable",
                    attrs={"color": "white"}, price=90, qty=200, unit="pcs")
    q, _ = match_service._score(req, cand, matching_preferences={"preset": "quality_first"})
    p, _ = match_service._score(req, cand, matching_preferences={"preset": "price_first"})
    assert q.total < p.total, (q.total, p.total)


# --------------------------------------------------------------------------
# 5. Lifecycle / leakage
# --------------------------------------------------------------------------

async def test_expired_candidate_excluded(make_actor, make_rfq, db):
    """PROVES (sanity): a seller listing past expires_at is not matched."""
    from models.rfq import RFQ
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer)
    theirs = await make_rfq(seller, role="seller", title="Red Type-C cables")
    row = await db.get(RFQ, uuid.UUID(theirs["id"]))
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db.commit()
    assert (await _matches(buyer, mine["id"]))["total"] == 0


async def test_closed_own_rfq_still_runs_matching(make_actor, make_rfq):
    """PROVES: only DRAFT is refused; a CLOSED requester RFQ still searches and
    writes MatchSearch rows. Expected 409."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer)
    await make_rfq(seller, role="seller", title="Red Type-C cables")
    await buyer.post(f"/rfqs/{mine['id']}/close")
    r = await buyer.post(f"/rfqs/{mine['id']}/matches")
    assert r.status_code == 409, (r.status_code, r.json().get("total"))


async def test_email_visible_before_connection(make_actor, make_rfq):
    """PROVES: counterparty.email is returned for every match with no connection,
    contradicting schemas/match.py ('contact details only once accepted')."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer)
    await make_rfq(seller, role="seller", title="Red Type-C cables")
    body = await _matches(buyer, mine["id"])
    assert body["results"][0]["counterparty"]["email"] is None, body["results"][0]["counterparty"]["email"]


async def test_huge_quantity_rejected_cleanly(make_actor):
    """PROVES: Quantity has no upper bound but the column is Numeric(18,4);
    1e15 should be a 422, not a 500/DB error."""
    buyer = await make_actor("buyer")
    try:
        r = await buyer.post("/rfqs", json=rfq_body(quantity=(1e15, "pcs")))
        status = r.status_code
    except Exception as exc:
        status = f"exception {type(exc).__name__}"
    assert status == 422, status


# --------------------------------------------------------------------------
# 6. Qdrant / Ollama degradation
# --------------------------------------------------------------------------

async def _vector_setup(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer)
    await make_rfq(seller, role="seller", title="Supplying red Type-C cables")
    return buyer, mine


async def test_qdrant_down_falls_back_to_sql(make_actor, make_rfq, monkeypatch):
    """PROVES (sanity): search() -> None triggers SQL fallback."""
    buyer, mine = await _vector_setup(make_actor, make_rfq)

    async def vec(_t):
        return [0.1] * 8

    async def down(*a, **k):
        return None
    monkeypatch.setattr("services.match_service.embed_one", vec)
    monkeypatch.setattr("services.qdrant_index.search", down)
    assert (await _matches(buyer, mine["id"]))["total"] >= 1


async def test_unindexed_listing_invisible_when_qdrant_up(make_actor, make_rfq, monkeypatch):
    """PROVES: if Qdrant answers [] (listing created while Ollama was down, so it
    was never indexed) there is no SQL fallback: a real match exists in Postgres
    but total == 0."""
    buyer, mine = await _vector_setup(make_actor, make_rfq)

    async def vec(_t):
        return [0.1] * 8

    async def empty(*a, **k):
        return []
    monkeypatch.setattr("services.match_service.embed_one", vec)
    monkeypatch.setattr("services.qdrant_index.search", empty)
    assert (await _matches(buyer, mine["id"]))["total"] >= 1


async def test_stale_vector_ids_no_fallback(make_actor, make_rfq, monkeypatch):
    """PROVES: Qdrant returning only stale ids (deleted/closed rows) yields an
    empty page instead of falling back to SQL."""
    buyer, mine = await _vector_setup(make_actor, make_rfq)

    async def vec(_t):
        return [0.1] * 8

    async def stale(*a, **k):
        return [(uuid.uuid4(), 0.95), (uuid.uuid4(), 0.9)]
    monkeypatch.setattr("services.match_service.embed_one", vec)
    monkeypatch.setattr("services.qdrant_index.search", stale)
    assert (await _matches(buyer, mine["id"]))["total"] >= 1


async def test_slow_embedding_has_no_matching_timeout(make_actor, make_rfq, monkeypatch):
    """PROVES: matching awaits the embedding with no local deadline (config
    EMBEDDING_TIMEOUT_SECONDS=180). A 3s stall should degrade to SQL in <1.5s."""
    buyer, mine = await _vector_setup(make_actor, make_rfq)

    async def slow(_t):
        await asyncio.sleep(3)
        return None
    monkeypatch.setattr("services.match_service.embed_one", slow)
    t0 = time.perf_counter()
    await _matches(buyer, mine["id"])
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.5, f"match took {elapsed:.2f}s"


# --------------------------------------------------------------------------
# 7. Scale: pool cap / recall / pagination
# --------------------------------------------------------------------------

async def _bulk_sellers(db, user_id, n, *, title, product, category="Electronics", age_days=0):
    from models.rfq import RFQ
    from services.rfq_indexing import refresh_index_fields
    now = datetime.now(UTC)
    for i in range(n):
        row = RFQ(user_id=uuid.UUID(user_id), role=RFQRole.SELLER, status=RFQStatus.ACTIVE,
                  category=category, title=f"{title} #{i}", product_details={"name": product},
                  quantity_value=Decimal(10000), quantity_unit="pcs",
                  price_amount=Decimal(150), price_currency="INR", price_per_unit="pcs",
                  location_city="Indore", location_state="Madhya Pradesh", location_country="India",
                  latitude=Decimal("22.7196"), longitude=Decimal("75.8577"),
                  expires_at=now + timedelta(days=30))
        refresh_index_fields(row)
        row.created_at = now - timedelta(days=age_days, seconds=i)
        db.add(row)
    await db.commit()


async def test_sql_pool_recall_cap(make_actor, make_rfq, db):
    """PROVES: the SQL fallback takes the 200 newest rows sharing ANY tag (e.g. the
    category tag). 200 newer HDMI listings push an older exact Type-C seller out
    of the pool, so it is never shown."""
    buyer = await make_actor("buyer")
    noise = await make_actor("seller")
    exact = await make_actor("seller")
    mine = await make_rfq(buyer)
    await _bulk_sellers(db, exact.id, 1, title="Red usb type-c cable", product="usb type-c cable", age_days=20)
    control = await _matches(buyer, mine["id"], limit=50)
    assert any(r["title"].startswith("Red usb type-c cable") for r in control["results"]), "control failed"
    await _bulk_sellers(db, noise.id, 200, title="HDMI 2.1 cable", product="hdmi cable")
    body = await _matches(buyer, mine["id"], limit=50)
    titles = [r["title"] for r in body["results"]]
    assert any(t.startswith("Red usb type-c cable") for t in titles), titles[:5]


async def test_total_capped_by_candidate_pool(make_actor, make_rfq, db):
    """PROVES: total and pagination are bounded by CANDIDATE_POOL=200: with 230
    relevant sellers, total==200 and offset=200 returns nothing."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    mine = await make_rfq(buyer)
    await _bulk_sellers(db, seller.id, 230, title="Red usb type-c cable", product="usb type-c cable")
    t0 = time.perf_counter()
    body = await _matches(buyer, mine["id"], limit=10)
    elapsed = time.perf_counter() - t0
    print(f"match over 230 candidates: {elapsed:.2f}s total={body['total']}")
    assert body["total"] == 230, body["total"]

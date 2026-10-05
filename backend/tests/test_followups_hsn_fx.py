"""Strict currency validation, deterministic HSN in scoring, and FX refresh behaviour."""

import json
import uuid
from decimal import Decimal

import pytest

from models.rfq import RFQ
from services import currency, hsn_service, match_scoring, query_grounding
from services import scheduler


# --- ISO 4217 --------------------------------------------------------------

@pytest.mark.parametrize("code", ["AED", "JPY", "INR", "usd"])
def test_real_iso_currencies_accepted(code):
    assert query_grounding.valid_currency(code) == code.upper()


@pytest.mark.parametrize("code", ["XYZ", "ABC", "US", "RUPEES", None])
def test_invented_currencies_rejected(code):
    assert query_grounding.valid_currency(code) is None


# --- HSN -------------------------------------------------------------------

def _no_network(*a, **k):
    raise AssertionError("network call during deterministic HSN/scoring")


def test_scoring_makes_no_network_call(monkeypatch):
    import urllib.request

    import httpx

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(httpx.AsyncClient, "post", _no_network)
    monkeypatch.setattr(httpx.Client, "post", _no_network)
    hsn_service.predict_hsn_from_text.cache_clear()
    match_scoring._dynamic_semantic_similarity.cache_clear()
    assert hsn_service.predict_hsn_from_text("zzqx unknown gadget") is None
    assert hsn_service.predict_hsn_from_text("hair spray") == "330530"
    match_scoring._dynamic_semantic_similarity("zzqx widget", "qqvv gizmo")


def test_stored_hsn_preferred_over_prediction():
    assert hsn_service.resolve_hsn({"hsn_code": "1006"}, "hair spray") == "1006"
    assert hsn_service.resolve_hsn({"hsn": "8544.42"}, "rice") == "854442"
    assert hsn_service.resolve_hsn({}, "hair spray") == "330530"
    assert hsn_service.resolve_hsn(None, "zzqx") is None


def _fake_ollama(monkeypatch, hsn_value):
    import httpx

    class Resp:
        def raise_for_status(self): ...
        def json(self):
            return {"response": json.dumps({"hsn_code": hsn_value})}

    class Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return Resp()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(hsn_service.settings, "HSN_AI_ENABLED", True)
    hsn_service._ai_hsn_cache.clear()


async def test_ai_classifier_drops_invalid_chapter(monkeypatch):
    _fake_ollama(monkeypatch, "9912")  # chapter 99 does not exist
    assert await hsn_service.classify_hsn_with_ai("mystery thing") is None
    _fake_ollama(monkeypatch, "8517")
    assert await hsn_service.classify_hsn_with_ai("mobile phone x") == "8517"


async def test_ai_classifier_disabled_and_never_raises(monkeypatch):
    assert await hsn_service.classify_hsn_with_ai("anything") is None  # disabled in tests
    monkeypatch.setattr(hsn_service.settings, "HSN_AI_ENABLED", True)
    hsn_service._ai_hsn_cache.clear()
    monkeypatch.setattr(hsn_service.settings, "OLLAMA_BASE_URL", "http://127.0.0.1:1")
    assert await hsn_service.classify_hsn_with_ai("anything") is None


async def test_create_stores_ai_hsn_with_source(make_actor, monkeypatch):
    from services import rfq_service

    async def fake(text):
        return "8517"

    monkeypatch.setattr(hsn_service, "classify_hsn_with_ai", fake)
    rfq = RFQ(title="Smartphone lot", description="x", product_details={"name": "phone"})
    await rfq_service._ensure_hsn(rfq)
    assert rfq.product_details["hsn_code"] == "8517"
    assert rfq.product_details["hsn_source"] == "ai"
    assert rfq.product_details["name"] == "phone"

    # a user-supplied HSN is never replaced
    rfq2 = RFQ(title="Smartphone lot", product_details={"hsn_code": "1006"})
    await rfq_service._ensure_hsn(rfq2)
    assert rfq2.product_details == {"hsn_code": "1006"}


async def test_create_via_api_stores_ai_hsn(make_actor, monkeypatch, db):
    from tests.conftest import rfq_body

    async def fake(text):
        return "8517"

    monkeypatch.setattr(hsn_service, "classify_hsn_with_ai", fake)
    owner = await make_actor("seller")
    res = await owner.post("/rfqs", json=rfq_body(role="seller"))
    assert res.status_code in (200, 201), res.text
    row = await db.get(RFQ, uuid.UUID(res.json()["id"]))
    assert row.product_details["hsn_code"] == "8517"
    assert row.product_details["hsn_source"] == "ai"


async def test_failed_classifier_stores_nothing(monkeypatch):
    from services import rfq_service

    async def boom(text):
        raise RuntimeError("down")

    monkeypatch.setattr(hsn_service, "classify_hsn_with_ai", boom)
    rfq = RFQ(title="Thing", product_details={})
    await rfq_service._ensure_hsn(rfq)
    assert rfq.product_details == {}


# --- FX refresh ------------------------------------------------------------

def _fake_frankfurter(monkeypatch, payload=None, fail=False):
    import httpx

    class Resp:
        def raise_for_status(self): ...
        def json(self): return payload

    class Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k):
            if fail:
                raise httpx.ConnectError("offline")
            return Resp()

    monkeypatch.setattr(httpx, "AsyncClient", Client)


async def test_scheduler_refresh_updates_rates_and_provenance(monkeypatch):
    currency.reset_rates()
    try:
        before = currency.get_rates_info()
        assert before["source"] == "static"
        _fake_frankfurter(monkeypatch, {"rates": {"INR": 91.25, "EUR": 0.9, "BAD": -1, "XXX": "nan"}})
        assert await scheduler.refresh_currency_rates_job(None) == 1
        after = currency.get_rates_info()
        assert after["rates"]["INR"] == 91.25
        assert after["rates"]["INR"] != before["rates"]["INR"]
        assert after["source"] != "static"
        assert after["as_of"] != before["as_of"]
        assert "BAD" not in after["rates"]
    finally:
        currency.reset_rates()


async def test_failed_refresh_keeps_previous_rates(monkeypatch):
    currency.reset_rates()
    try:
        _fake_frankfurter(monkeypatch, {"rates": {"INR": 90.5}})
        await scheduler.refresh_currency_rates_job(None)
        good = currency.get_rates_info()
        _fake_frankfurter(monkeypatch, fail=True)
        assert await scheduler.refresh_currency_rates_job(None) == 0
        assert currency.get_rates_info() == good
        _fake_frankfurter(monkeypatch, {"rates": {"INR": -5}})
        assert await scheduler.refresh_currency_rates_job(None) == 0
        assert currency.get_rates_info() == good
    finally:
        currency.reset_rates()

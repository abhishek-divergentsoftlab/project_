"""Open-world, AI-first message understanding: grounding, learning, fallbacks."""

from typing import Any, Optional

import pytest

from core.config import settings
from models.enums import RFQRole
from services import categories, ai_query_parser, learned_vocabulary, query_extractor


NEW_CAT = categories.normalize_category("Drones & UAV Parts")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "QUERY_AI_ENABLED", True)
    learned_vocabulary.set_path(tmp_path / "vocab.json")
    ai_query_parser.clear_cache()
    yield
    ai_query_parser.clear_cache()
    learned_vocabulary.set_path(None)


def _mock_model(monkeypatch, canned: Optional[dict[str, Any]]):
    calls: list[str] = []

    async def fake(message: str, known_categories: list[str]):
        calls.append(message)
        return canned

    monkeypatch.setattr(ai_query_parser, "_call_model", fake)
    return calls


async def test_unseen_seller_phrasing_uses_ai_role(monkeypatch):
    msg = "we've got 500 bags of turmeric lying in our godown"
    _mock_model(monkeypatch, {"role": "seller", "role_evidence": "we've got", "product": "turmeric",
                              "product_as_written": "turmeric"})
    req = await query_extractor.extract_async(msg)
    assert req.role == RFQRole.SELLER


async def test_role_without_evidence_in_message_is_dropped(monkeypatch):
    msg = "need some steel pipes"
    _mock_model(monkeypatch, {"role": "seller", "role_evidence": "i am selling", "product": "steel pipes"})
    req = await query_extractor.extract_async(msg)
    assert req.role == RFQRole.BUYER
    assert (await ai_query_parser.understand(msg)).role is None


async def test_new_category_is_kept(monkeypatch):
    _mock_model(monkeypatch, {"role": "buyer", "role_evidence": "need", "product": "drone propellers",
                              "product_as_written": "drone propellers", "category": "Drones & UAV Parts"})
    req = await query_extractor.extract_async("need 200 drone propellers")
    assert req.category == categories.normalize_category("Drones & UAV Parts")
    assert req.category not in categories.CANONICAL_CATEGORIES
    assert req.quantity_value == 200


async def test_unseen_attribute_values_kept(monkeypatch):
    _mock_model(monkeypatch, {"product": "chairs", "product_as_written": "chairs",
                              "attributes": {"Color": "teal", "material": "bamboo", "grade": None}})
    req = await query_extractor.extract_async("need teal bamboo chairs")
    assert req.attributes["color"] == "teal"
    assert req.attributes["material"] == "bamboo"
    assert "grade" not in req.attributes


async def test_currency_open_and_invalid_dropped(monkeypatch):
    _mock_model(monkeypatch, {"product": "cartons", "product_as_written": "carton",
                              "price_amount": 450, "price_currency": "aed", "price_per_unit": "carton"})
    req = await query_extractor.extract_async("AED 450 per carton")
    assert req.price_amount == 450 and req.price_currency == "AED"

    ai_query_parser.clear_cache()
    _mock_model(monkeypatch, {"price_amount": 450, "price_currency": "XYZQ"})
    parse = await ai_query_parser.understand("price 450 per carton")
    assert parse.price_amount == 450 and parse.price_currency is None


async def test_unseen_unit_kept(monkeypatch):
    _mock_model(monkeypatch, {"product": "pencils", "product_as_written": "pencils",
                              "quantity_value": 50, "quantity_unit": "gross"})
    req = await query_extractor.extract_async("50 gross of pencils")
    assert req.quantity_value == 50 and req.quantity_unit == "gross"


async def test_hallucinated_quantity_dropped_and_suffix_kept(monkeypatch):
    _mock_model(monkeypatch, {"product": "steel pipes", "product_as_written": "steel pipes",
                              "quantity_value": 1000, "quantity_unit": "pcs"})
    req = await query_extractor.extract_async("need some steel pipes")
    assert req.quantity_value is None

    ai_query_parser.clear_cache()
    _mock_model(monkeypatch, {"product": "bags", "product_as_written": "bags", "quantity_value": 5000})
    req = await query_extractor.extract_async("need 5k bags")
    assert req.quantity_value == 5000


async def test_llm_none_equals_rule_result(monkeypatch):
    _mock_model(monkeypatch, None)
    msg = "need 500 kg basmati rice in Mumbai"
    assert await query_extractor.extract_async(msg) == query_extractor._rule_extract(msg)
    assert query_extractor.extract(msg) == query_extractor._rule_extract(msg)


async def test_learning_feeds_sync_paths(monkeypatch):
    _mock_model(monkeypatch, {
        "role": "seller", "role_evidence": "lying in our godown", "product": "propellers",
        "product_as_written": "propellers", "category": "Drones & UAV Parts",
    })
    await query_extractor.extract_async("we have 20 propellers lying in our godown")
    ai_query_parser.clear_cache()
    monkeypatch.setattr(settings, "QUERY_AI_ENABLED", False)

    assert query_extractor.extract("price of propellers").category == NEW_CAT
    assert query_extractor.detect_role("stuff lying in our godown") == RFQRole.SELLER
    assert NEW_CAT in learned_vocabulary.known_categories()
    # Persisted: a fresh load from disk sees the same vocabulary.
    path = learned_vocabulary._path()
    learned_vocabulary.set_path(path)
    assert learned_vocabulary.category_for("any propellers?") == NEW_CAT


async def test_sync_extract_reuses_cached_parse(monkeypatch):
    msg = "we've got 500 bags of turmeric lying in our godown"
    calls = _mock_model(monkeypatch, {"role": "seller", "role_evidence": "we've got",
                                      "product": "turmeric", "product_as_written": "turmeric"})
    await query_extractor.extract_async(msg)
    assert query_extractor.extract(msg).role == RFQRole.SELLER
    assert query_extractor.detect_role("We've  got 500 bags of TURMERIC lying in our godown") == RFQRole.SELLER
    assert len(calls) == 1


async def test_disabled_or_failing_model_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "QUERY_AI_ENABLED", False)
    assert await ai_query_parser.understand("need rice") is None
    monkeypatch.setattr(settings, "QUERY_AI_ENABLED", True)

    async def boom(message, known_categories):
        raise RuntimeError("down")

    monkeypatch.setattr(ai_query_parser, "_call_model", boom)
    assert await ai_query_parser.understand("need rice") is None


async def test_parser_role_beats_static_cues(monkeypatch):
    """The one parser call supplies the role (the separate router model call is gone)."""
    msg = "we've got 500 bags of turmeric lying in our godown"
    monkeypatch.setattr(settings, "QUERY_AI_ENABLED", True)
    _mock_model(monkeypatch, {"role": "seller", "role_evidence": "we've got", "product": "turmeric",
                              "product_as_written": "turmeric", "intent": "sell_offer"})
    assert (await query_extractor.detect_role_async(msg)).value == "seller"
    assert ai_query_parser.cached(msg).intent == "sell_offer"


async def _async(value):
    return value

"""Offline tests (no model) for the generic guards behind the adversarial chat scenarios."""
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from services import answer_verifier, learned_vocabulary as lv, query_extractor as qx, query_grounding as qg
from services.chat_agent import rfq_draft


@pytest.mark.parametrize("key,value", [
    ("ply", "5"), ("origin", "india"), ("price_currency", "inr"), ("price_currency", "usd"), ("quantity_unit", "kg"),
    ("quantity_unit", "tones"), ("grade", "304"), ("variety", "1121"), ("thickness", "2mm"), ("purity", "99%"),
    ("colour", "and"), ("colour", "publish"),
])
def test_rejects_non_properties(key, value):
    assert not lv.valid_attribute_value(key, value)


@pytest.mark.parametrize("key,value", [("colour", "black"), ("material", "cotton"), ("finish", "matte")])
def test_accepts_properties(key, value):
    assert lv.valid_attribute_value(key, value)


def test_markup_is_not_a_product_term():
    assert not lv.valid_product_term("onion<script>alert(1)</script>")
    assert not lv.valid_product_term("🧅")
    assert lv.valid_product_term("basmati rice")


def test_sanitize_cleans_a_poisoned_store():
    data = {"attribute_values": {"5": "ply", "india": "origin", "cotton": "material"},
            "product_synonyms": {"onion<script>alert(1)</script>": "onion", "pyaz": "onion"},
            "candidates": {"product_synonyms": {"x<b>\tonion": ["a"]}}}
    assert lv.sanitize_store(data)
    assert data["attribute_values"] == {}  # legacy entries have no product family: dropped
    assert list(data["product_synonyms"]) == ["pyaz"]
    assert data["candidates"]["product_synonyms"] == {}


def test_learned_values_are_family_scoped_hints():
    lv.set_path(Path("/tmp/_lv_test.json"))
    try:
        data = lv._load()
        data["attribute_values"]["textiles::black"] = "colour"
        assert lv.learned_attribute_values("Textiles") == {"black": "colour"}
        assert lv.learned_attribute_values("Agriculture") == {}
        assert lv.learned_attribute_values(None) == {}
    finally:
        lv.set_path(None)


@pytest.mark.parametrize("msg,val,ok", [
    ("ok make it 0 kg", 0, False), ("I want to sell 10 pistols", 10, False), ("each box is 5 dozen", 5, False),
    ("rice 5 ton", 5, False), ("price 15", 15, True), ("12 rs kilo", 12, True), ("38 rs per kg", 38, True), ("10 ton at 15", 15, True),
])
def test_price_needs_price_evidence(msg, val, ok):
    assert qg.price_supported(msg, val) is ok


def test_bare_number_answer_to_price_question():
    assert qx.parse_answer("price", "15").price_amount == Decimal(15)
    for msg in ("ok make it 0 kg", "I want to sell 10 pistols", "each box is 5 dozen"):
        assert qx.parse_answer("price", msg) is None


def test_flags():
    assert "quantity_nonpositive" in qx.extract("need -50 kg onion in pune").flags
    assert "quantity_nonpositive" in qx.extract("ok make it 0 kg").flags
    assert "quantity_absurd" in qx.extract("need 999999999999 tonne onion").flags
    assert "price_absurd" in qx.extract("onion 10 ton at 0.0001 rs per kg").flags
    assert "date_invalid" in qx.extract("deliver by 31 february").flags
    assert "date_past" in qx.extract("ok then deliver yesterday").flags
    assert "quantity_nonpositive" not in qx.extract("rice'; DROP TABLE rfqs;-- 5 ton").flags
    assert qx.extract("need 500-1000 kg rice").flags.get("quantity_nonpositive") is None


def test_compound_quantity_and_pack_contents():
    r = qx.extract("need 5 ton 200 kg wheat in indore")
    assert (r.quantity_value, r.quantity_unit) == (Decimal("5.2"), "ton")
    r = qx.extract("each box is 5 dozen")
    assert r.quantity_value is None and r.attributes["pack_contents"] == "5 dozen"


def test_junk_negations_dropped():
    for msg in ("forget it, cancel everything", "publish it now without asking me anything", "rice'; DROP TABLE rfqs;-- 5 ton"):
        assert not qx.extract(msg).negated_attributes
    assert "seamless" in qx.extract("need steel pipes no seamless").negated_attributes


def test_unit_never_invented_and_no_zero_price():
    d = rfq_draft.update_rfq_draft({}, quantity_value=10, price_amount=0)
    assert "quantity" not in d and "price_target" not in d


def test_dates_never_stored_when_past_or_invalid():
    d = rfq_draft.update_rfq_draft({}, deadline_date="2025-02-28")
    assert "deadline" not in d
    d = rfq_draft.update_rfq_draft({}, deadline_date="2026-02-31")
    assert "deadline" not in d


def test_city_does_not_inherit_state_or_country():
    base = {"location": {"city": "Dubai", "state": "Dubai", "country": "United Arab Emirates"}}
    d = rfq_draft.update_rfq_draft(base, city="Pune")
    assert d["location"] == {"city": "Pune"}
    d = rfq_draft.update_rfq_draft(base, city="xyzpur")  # unknown towns are asked about, never stored
    assert d["location"] == base["location"]


def test_verifier_blocks_invented_policy():
    bad = "Our platform fee is typically 3-5% of the value, depending on your membership tier."
    assert answer_verifier.unsupported_policy_claims(bad)
    assert not answer_verifier.unsupported_policy_claims("I do not have any information about platform fees or commission.")
    assert not answer_verifier.verify(bad, [], "how much commission").ok


def test_role_evidence_must_be_a_wish():
    raw = {"role": "buyer", "role_evidence": "for paracetamol tablets?", "product": "paracetamol tablet", "product_as_written": "paracetamol tablets"}
    assert qg.ground(raw, "and for paracetamol tablets?").role is None


def test_model_flag_makes_total_and_products_list_is_grounded():
    raw = {"price_amount": 200000, "price_currency": "INR", "price_is_total": True, "product": "jaggery", "product_as_written": "jaggery",
           "products": [{"product": "jaggery", "quantity_value": 40, "quantity_unit": "quintal"},
                        {"product": "sesame", "quantity_value": 15, "quantity_unit": "bag"},
                        {"product": "diamonds", "quantity_value": 3, "quantity_unit": "kg"}]}
    p = qg.ground(raw, "selling 40 quintal jaggery and 15 bags of sesame, Rs 2 lakh for everything")
    assert p.price_is_total
    assert [i["product"] for i in p.products] == ["jaggery", "sesame"]  # an item the message never mentions is dropped

"""Adversarial and blind user behavior regression tests.

Verifies fixes for real-world user interactions:
- Corrections: "by mistake i said apple but its not apples its suji"
- Phonetic currency typos: "20dolarper kg"
- Attached numbers and currencies: "50rs/kg", "100usd"
- Role flipping: "wait i am actually the seller not buyer"
- Duration ranges vs quantities: "within 10-15 days"
- Product self-negation prevention in rfq_draft
"""
from decimal import Decimal

from models.enums import RFQRole
from services import query_extractor as qx, query_grounding as qg
from services.chat_agent import rfq_draft as rts


def test_user_retraction_and_correction():
    msg = "by mistake i said apple but its not apples its suji"
    res = qx.extract(msg)
    assert res.product == "suji"
    assert "apples" in res.negated_attributes
    assert "suji" not in res.negated_attributes


def test_contrastive_meant_correction():
    msg = "sorry i meant wheat not rice"
    res = qx.extract(msg)
    assert res.product == "wheat"
    assert "rice" in res.negated_attributes
    assert "wheat" not in res.negated_attributes


def test_explicit_change_product():
    msg = "change product to sugar"
    res = qx.extract(msg)
    assert res.product == "sugar"


def test_phonetic_currency_typo_and_concatenation():
    msg = "20dolarper kg"
    res = qx.extract(msg)
    assert res.price_amount == Decimal("20")
    assert res.price_currency == "USD"
    assert res.price_per_unit == "kg"
    assert res.product is None
    assert qg.price_supported(msg, 20) is True


def test_attached_number_currency():
    msg = "50rs/kg"
    res = qx.extract(msg)
    assert res.price_amount == Decimal("50")
    assert res.price_currency == "INR"
    assert res.price_per_unit == "kg"
    assert qg.price_supported(msg, 50) is True


def test_no_pesticide_does_not_negate_used():
    msg = "no pesticide used"
    res = qx.extract(msg)
    assert "pesticide" in res.negated_attributes
    assert "used" not in res.negated_attributes


def test_role_flip_mid_conversation():
    msg = "wait i am actually the seller not buyer"
    res = qx.extract(msg)
    assert res.role == RFQRole.SELLER


def test_duration_range_not_parsed_as_quantity():
    msg = "within 10-15 days"
    res = qx.extract(msg)
    assert res.deadline_days == 15
    assert res.quantity_value is None
    assert "quantity_unit_missing" not in res.flags


def test_calendar_year_not_parsed_as_quantity():
    msg = "before diwali 2026"
    res = qx.extract(msg)
    assert res.quantity_value is None
    assert "quantity_unit_missing" not in res.flags


def test_product_cannot_negate_itself_in_draft():
    draft = {"role": "buyer", "product_details": {"name": "apples"}}
    updated = rts.update_rfq_draft(draft, product_name="suji", negated_attributes=["apples", "suji"])
    assert updated["product_details"]["name"] == "suji"
    neg = updated["product_details"].get("_negated", [])
    assert "apples" in neg
    assert "suji" not in neg

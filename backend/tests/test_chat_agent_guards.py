"""Offline tests for the grounding and argument cleaning behind the chat agent's draft updates."""
import pytest

from services.chat_agent import draft_assist
from services.chat_agent import guards as g

USER = ["mujhe 500 kg basmati chawal chahiye, rate 5,000 rs per quintal", "delivery Indore mein, 8mm lamba daana"]


def clean(fields, remove=None, sources=USER, draft=None):
    return g.clean_update(fields, remove, sources, draft or {})


def test_must_suffix_matches_the_draft_helpers():
    assert g.MUST_SUFFIX == draft_assist.MUST_MATCH_SUFFIX


@pytest.mark.parametrize("a,b", [
    ("5,000 KG!", "5000 kg"), ("500kg", "500 kg"), ("1,00,000 pcs", "100000 pcs"), ("१,००० किलो", "1000 किलो"),
    ("  Basmati   Chawal ", "basmati chawal"), ("2.50 rs", "2.5 rs"),
])
def test_canon_ignores_format(a, b):
    assert g.canon(a) == g.canon(b)


def test_grounded_quote_and_numeric_forms():
    assert g.is_grounded("500 kg", USER)
    assert g.is_grounded("500kg", USER)
    assert g.is_grounded("5000 rs per quintal", USER)  # the user typed 5,000
    assert g.is_grounded("basmati chawal", USER)
    assert g.is_grounded("Indore mein", USER)
    assert g.is_grounded("8 mm", USER)  # the user typed 8mm


def test_grounded_devanagari_and_whole_words():
    src = ["मुझे ५०० किलो चावल चाहिए"]
    assert g.is_grounded("500 किलो", src)
    assert g.is_grounded("चावल", src)
    assert not g.is_grounded("चाव", src)  # part of a word is not a quote
    assert not g.is_grounded("50 किलो", src)


@pytest.mark.parametrize("said", ["", "  ", "x", "1200 kg", "gold", "basmati"[:0]])
def test_ungrounded_or_empty_quotes_fail(said):
    assert not g.is_grounded(said, USER)


def test_accepts_grounded_fields_and_keeps_evidence():
    c = clean({
        "role": {"value": "buyer", "said": "chahiye"},
        "product_name": {"value": "Basmati Rice", "said": "basmati chawal"},
        "quantity": {"value": 500, "unit": "kg", "said": "500 kg"},
        "price_target": {"amount": 5000, "per_unit": "quintal", "said": "5,000 rs per quintal"},
        "location": {"city": "Indore", "state": None, "country": "", "said": "Indore"},
        "specifications": {"grain_length": {"value": "8 mm", "said": "8mm"}},
    })
    assert not c.rejected
    assert c.kwargs == {
        "role": "buyer", "product_name": "Basmati Rice", "quantity_value": 500.0, "quantity_unit": "kg",
        "price_amount": 5000.0, "price_per_unit": "quintal", "city": "Indore",
    }
    assert c.specs == {"grain_length": "8 mm"}
    assert c.evidence["quantity"] == "500 kg" and c.evidence["specifications.grain_length"] == "8mm"


def test_rejects_an_invented_price_and_spec_but_keeps_grounded_fields():
    c = clean({
        "quantity": {"value": 500, "unit": "kg", "said": "500 kg"},
        "price_target": {"amount": 35, "currency": "INR", "per_unit": "kg", "said": "35 rs per kg"},
        "specifications": {"moisture": {"value": "12%", "said": "moisture 12%"}, "grade": {"value": "A"}},
    })
    assert c.kwargs == {"quantity_value": 500.0, "quantity_unit": "kg"}
    assert {r["field"] for r in c.rejected} == {"price_target", "specifications.moisture", "specifications.grade"}
    assert all("said" in r["reason"] or "stated" in r["reason"] for r in c.rejected)
    assert [r["said"] for r in c.rejected if r["field"] == "price_target"] == ["35 rs per kg"]


def test_a_number_that_is_not_in_the_quoted_words_is_rejected():
    c = clean({"quantity": {"value": 5000, "unit": "kg", "said": "500 kg"}})
    assert not c.kwargs and "not in the quoted words" in c.rejected[0]["reason"]
    c = clean({"specifications": {"length": {"value": "9 mm", "said": "8mm"}}})
    assert not c.specs and c.rejected


def test_a_missing_unit_is_asked_not_guessed():
    c = clean({"quantity": {"value": 500, "said": "500 kg"}})
    assert not c.kwargs and "no unit" in c.rejected[0]["reason"]
    # the draft already holds that unit: a bare number may follow
    c = g.clean_update({"quantity": {"value": 800, "unit": "kg", "said": "800"}}, None, ["800"], {"quantity": {"value": 500, "unit": "kg"}})
    assert c.kwargs == {"quantity_value": 800.0, "quantity_unit": "kg"}


def test_currency_must_be_said_unless_inr():
    c = clean({"price_target": {"amount": 5000, "currency": "USD", "said": "5,000 rs per quintal"}})
    assert c.kwargs["price_amount"] == 5000.0 and "price_currency" not in c.kwargs
    assert c.rejected[0]["field"] == "price_target.currency"
    c = g.clean_update({"price_target": {"amount": 5, "currency": "USD", "said": "$5"}}, None, ["price $5 a kg"], {})
    assert c.kwargs["price_currency"] == "USD"


@pytest.mark.parametrize("entry", [
    {"value": None, "said": ""}, {"value": "", "said": ""}, {"value": "   "}, None, "",
])
def test_null_and_empty_values_change_nothing(entry):
    c = clean({"product_name": entry, "description": entry, "category": entry})
    assert c.empty and not c.rejected


def test_null_parts_of_a_compound_field_are_skipped():
    c = clean({
        "quantity": {"value": None, "unit": "", "said": ""}, "price_target": {"amount": None, "currency": None, "per_unit": "", "said": ""},
        "location": {"city": None, "state": "", "said": ""}, "deadline": {"days": None, "date": "", "said": ""},
    })
    assert c.empty and not c.rejected


def test_a_value_without_a_quote_is_rejected():
    c = clean({"product_name": {"value": "Wheat"}, "category": "grain"})
    assert not c.kwargs and len(c.rejected) == 2


def test_quick_reply_and_lookup_sources_ground_a_value():
    shown = ["Organic", "1006", "Rice"]
    assert not g.is_grounded("Organic", USER)
    c = g.clean_update({"hsn_code": {"value": "1006", "said": "1006"}}, None, [*USER, *shown], {})
    assert c.kwargs == {"hsn_code": "1006"}
    c = g.clean_update({"hsn_code": {"value": "1006", "said": "1006"}}, None, USER, {})
    assert not c.kwargs and c.rejected
    c = g.clean_update({"hsn_code": {"value": "9999", "said": "1006"}}, None, shown, {})
    assert not c.kwargs  # the code is not in the quoted words


def test_unknown_and_reserved_fields_are_rejected():
    c = clean({"colour": {"value": "red", "said": "500 kg"}, "specifications": {"hsn_code": {"value": "1", "said": "500 kg"}}})
    assert {r["field"] for r in c.rejected} == {"colour", "specifications.hsn_code"}


def test_remove_paths_and_their_application():
    ok, bad = g.valid_removals(["price_target", "specifications.grade", "location.city", "role", "", "product_details"])
    assert ok == ["price_target", "specifications.grade", "location.city"] and bad == ["role", "product_details"]
    draft = {
        "price_target": {"amount": 5}, "location": {"city": "Indore"},
        "product_details": {"name": "Rice", "grade": "A", "grade__must_match": True, "origin": "India", "hsn_code": "1006", "hsn_source": "manual"},
    }
    done = g.apply_removals(draft, ["price_target", "specifications.Grade", "location.city", "hsn_code", "deadline"])
    assert done == ["price_target", "specifications.Grade", "location.city", "hsn_code"]
    assert draft == {"product_details": {"name": "Rice", "origin": "India"}}
    assert g.apply_removals({"product_details": {"name": "Rice", "a": 1, "b": 2}}, ["specifications"]) == ["specifications"]


def test_clean_update_collects_valid_removals():
    c = clean({}, ["quantity", "nonsense"])
    assert c.remove == ["quantity"] and c.rejected[0]["field"] == "nonsense" and not c.empty


def test_evidence_after_removal():
    ev = {"quantity": "500 kg", "specifications.grade": "A", "specifications.size": "8", "location": "Indore"}
    assert g.evidence_after_removal(ev, ["specifications", "quantity"]) == {"location": "Indore"}
    assert g.evidence_after_removal(ev, ["specifications.grade"]).keys() == {"quantity", "specifications.size", "location"}


@pytest.mark.parametrize("old,new,same", [
    ("rice", "basmati rice", True), ("Basmati Rice", "rice", True), ("rice", "wheat", False), ("steel pipe", "steel rod", False),
    ("", "rice", False),
])
def test_same_product(old, new, same):
    assert g.same_product(old, new) is same


# ---- phase C: live evidence ------------------------------------------------
@pytest.mark.parametrize("said,src,ok", [
    ("मुझे ... चाहिए", ["मुझे 500 किलो चावल चाहिए"], True),
    ("मुझे … चाहिए", ["मुझे 500 किलो चावल चाहिए"], True),
    ("चाहिए ... मुझे", ["मुझे 500 किलो चावल चाहिए"], False),  # parts must keep their order
    ("मुझे ... सोना", ["मुझे 500 किलो चावल चाहिए"], False),
    ("...", ["मुझे 500 किलो चावल चाहिए"], False),
])
def test_an_ellipsis_quote_is_grounded_when_every_part_is_in_order(said, src, ok):
    assert g.is_grounded(said, src) is ok


def test_live_hindi_role_and_quantity_with_a_hindi_unit_are_accepted():
    src = ["मुझे 10 टन चावल खरीदना है"]
    c = g.clean_update({
        "role": {"value": "buyer", "said": "मुझे ... खरीदना"},
        "quantity": {"value": 10, "unit": "tonne", "said": "10 टन"},
    }, None, src, {})
    assert not c.rejected
    assert c.kwargs == {"role": "buyer", "quantity_value": 10.0, "quantity_unit": "tonne"}
    # the number must still be the user's
    c = g.clean_update({"quantity": {"value": 20, "unit": "tonne", "said": "10 टन"}}, None, src, {})
    assert not c.kwargs and c.rejected[0]["field"] == "quantity"


def test_missing_facts_are_found_in_any_language_and_number_format():
    reply = "Aapka RFQ: Basmati Rice, 5,000 kg, ID a1b2c3d4"
    assert g.missing_facts(reply, ["Basmati Rice", "5000", "a1b2c3d4"]) == []
    assert g.missing_facts("Basmati, 500 kg", ["Basmati Rice", "5000"]) == ["Basmati Rice", "5000"]


@pytest.mark.parametrize("unit,said,ok", [
    ("tonnes", "500 kg", False),  # invented: the user said kg
    ("kg", "500 kg", True),
    ("tonne", "5 MT", True),  # a known spelling of the same unit
    ("bag", "10 bags of 50 kg", True),  # the pack unit is one of the units said
    ("kg", "10 bags of 50 kg", True),
    ("tonne", "10 टन", True),  # the quote names no unit the catalogue knows: the model's reading is trusted
    ("container", "2 container", True),
    ("tonne", "500", True),
])
def test_a_quantity_unit_must_be_one_the_quote_contains(unit, said, ok):
    n = said.split()[0]
    c = clean({"quantity": {"value": float(n), "unit": unit, "said": said}}, sources=[said])
    assert bool(c.kwargs) is ok
    if not ok:
        assert "not in the quoted words" in c.rejected[0]["reason"] and "kg" in c.rejected[0]["reason"]


def test_role_accepts_a_bare_string_or_a_value_without_said():
    for entry in ("buyer", {"value": "buyer"}, {"value": "Buying", "said": "not in the sources"}):
        c = clean({"role": entry})
        assert c.kwargs == {"role": "buyer"} and not c.rejected
    assert clean({"role": "seller"}).kwargs == {"role": "seller"}
    assert clean({"role": "banker"}).rejected[0]["field"] == "role"


def test_other_fields_still_need_value_and_said():
    c = clean({"product_name": "onion"})
    assert not c.kwargs and c.rejected[0]["field"] == "product_name"
    assert clean({"product_name": {"value": "Onion"}}).rejected[0]["field"] == "product_name"

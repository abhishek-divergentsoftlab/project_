"""The spec catalogue: families, generation, the normalizer, the API, matching and the chat's quick replies."""

import asyncio
import copy
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text

from models.spec_catalog import SpecCatalog
from services import chat_conversation as cc, chat_draft_assist as cda, chat_spec_filing as csf, spec_catalog as sc
from services.match_scoring import attribute_score_with_breakdown
from tests.conftest import rfq_body
from tests.test_chat_live_findings import (
    APPLE8, B1, HSN_0803, HSN_RICE, U1, U3, Model, _Helpers, _msg, _rfqs, _state,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.filterwarnings("ignore::pytest.PytestWarning")]

LLM_APPLE = [
    {"key": "apple_variety", "label": "Apple Variety", "placeholder": "Fuji", "input_type": "select",
     "options": ["Fuji", "Gala", "Honeycrisp"], "explanation": "Variety sets taste and price.", "must_match": True},
    {"key": "size_diameter_mm", "label": "Size Diameter (mm)", "placeholder": "70", "input_type": "numeric",
     "options": ["65", "70", "75"], "must_match": False},
    {"key": "brix", "label": "Brix (°Bx)", "placeholder": "14", "input_type": "numeric", "options": ["12", "14", "16"]},
    {"key": "packaging_type", "label": "Packaging Type", "placeholder": "Carton Box", "input_type": "select",
     "options": ["Corrugated Box", "Plastic Crate", "Net Bag", "Vacuum Pack"], "must_match": False},
    {"key": "certification", "label": "Certification", "placeholder": "Organic", "input_type": "select",
     "options": ["Organic", "FSSAI"]},
]


def _apple() -> sc.Family:
    return sc.family_from_suggestions("Fuji apples", "Agriculture", LLM_APPLE)


@pytest.fixture(autouse=True)
def _fresh_catalogue_state():
    sc.reset_state()
    yield
    sc.reset_state()


# ---------------------------------------------------------------------------------------------------------------------
# family resolution
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,family", [
    ("fuji apples", "apple"), ("Fuji Apple", "apple"), ("basmati rice", "rice"), ("Basmati Rice", "rice"),
    ("fresh organic tomatoes", "tomato"), ("USB Type-C Fast Charging Cable", "cable"), ("milk powder", "milk powder"),
    ("Apple Juice", "apple juice"), ("bananas", "banana"), ("Green Tea", "tea"),
])
def test_the_family_is_the_singular_head_noun(name, family):
    assert sc.family_key_for(name) == family


async def test_a_variety_word_or_hsn_heading_finds_the_existing_family(db):
    db.add(SpecCatalog(
        family_key="apple", display_name="Apple", category="Agriculture", hsn_heading="0808", source="llm",
        attributes=sc.attributes_from_suggestions("apple", "Agriculture", LLM_APPLE),
    ))
    await db.commit()
    assert (await sc.find_family(db, "fuji apples")).family_key == "apple"
    assert (await sc.find_family(db, "Fuji")).family_key == "apple"  # a variety is a VALUE of the apple family
    assert (await sc.find_family(db, "red delicious", None, "08081000")).family_key == "apple"  # secondary key: HSN heading
    assert await sc.find_family(db, "basmati rice") is None


# ---------------------------------------------------------------------------------------------------------------------
# generation once, then reuse
# ---------------------------------------------------------------------------------------------------------------------
async def test_a_family_is_generated_once_and_then_read_from_the_catalogue(db):
    async def slow(*a, **k):
        await asyncio.sleep(0.15)
        return copy.deepcopy(LLM_APPLE), "llm"

    rec = AsyncMock(side_effect=slow)
    with patch("services.attribute_recommender.recommend_product_attributes", rec):
        fams = await asyncio.gather(*[sc.ensure_family(n, "Agriculture") for n in ("Fuji apples", "apple", "Apples", "gala apple")])
        again = await sc.ensure_family("apples", "Agriculture")
    assert rec.await_count == 1  # concurrent calls share one generation, later calls none
    assert all(f.family_key == "apple" and f.persisted for f in [*fams, again])
    rows = (await db.execute(select(SpecCatalog))).scalars().all()
    assert len(rows) == 1 and rows[0].source == "llm" and rows[0].display_name == "Apple"
    assert [a["key"] for a in rows[0].attributes][:3] == ["variety", "size_diameter", "brix"]


async def test_a_failed_generation_stores_nothing_and_offers_the_common_attributes(db):
    with patch("services.attribute_recommender.recommend_product_attributes", AsyncMock(return_value=([], "none"))):
        fam = await sc.ensure_family("quinoa", "Agriculture")
    assert not fam.persisted and [a["key"] for a in fam.attributes] == [
        "packaging_type", "pack_size", "certifications", "grade", "origin", "condition"]
    assert (await db.execute(select(SpecCatalog))).scalars().all() == []


# ---------------------------------------------------------------------------------------------------------------------
# LLM suggestions -> catalogue attributes
# ---------------------------------------------------------------------------------------------------------------------
def test_llm_attributes_become_catalogue_attributes():
    attrs = {a["key"]: a for a in sc.attributes_from_suggestions("Fuji apples", "Agriculture", LLM_APPLE)}
    assert list(attrs)[:3] == ["variety", "size_diameter", "brix"]  # the product word is gone from the key
    assert attrs["variety"]["type"] == "choice" and attrs["variety"]["must_match_default"] is True
    assert attrs["size_diameter"]["type"] == "number" and attrs["size_diameter"]["unit"] == "mm"
    assert attrs["size_diameter"]["unit_family"] == "length" and attrs["brix"]["unit"] == "°Bx"
    # "Packaging Type" merged into the common packaging_type: one attribute, the LLM's name kept as a synonym
    assert "packaging_type" in attrs and not any(k.startswith("packaging_") and k != "packaging_type" for k in attrs)
    pack = attrs["packaging_type"]
    assert "packaging type" in pack["synonyms"] and "Vacuum Pack" in pack["options"] and "Net Bag" in pack["option_synonyms"]["Bag"]
    assert "Corrugated Box" in pack["option_synonyms"]["Box"]
    # "Certification" merged into the common multi-choice
    assert attrs["certifications"]["type"] == "multi_choice" and "FSSAI" in attrs["certifications"]["options"]
    assert [k for k in attrs if k == "certifications"] == ["certifications"]
    # the category's common attributes are appended; electronics never get a condition
    assert {"pack_size", "grade", "origin", "condition"} <= set(attrs)
    assert "condition" not in {a["key"] for a in sc.attributes_from_suggestions("usb cable", "Electronics", [])}


def test_conversion_is_idempotent_for_catalogue_shaped_input():
    fam = _apple()
    again = sc.attributes_from_suggestions("apple", "Agriculture", fam.suggestions())
    assert [a["key"] for a in again] == [a["key"] for a in fam.attributes]
    assert [a["options"] for a in again] == [a["options"] for a in fam.attributes]


# ---------------------------------------------------------------------------------------------------------------------
# the normalizer
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw_key,raw_value,expected", [
    ("product type", "fuji", ("variety", "Fuji")),
    ("Apple Variety", "Fuji Apple", ("variety", "Fuji")),
    ("variety", "gala", ("variety", "Gala")),
    ("packing", "carton size box of", ("packaging_type", "Carton")),
    ("Packaging Type", "Corrugated Box", ("packaging_type", "Box")),
    ("packaging", "peti", ("packaging_type", "Box")),
    ("packaging", "bori", ("packaging_type", "Sack")),
    ("packaging", "khula", ("packaging_type", "Loose / Bulk")),
    ("pack_weight_kg", 20, ("pack_size", "20 kg")),
    ("Pack Weight", "20000 g", ("pack_size", "20 kg")),
    ("box weight", "20kg", ("pack_size", "20 kg")),
    ("weight per box", "2.5 kg each", ("pack_size", "2.5 kg")),
    ("pack_size", {"value": 20, "unit": "kg"}, ("pack_size", "20 kg")),
    ("certification", "organic and fssai", ("certifications", ["Organic", "FSSAI"])),
    ("Certifications", "Organic, ISO 9001 & haccp", ("certifications", ["Organic", "ISO 9001", "HACCP"])),
    ("certificate", ["fssai"], ("certifications", ["FSSAI"])),
    ("organic", True, ("certifications", ["Organic"])),
    ("quality", "grade a", ("grade", "A")),
    ("Grade", "Export", ("grade", "Export Quality")),
    ("grade", "Class 1121", ("grade", "Class 1121")),  # unknown values are kept (title case), not forced into the options
    ("Size Diameter (mm)", "70", ("size_diameter", "70 mm")),
    ("size diameter", "7 cm", ("size_diameter", "70 mm")),
    ("brix", "min 14 brix", ("brix", "≥14 °Bx")),
    ("Brix (°Bx)", "12-14", ("brix", "12-14 °Bx")),
    ("brix", "maximum 16", ("brix", "≤16 °Bx")),
    ("country of origin", "India", ("origin", "India")),
    ("Colour", "red", ("colour", "red")),  # unknown key: kept as a free spec
    ("Grain Length (mm)", "7", ("grain_length", "7 mm")),
    ("apple_shine", "high", ("shine", "high")),  # the product word is not part of a free key
])
def test_normalize_spec(raw_key, raw_value, expected):
    assert _apple().normalize(raw_key, raw_value) == expected


@pytest.mark.parametrize("raw_key", ["quantity_value", "price_amount", "city", "deadline_days", "role", "category", "title"])
def test_the_forms_core_fields_are_not_specs(raw_key):
    assert _apple().normalize(raw_key, 5) is None
    assert _apple().normalize_details({"name": "apples", raw_key: 5}, strip_core=True) == {"name": "apples"}


def test_normalize_details_flattens_nested_values_and_merges_aliases():
    out = _apple().normalize_details({
        "name": "Fuji apples", "hsn_code": "0808", "hsn_source": "manual",
        "Apple Variety": "Fuji", "pack": {"weight_per_box_kg": 20}, "Packaging Type": "Carton",
        "certification": ["organic", "fssai"], "organic": True, "Certifications__must_match": True,
        "Apple Variety__must_match": False, "origin": "", "colour": None,
    })
    assert out == {
        "name": "Fuji apples", "hsn_code": "0808", "hsn_source": "manual", "variety": "Fuji", "pack_size": "20 kg",
        "packaging_type": "Carton", "certifications": ["Organic", "FSSAI"], "certifications__must_match": True,
    }
    assert not any(isinstance(v, dict) for v in out.values())


def test_a_false_must_match_flag_is_never_written_and_a_true_one_follows_its_spec():
    fam = _apple()
    out = fam.normalize_details({"Apple Variety": "Fuji", "Apple Variety__must_match": False, "grade": "A", "grade__must_match": True})
    assert out == {"variety": "Fuji", "grade": "A", "grade__must_match": True}
    again = fam.normalize_details(out)
    assert again == out  # normalizing is idempotent


def test_a_value_that_is_already_canonical_wins_over_an_alias_of_the_same_spec():
    out = _apple().normalize_details({"variety": "Fuji", "Apple Variety": "garbled model text"})
    assert out["variety"] == "Fuji"


@pytest.mark.parametrize("value,unit,expected", [
    ("20000 g", "kg", "20 kg"), ("20kg", "kg", "20 kg"), ("20", "kg", "20 kg"), (20, "kg", "20 kg"), ("2 lb", "kg", "0.9072 kg"),
    ("12-14", "°Bx", "12-14 °Bx"), ("≥14 °Bx", "°Bx", "≥14 °Bx"), ("at least 14 brix", "°Bx", "≥14 °Bx"),
    ("20 mm", "kg", "20 mm"),  # a unit that cannot be converted is kept as written
    ("14", None, "14"),
])
def test_numbers_are_flat_strings_in_the_catalogue_unit(value, unit, expected):
    assert sc.normalize_number(value, unit) == expected


# ---------------------------------------------------------------------------------------------------------------------
# the chat's real-session answers
# ---------------------------------------------------------------------------------------------------------------------
def _file(message, details=None):
    draft = {"role": "buyer", "product_details": {"name": "apples", **(details or {})}}
    filing = csf.file_answer(draft, message, sc.canonicalize_suggestions("apples", None, APPLE8))
    return draft["product_details"], filing


def test_the_session_answer_files_canonical_keys_and_values():
    details, filing = _file(U3)
    assert details["variety"] == "Fuji" and details["packaging_type"] == "Carton" and details["pack_size"] == "20 kg"
    assert details["certifications"] == ["Organic", "FSSAI"] and details["certifications__must_match"] is True
    assert not any(k.endswith("__must_match") and v is not True for k, v in details.items())
    assert filing.must == ["certifications"]


def test_product_type_is_fuji():
    details, _ = _file("product type is fuji")
    assert details["variety"] == "Fuji"


def test_chat_spec_filing_stores_a_flat_pack_size_in_kg():
    details, _ = _file("packing in carton, weight per box 20000 g")
    assert details["pack_size"] == "20 kg" and details["packaging_type"] == "Carton"


# ---------------------------------------------------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------------------------------------------------
async def test_suggest_attributes_returns_canonical_keys_units_and_multi(make_actor):
    actor = await make_actor("buyer")
    rec = AsyncMock(return_value=(copy.deepcopy(LLM_APPLE), "llm"))
    with patch("services.attribute_recommender.recommend_product_attributes", rec):
        res = await actor.get("/rfqs/suggest-attributes", params={"product_name": "Fuji apples", "category": "Agriculture"})
        res2 = await actor.get("/rfqs/suggest-attributes", params={"product_name": "apples", "category": "Agriculture"})
    assert res.status_code == 200 and rec.await_count == 1
    by_key = {a["key"]: a for a in res.json()["suggested_attributes"]}
    assert list(by_key)[:3] == ["variety", "size_diameter", "brix"]
    assert by_key["size_diameter"]["unit"] == "mm" and by_key["size_diameter"]["input_type"] == "numeric"
    assert by_key["certifications"]["multi"] is True and by_key["certifications"]["input_type"] == "select"
    assert by_key["pack_size"]["unit"] == "kg" and "weight per box" in by_key["pack_size"]["synonyms"]
    assert by_key["variety"]["must_match"] is True and by_key["variety"]["multi"] is False
    assert res2.json()["suggested_attributes"] == res.json()["suggested_attributes"]


async def test_create_rfq_normalizes_legacy_keys_with_the_stored_family(make_actor, db):
    db.add(SpecCatalog(
        family_key="apple", display_name="Apple", category="Agriculture", source="llm",
        attributes=sc.attributes_from_suggestions("apple", "Agriculture", LLM_APPLE),
    ))
    await db.commit()
    actor = await make_actor("buyer")
    body = rfq_body(
        "buyer", title="Need 500 kg Fuji apples", category="Agriculture", product="Fuji apples",
        attributes={"Apple Variety": "fuji", "pack_weight_kg": 20, "Packaging Type": "carton", "certification": "organic, fssai",
                    "Apple Variety__must_match": True, "origin__must_match": False, "Size Diameter (mm)": "70"},
    )
    res = await actor.post("/rfqs", json=body)
    assert res.status_code == 201, res.text
    details = res.json()["product_details"]
    assert details == {
        "name": "Fuji apples", "variety": "Fuji", "pack_size": "20 kg", "packaging_type": "Carton",
        "certifications": ["Organic", "FSSAI"], "variety__must_match": True, "size_diameter": "70 mm",
    }
    # a PATCH is normalized the same way
    patched = await actor.patch(f"/rfqs/{res.json()['id']}", json={"product_details": {"name": "Fuji apples", "pack weight": "25000 g"}})
    assert patched.status_code == 200, patched.text
    assert patched.json()["product_details"] == {"name": "Fuji apples", "pack_size": "25 kg"}


async def test_create_rfq_without_a_stored_family_uses_the_common_attributes_and_never_calls_the_model(make_actor):
    actor = await make_actor("buyer")
    rec = AsyncMock(side_effect=AssertionError("saving an RFQ must not generate a family"))
    with patch("services.attribute_recommender.recommend_product_attributes", rec):
        res = await actor.post("/rfqs", json=rfq_body(
            "buyer", category="Agriculture", product="quinoa", attributes={"packing": "bori", "Pack Weight": "50000 g", "Moisture": "12%"}))
    assert res.status_code == 201, res.text
    assert res.json()["product_details"] == {"name": "quinoa", "packaging_type": "Sack", "pack_size": "50 kg", "moisture": "12%"}


# ---------------------------------------------------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------------------------------------------------
def _status(wanted, offered, key):
    _score, breakdown = attribute_score_with_breakdown(wanted, offered)
    return next(b for b in breakdown if b["key"] == key)


def test_pack_size_compares_after_unit_conversion():
    row = _status({"pack_size": "20 kg"}, {"pack_size": "20000 g"}, "pack_size")
    assert row["status"] == "matched" and row["score"] == 1.0
    assert _status({"pack_size": "20 kg"}, {"pack_size": "20.5 kg"}, "pack_size")["status"] == "matched"  # within 5%
    assert _status({"pack_size": "20 kg"}, {"pack_size": "50 kg"}, "pack_size")["status"] == "mismatch"


def test_certifications_are_a_subset_test_with_partial_credit():
    wanted = {"certifications": ["Organic", "FSSAI"]}
    assert _status(wanted, {"certifications": ["FSSAI", "Organic", "HACCP"]}, "certifications")["status"] == "matched"
    partial = _status(wanted, {"certifications": ["Organic"]}, "certifications")
    assert partial["status"] == "partial" and 0.55 <= partial["score"] < 0.85
    assert _status(wanted, {"certifications": ["HACCP"]}, "certifications")["status"] == "mismatch"


def test_choice_synonyms_and_ranges_are_compared_by_type():
    assert _status({"packaging_type": "Box"}, {"packaging_type": "peti"}, "packaging_type")["status"] == "matched"
    assert _status({"packaging_type": "Box"}, {"packaging_type": "Carton"}, "packaging_type")["status"] == "mismatch"
    assert _status({"brix": "≥14 °Bx"}, {"brix": "15 °Bx"}, "brix")["status"] == "matched"
    assert _status({"brix": "12-14 °Bx"}, {"brix": "13 °Bx"}, "brix")["status"] == "matched"
    assert _status({"brix": "≥14 °Bx"}, {"brix": "7 °Bx"}, "brix")["status"] == "mismatch"


def test_matching_keeps_its_generic_behaviour_for_keys_outside_the_catalogue():
    assert _status({"color": "black"}, {"color": "black"}, "color")["status"] == "matched"
    assert _status({"color": "black"}, {"colour": "red"}, "color")["status"] in ("unstated", "mismatch")


# ---------------------------------------------------------------------------------------------------------------------
# quick replies: one set per question type, and each, sent back, is handled by the existing flow
# ---------------------------------------------------------------------------------------------------------------------
def test_quick_replies_per_question_type():
    draft = {"role": "buyer", "product_details": {"name": "apples"}}
    sug = {"specs": sc.canonicalize_suggestions("apples", None, APPLE8), "hsn": HSN_0803}
    assert cc.quick_replies("role", draft) == ["Buying", "Selling"]
    assert cc.quick_replies("specs", draft, sug) == [  # the attribute labels, must-match presets first, at most 6
        "Apple Variety", "Size Diameter", "Firmness", "Blemish Percentage", "Packaging Type", "Brix Sugar Content", "Skip"]
    assert cc.quick_replies("spec_pick", draft, sug)[-1] == "Done"
    assert cc.quick_replies("spec_value", draft, sug, {"examples": ["12", "14"]}) == ["12", "14", "Skip"]
    assert cc.quick_replies("hsn", draft, sug) == ["Fresh", "Dried", "No"]
    assert cc.quick_replies("hsn", draft, {"hsn": HSN_RICE}) == ["Yes", "No"]
    assert cc.quick_replies("recap", draft) == ["Publish now", "Save as draft", "Change something"]
    many = {"specs": [{"key": "v", "label": "V", "input_type": "select", "options": [f"o{i}" for i in range(9)]}]}
    assert cc.quick_replies("specs", draft, many) == ["V", "Skip"]
    assert cc.focus_chips(many["specs"][0]) == ["o0", "o1", "o2", "o3", "o4", "o5", "o6", "o7", "Other", "Skip"]  # at most 8 options


@pytest.mark.parametrize("chip,expected", [("Publish now", "yes"), ("Save as draft", "draft"), ("Change something", "no")])
def test_every_recap_chip_is_a_recognised_recap_answer(chip, expected):
    assert cc.classify_recap_reply(chip) == expected


async def test_the_role_question_has_chips_and_buying_answers_it(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(), Model():
        d1 = await _msg(user, "apples")
        assert d1["reply"].rstrip().endswith("Are you buying or selling apples?")
        assert d1["quick_replies"] == ["Buying", "Selling"]
        d2 = await _msg(user, "Buying", cid=d1["conversation_id"])
    assert d2["rfq_draft"]["role"] == "buyer"


async def test_the_spec_question_chips_are_labels_and_a_label_asks_only_that_spec(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=APPLE8), Model():
        d1 = await _msg(user, U1)
        cid = d1["conversation_id"]
        assert d1["quick_replies"] == [
            "Apple Variety", "Size Diameter", "Firmness", "Blemish Percentage", "Packaging Type", "Brix Sugar Content", "Skip"]
        d2 = await _msg(user, "Apple Variety", cid=cid)
        assert d2["reply"] == "Which apple variety?"
        assert d2["quick_replies"] == ["Fuji", "Honeycrisp", "Gala", "Granny Smith", "Red Delicious", "Other", "Skip"]
        d3 = await _msg(user, "Gala", cid=cid)  # an option tap files the canonical value
    assert d3["rfq_draft"]["product_details"]["variety"] == "Gala" and d3["rfq_draft"]["product_details"]["variety__must_match"] is True
    assert d3["quick_replies"][-1] == "Done" and "Apple Variety" not in d3["quick_replies"]
    assert d3["reply"].startswith("Added: Apple Variety Gala") and d3["reply"].rstrip().endswith("Pick a spec below, or say Done.")


async def test_skip_chip_declines_the_spec_question_and_the_recap_chips_finish_the_flow(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=APPLE8), Model():
        d1 = await _msg(user, U1)
        cid = d1["conversation_id"]
        d2 = await _msg(user, "Skip", cid=cid)
        assert d2["reply"].rstrip().endswith("Shall I publish it now, or save it as a draft?")
        assert d2["quick_replies"] == ["Publish now", "Save as draft", "Change something"]
        d3 = await _msg(user, "Change something", cid=cid)  # a no: nothing is created, the user is asked what to change
        assert d3["created_rfq"] is None and "What would you like to change?" in d3["reply"]
        assert await _rfqs(db) == []

        d4 = await _msg(user, U1)  # a fresh conversation: the chips publish it
        d5 = await _msg(user, "Skip", cid=d4["conversation_id"])
        d6 = await _msg(user, "Publish now", cid=d4["conversation_id"])
        assert d5["quick_replies"] == ["Publish now", "Save as draft", "Change something"]
        assert d6["created_rfq"] is not None and d6["created_rfq"]["status"] == "active"

        d7 = await _msg(user, U1)
        await _msg(user, "Skip", cid=d7["conversation_id"])
        d8 = await _msg(user, "Save as draft", cid=d7["conversation_id"])
        assert d8["created_rfq"] is not None and d8["created_rfq"]["status"] == "draft"
    assert len(await _rfqs(db)) == 2


async def test_the_value_question_chips_are_its_examples_and_a_chip_answers_it(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=APPLE8), Model():
        d1 = await _msg(user, U1)
        d2 = await _msg(user, U3, cid=d1["conversation_id"])
        assert d2["reply"].rstrip().endswith("e.g. 12, 14") and d2["quick_replies"] == ["12", "14", "Skip"]
        d3 = await _msg(user, "14", cid=d1["conversation_id"])
    assert d3["rfq_draft"]["product_details"]["brix_sugar_content"] == "≥14 °Bx"
    assert d3["quick_replies"] == ["Publish now", "Save as draft", "Change something"]


async def test_the_hsn_offer_chips_are_its_qualifiers_and_a_chip_accepts_it(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=[], hsn=HSN_0803), Model():
        d1 = await _msg(user, B1)
        assert "I suggest HSN 0803" in d1["reply"] and d1["quick_replies"] == ["Fresh", "Dried", "No"]
        d2 = await _msg(user, "Dried", cid=d1["conversation_id"])
    details = d2["rfq_draft"]["product_details"]
    assert details["hsn_code"] == "0803" and details["condition"] == "Dried"


async def test_a_plain_yes_no_hsn_offer_has_yes_and_no_chips(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=[], hsn=HSN_RICE), Model():
        d1 = await _msg(user, "I want to buy basmati rice 500 kg in Pune, 50 rupees per kg, within 7 days")
        assert d1["quick_replies"] == ["Yes", "No"]
        d2 = await _msg(user, "Yes", cid=d1["conversation_id"], prev=d1["reply"])  # the client sends the question back as history
    assert d2["rfq_draft"]["product_details"]["hsn_code"] == "100630"


async def test_the_chat_never_stores_a_false_must_match_flag_and_the_published_rfq_has_canonical_specs(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=APPLE8), Model():
        d1 = await _msg(user, U1)
        cid = d1["conversation_id"]
        await _msg(user, U3, cid=cid)
        st = await _state(db)
        assert {"variety", "pack_size", "certifications"} <= set(st["draft_suggestions"]["answered"])  # replaces the False flags
        await _msg(user, "14", cid=cid)
        d = await _msg(user, "Publish now", cid=cid)
    assert d["created_rfq"] is not None
    (row,) = await _rfqs(db)
    details = row.product_details
    assert details["variety"] == "Fuji" and details["packaging_type"] == "Carton" and details["pack_size"] == "20 kg"
    assert details["certifications"] == ["Organic", "FSSAI"] and details["brix_sugar_content"] == "≥14 °Bx"
    assert not any(k.endswith("__must_match") and v is not True for k, v in details.items())
    assert not {"Apple Variety", "Packaging Type", "Pack Size", "Certifications"} & set(details)
    assert d["rfq_draft"] == {}  # published: the draft is cleared


# ---------------------------------------------------------------------------------------------------------------------
# the migration script
# ---------------------------------------------------------------------------------------------------------------------
async def test_the_migration_script_dry_run_prints_and_apply_rewrites_and_reindexes(make_actor, db):
    from models.rfq import RFQ
    from scripts import normalize_rfq_specs as script

    db.add(SpecCatalog(
        family_key="apple", display_name="Apple", category="Agriculture", source="llm",
        attributes=sc.attributes_from_suggestions("apple", "Agriculture", LLM_APPLE),
    ))
    await db.commit()
    actor = await make_actor("buyer")
    legacy = {
        "name": "Fuji apples", "Apple Variety": "Fuji", "pack_weight": {"value": 20000, "unit": "g"}, "Packaging Type": "carton",
        "Certifications": "Organic, FSSAI", "Certifications__must_match": True, "Apple Variety__must_match": False,
    }
    created = (await actor.post("/rfqs", json=rfq_body("buyer", title="Fuji apples", category="Agriculture", product="Fuji apples"))).json()
    await db.execute(text("update rfqs set product_details = cast(:d as jsonb) where id = :id"), {"d": __import__("json").dumps(legacy), "id": created["id"]})
    await db.commit()

    lines: list[str] = []
    dry = await script.normalize_all(db, apply=False, out=lines.append)
    assert len(dry) == 1 and dry[0]["new"] == {
        "name": "Fuji apples", "variety": "Fuji", "pack_size": "20 kg", "packaging_type": "Carton",
        "certifications": ["Organic", "FSSAI"], "certifications__must_match": True,
    }
    assert any(l.strip().startswith("new:") and '"pack_size": "20 kg"' in l for l in lines)
    assert any(l.strip().startswith("old:") and "Apple Variety" in l for l in lines)
    await db.rollback()
    stored = (await db.execute(select(RFQ.product_details))).scalar_one()
    assert stored == legacy  # a dry run writes nothing

    indexed: list = []

    async def fake_index(_db, rfq):
        indexed.append(rfq.id)
        return True

    with patch("services.rfq_service.index_rfq", fake_index):
        done = await script.normalize_all(db, apply=True, out=lambda _l: None)
        again = await script.normalize_all(db, apply=True, out=lambda _l: None)
    assert len(done) == 1 and again == [] and len(indexed) == 1  # re-indexed once; a second run changes nothing
    await db.rollback()
    row = (await db.execute(select(RFQ))).scalar_one()
    assert row.product_details == dry[0]["new"] and "20 kg" in row.search_text


async def test_a_common_option_word_does_not_pick_a_family(db):
    db.add(SpecCatalog(
        family_key="apple", display_name="Apple", category="Agriculture", source="llm",
        attributes=sc.attributes_from_suggestions("apple", "Agriculture", LLM_APPLE),
    ))
    await db.commit()
    assert await sc.find_family(db, "cardboard box") is None  # "Box" is a packaging option of every family
    assert (await sc.find_family(db, "honeycrisp")).family_key == "apple"


# ---------------------------------------------------------------------------------------------------------------------
# SCOPE ADDITION: attribute chips after the required fields, tapping specs, the HSN answer, the read tools
# ---------------------------------------------------------------------------------------------------------------------
from tests.test_chat_live_findings import BANANA_SPECS, RECAP_Q  # noqa: E402

MIX = [
    APPLE8[0],
    {"key": "certifications", "label": "Certifications", "input_type": "select", "multi": True, "synonyms": ["certification"],
     "options": ["Organic", "FSSAI", "HACCP"], "must_match": False, "placeholder": "Organic"},
    {"key": "pack_size", "label": "Pack size (kg)", "input_type": "numeric", "unit": "kg", "synonyms": ["pack size"],
     "options": ["10", "20", "25"], "must_match": False, "placeholder": "20"},
    APPLE8[4],
]
RECAP_CHIPS = ["Publish now", "Save as draft", "Change something"]


async def test_the_bananas_session_asks_for_attributes_after_the_hsn_answer_and_taps_build_the_specs(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=BANANA_SPECS, hsn=HSN_0803), Model():
        d1 = await _msg(user, B1)
        cid = d1["conversation_id"]
        assert "tell me a few specs" in d1["reply"] and "I suggest HSN 0803" not in d1["reply"] or "Suggested HSN 0803" in d1["reply"]
        assert d1["quick_replies"] == ["Variety", "Ripeness Level", "Skip"]
        d2 = await _msg(user, "fresh", cid=cid)
        assert d2["reply"].startswith("Added: HSN 0803, Condition Fresh.")
        assert "HSN 0803 applied" not in d2["reply"] and RECAP_Q not in d2["reply"]
        assert "tell me a few specs" in d2["reply"] and d2["quick_replies"] == ["Variety", "Ripeness Level", "Skip"]
        d3 = await _msg(user, "Variety", cid=cid)
        assert d3["reply"] == "Which variety?" and d3["quick_replies"] == ["Cavendish", "Red Banana", "Lady Finger", "Other", "Skip"]
        d4 = await _msg(user, "Cavendish", cid=cid)
        assert d4["rfq_draft"]["product_details"]["variety"] == "Cavendish" and d4["quick_replies"] == ["Ripeness Level", "Done"]
        d5 = await _msg(user, "Done", cid=cid)
    assert d5["reply"].rstrip().endswith(RECAP_Q) and d5["quick_replies"] == RECAP_CHIPS


async def test_multi_choice_accumulates_until_done_and_done_goes_to_the_recap(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=MIX), Model():
        d1 = await _msg(user, U1)
        cid = d1["conversation_id"]
        d2 = await _msg(user, "Certifications", cid=cid)
        assert d2["quick_replies"] == ["Organic", "FSSAI", "HACCP", "Other", "Done"]
        d3 = await _msg(user, "Organic", cid=cid)
        assert d3["rfq_draft"]["product_details"]["certifications"] == ["Organic"]
        assert d3["quick_replies"] == ["FSSAI", "HACCP", "Other", "Done"]
        assert d3["reply"].rstrip().endswith("Choose more certifications, or tap Done.")
        d4 = await _msg(user, "FSSAI", cid=cid)
        assert d4["rfq_draft"]["product_details"]["certifications"] == ["Organic", "FSSAI"]
        d5 = await _msg(user, "Done", cid=cid)  # done with certifications: back to the remaining labels
        assert d5["quick_replies"] == ["Apple Variety", "Pack size", "Packaging Type", "Done"]
        d6 = await _msg(user, "Done", cid=cid)  # done with everything: the recap
    assert d6["reply"].rstrip().endswith(RECAP_Q) and d6["quick_replies"] == RECAP_CHIPS


async def test_number_specs_offer_example_values_with_the_unit_and_other_asks_for_free_text(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=MIX), Model():
        d1 = await _msg(user, U1)
        cid = d1["conversation_id"]
        d2 = await _msg(user, "Pack size", cid=cid)
        assert d2["quick_replies"] == ["10 kg", "20 kg", "25 kg", "Other"]
        d3 = await _msg(user, "Other", cid=cid)
        assert d3["reply"] == "Please type the pack size." and not d3["quick_replies"]
        d4 = await _msg(user, "12500 g", cid=cid)  # free text is filed for the open spec, in the catalogue unit
        assert d4["rfq_draft"]["product_details"]["pack_size"] == "12.5 kg"
        d5 = await _msg(user, "Apple Variety", cid=cid)
        d6 = await _msg(user, "Pink Lady", cid=cid)  # not an option: kept as typed
    assert d6["rfq_draft"]["product_details"]["variety"] == "Pink Lady"


async def test_a_typed_multi_spec_answer_still_works_while_tapping(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=APPLE8), Model():
        d1 = await _msg(user, U1)
        cid = d1["conversation_id"]
        await _msg(user, "Apple Variety", cid=cid)
        d3 = await _msg(user, U3, cid=cid)
    details = d3["rfq_draft"]["product_details"]
    assert details["variety"] == "Fuji" and details["packaging_type"] == "Carton" and details["pack_size"] == "20 kg"
    assert details["certifications"] == ["Organic", "FSSAI"]


async def test_make_variety_must_match_after_a_tapped_value(client, db, make_actor):
    user = await make_actor("both")
    with _Helpers(specs=[{**APPLE8[0], "must_match": False}]), Model():
        d1 = await _msg(user, U1)
        cid = d1["conversation_id"]
        await _msg(user, "Apple Variety", cid=cid)
        d3 = await _msg(user, "Fuji", cid=cid)
        assert "variety__must_match" not in d3["rfq_draft"]["product_details"]
        d4 = await _msg(user, "make variety must match", cid=cid)
    assert d4["rfq_draft"]["product_details"]["variety__must_match"] is True


def test_hinglish_words_for_the_tapping_flow():
    assert cc.is_done("Ho gaya") and cc.is_done("done") and not cc.is_done("done with the rest of my day")
    assert cc.detect_decline("Ho gaya") == "all"
    assert cc.spec_ask_text(APPLE8[0], "hinglish") == "Kaun sa apple variety?"
    assert "Ho gaya" in cc.spec_pick_text("hinglish")
    assert cc.match_spec_label("variety", sc.canonicalize_suggestions("apples", None, APPLE8))["key"] == "variety"
    assert cc.match_spec_label("Pack Size", MIX)["key"] == "pack_size" and cc.match_spec_label("fuji", MIX) is None


async def test_the_read_tools_return_catalogue_and_hsn_data_the_verifier_accepts(db):
    from services import answer_verifier, market_data_tools as mdt

    assert {"get_spec_options", "lookup_hsn"} <= mdt.READ_TOOL_NAMES
    assert {s["function"]["name"] for s in mdt.READ_TOOL_SCHEMAS} >= {"get_spec_options", "lookup_hsn"}
    db.add(SpecCatalog(family_key="apple", display_name="Apple", category="Agriculture", source="llm",
                       attributes=sc.attributes_from_suggestions("apple", "Agriculture", LLM_APPLE)))
    await db.commit()
    res = await mdt.run_tool(db, None, "get_spec_options", {"product": "Fuji apples", "bogus": 1})
    assert res["ok"] and res["tool"] == "get_spec_options" and res["data"]["family"] == "apple"
    variety = next(a for a in res["data"]["attributes"] if a["key"] == "variety")
    assert variety == {"key": "variety", "label": "Apple Variety", "type": "choice", "options": ["Fuji", "Gala", "Honeycrisp"], "unit": None}
    one = await mdt.run_tool(db, None, "get_spec_options", {"product": "apples", "spec": "pack weight"})
    assert [a["key"] for a in one["data"]["attributes"]] == ["pack_size"] and one["data"]["attributes"][0]["unit"] == "kg"
    none = await mdt.run_tool(db, None, "get_spec_options", {"product": "apples", "spec": "colour of the moon"})
    assert not none["ok"] and none["notes"]
    ok = answer_verifier.verify("Apples come as Fuji, Gala or Honeycrisp, sold in 20 kg packs.", [res, one], "what varieties of apples?")
    assert ok.ok is True and ok.read_tools_called == 2

    hsn = await mdt.run_tool(db, None, "lookup_hsn", {"product_or_code": "bananas"})
    assert hsn["ok"] and hsn["data"]["candidates"] and {"code", "title", "context", "recommended"} <= set(hsn["data"]["candidates"][0])
    code = hsn["data"]["candidates"][0]["code"]
    assert answer_verifier.verify(f"Use HSN {code} for bananas.", [hsn], "which hsn for bananas").ok is True
    bad = await mdt.run_tool(db, None, "lookup_hsn", {})
    assert not bad["ok"]

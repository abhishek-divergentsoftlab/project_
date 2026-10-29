"""Listing input hygiene, AI moderation, category resolution, typo rules, GST honesty and the GPU guard logic."""

import importlib.util
import json
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from schemas.common import Location, Money, Quantity, canonical_unit, strip_markup
from schemas.rfq import RFQCreate, clean_title
from services import categories, llm_client, moderation_service
from services.catalog_service import _damerau, _term_alternatives, is_typo_of
from services.moderation_service import AIContentModerator


# --- units -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,want",
    [("kilo", "kg"), ("Kgs.", "kg"), ("tons", "tonne"), ("MT", "tonne"), ("metric tons", "tonne"), ("pieces", "pcs"),
     ("Nos", "pcs"), ("ltr", "l"), ("per kg", "kg"), ("Rs/kg", "kg"), ("sq ft", "sq ft"), ("sqm", "sq m"),
     ("container", "container"), ("bags", "bags"), ("dozen", "dozen"), ("50 kg", "50 kg"), ("टन", "tonne")],
)
def test_unit_aliases_resolve(raw, want):
    assert canonical_unit(raw) == want


@pytest.mark.parametrize("raw", ["lightyear", "banana", "", "   ", "xyz123"])
def test_unknown_units_are_rejected_with_a_message(raw):
    assert canonical_unit(raw) is None
    with pytest.raises(ValidationError):
        Quantity(value=1, unit=raw)


def test_unknown_unit_message_suggests_a_close_one():
    with pytest.raises(ValidationError, match="did you mean 'carton'"):
        Quantity(value=1, unit="cartin")


def test_every_unit_the_chat_extracts_validates():
    from services import query_extractor

    assert [u for u in query_extractor._UNITS if not canonical_unit(u)] == []


def test_money_amount_must_be_positive_and_per_unit_known():
    with pytest.raises(ValidationError):
        Money(amount=0, currency="INR")
    with pytest.raises(ValidationError):
        Money(amount=5, currency="INR", per_unit="banana")
    assert Money(amount=5, currency="INR", per_unit="Kilo").per_unit == "kg"
    assert Money(amount=5, currency="INR", per_unit="  ").per_unit is None


# --- text hygiene ------------------------------------------------------------------------------------------------

def test_markup_is_stripped_but_prose_survives():
    assert clean_title("<img src=x onerror=alert(1)> onion") == "onion"
    assert strip_markup("<script>alert(1)</script>fresh <b>red</b> onions") == "fresh red onions"
    assert strip_markup("tolerance < 5 mm &amp; > 2 mm") == "tolerance < 5 mm & > 2 mm"


def test_title_needs_meaningful_characters():
    with pytest.raises(ValueError):
        clean_title("a")
    with pytest.raises(ValueError):
        clean_title("<b></b>")
    with pytest.raises(ValueError):
        clean_title("aa")
    assert clean_title("PP") == "PP"
    assert clean_title("Rice") == "Rice"


def test_blank_location_parts_become_null():
    loc = Location(city="   ", state=" Gujarat ", country="")
    assert loc.city is None and loc.country is None and loc.state == "Gujarat"


def test_rfq_create_cleans_description_and_product_details():
    rfq = RFQCreate(
        role="buyer", category="Agriculture", title="<b>Onion</b> red", description="<i>fresh</i>",
        product_details={"note": "<script>x</script>grade A", "n": 3},
    )
    assert rfq.title == "Onion red" and rfq.description == "fresh"
    assert rfq.product_details == {"note": "grade A", "n": 3}


# --- categories -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["Weapons", "asdf", "xyzzy goods"])
def test_made_up_categories_do_not_resolve(raw):
    assert categories.resolve_category_strict(raw) is None


@pytest.mark.parametrize("raw,want", [("electronic", "Electronics"), ("vagitables", "Agriculture"), ("Solar", "Solar & Energy"),
                                      ("general goods", "General Goods"), ("Packaging materials", "Packaging")])
def test_real_categories_resolve(raw, want):
    assert categories.resolve_category_strict(raw) == want


async def test_unknown_category_is_classified_by_the_model_and_cached(monkeypatch):
    calls = []

    async def fake(prompt, schema=None, **kw):
        calls.append(prompt)
        return {"category": "Agriculture"}

    monkeypatch.setattr(llm_client, "generate_json", fake)
    categories._CLASSIFY_CACHE.clear()
    assert await categories.resolve_category("spice root", "Turmeric fingers 5 kg") == "Agriculture"
    assert await categories.resolve_category("spice root", "Turmeric fingers 5 kg") == "Agriculture"
    assert len(calls) == 1


async def test_category_the_model_rejects_lists_allowed_values(monkeypatch):
    async def fake(prompt, schema=None, **kw):
        return {"category": "none"}

    monkeypatch.setattr(llm_client, "generate_json", fake)
    categories._CLASSIFY_CACHE.clear()
    categories._CLASSIFY_MISS.clear()
    with pytest.raises(categories.CategoryError, match="Electronics"):
        await categories.resolve_category("asdf", "asdf")


async def test_category_with_model_down_is_refused_honestly_and_not_cached(monkeypatch):
    async def fake(prompt, schema=None, **kw):
        return None

    monkeypatch.setattr(llm_client, "generate_json", fake)
    categories._CLASSIFY_CACHE.clear()
    categories._CLASSIFY_MISS.clear()
    with pytest.raises(categories.CategoryError, match="unavailable"):
        await categories.resolve_category("asdf", "asdf")
    assert not categories._CLASSIFY_MISS


# --- moderation ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def fresh_verdicts(monkeypatch, tmp_path):
    monkeypatch.setattr(moderation_service, "_VERDICT_FILE", tmp_path / "v.json")
    monkeypatch.setattr(moderation_service, "_verdicts", {})
    monkeypatch.setattr(moderation_service, "_verdicts_loaded", True)


async def test_regex_hit_blocks_without_calling_the_model(monkeypatch, fresh_verdicts):
    async def boom(*a, **k):
        raise AssertionError("model must not be called")

    monkeypatch.setattr(llm_client, "generate_json", boom)
    res = await AIContentModerator.check_text_async("10 AK47 rifles")
    assert not res.is_safe


async def test_ai_blocks_what_the_regexes_miss_and_caches(monkeypatch, fresh_verdicts):
    calls = []

    async def fake(prompt, schema=None, **kw):
        calls.append(1)
        return {"verdict": "prohibited", "category": "narcotics_and_controlled_substances", "flagged": "ganja", "reason": "cannabis"}

    monkeypatch.setattr(llm_client, "generate_json", fake)
    res = await AIContentModerator.check_text_async("Ganja 5 kg")
    assert not res.is_safe and res.category == "narcotics_and_controlled_substances"
    again = await AIContentModerator.check_text_async("  ganja   5 KG ")
    assert not again.is_safe and len(calls) == 1
    assert json.loads(moderation_service._VERDICT_FILE.read_text())  # persisted


async def test_ai_allows_legal_goods(monkeypatch, fresh_verdicts):
    async def fake(prompt, schema=None, **kw):
        return {"verdict": "allowed"}

    monkeypatch.setattr(llm_client, "generate_json", fake)
    assert (await AIContentModerator.check_text_async("Basmati rice 25 kg bags")).is_safe


async def test_ai_unavailable_allows_but_flags_for_review_and_is_not_cached(monkeypatch, fresh_verdicts):
    async def fake(prompt, schema=None, **kw):
        return None

    monkeypatch.setattr(llm_client, "generate_json", fake)
    res = await AIContentModerator.check_text_async("Ganja 5 kg")
    assert res.is_safe and res.category == moderation_service.REVIEW_CATEGORY
    assert not moderation_service._verdicts


async def test_audit_logs_review_row_when_ai_down(monkeypatch, fresh_verdicts):
    async def fake(prompt, schema=None, **kw):
        return None

    monkeypatch.setattr(llm_client, "generate_json", fake)
    added = []

    class DB:
        def add(self, row):
            added.append(row)

        async def commit(self):
            pass

    res = await AIContentModerator.audit_and_verify(DB(), uuid.uuid4(), "rfq_create", "Mystery powder", None, "Chemicals")
    assert res.is_safe and len(added) == 1 and added[0].blocked is False


def test_existing_sync_check_text_unchanged():
    assert not AIContentModerator.check_text("elephant ivory").is_safe
    assert AIContentModerator.check_text("glue gun sticks").is_safe


# --- catalog typo rule ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("word,target,ok", [("onoin", "onion", True), ("tumeric", "turmeric", True), ("vegitable", "vegetable", True),
                                           ("haldi", "half", False), ("haldi", "cell", False), ("moongfali", "moong", False),
                                           ("rice", "price", False), ("cabel", "cable", True), ("steel", "stool", False)])
def test_typo_rule(word, target, ok):
    assert is_typo_of(word, target) is ok


def test_damerau_transposition_counts_as_one():
    assert _damerau("onoin", "onion", 2) == 1


def test_alternatives_never_contain_unrelated_fuzzy_words():
    alts = " ".join(_term_alternatives("haldi", fuzzy=True, vocab=["half", "cell", "solar", "panel"]))
    assert "half" not in alts and "cell" not in alts


# --- quotation document tax ---------------------------------------------------------------------------------------------------

def test_tax_rate_only_from_data():
    from services.document_generator import _declared_tax_rate

    assert _declared_tax_rate(SimpleNamespace(), SimpleNamespace(product_details={})) is None
    assert _declared_tax_rate(SimpleNamespace(), SimpleNamespace(product_details={"gst_rate": "5%"})) == Decimal("0.05")
    assert _declared_tax_rate(SimpleNamespace(gst_rate=18), SimpleNamespace(product_details={})) == Decimal("0.18")
    assert _declared_tax_rate(SimpleNamespace(), SimpleNamespace(product_details={"gst": "abc"})) is None


def test_invoice_source_has_no_hardcoded_gst():
    src = Path(__file__).resolve().parent.parent.joinpath("services/document_generator.py").read_text()
    assert 'Decimal("0.18")' not in src and "4819.10" not in src


# --- GPU guard ------------------------------------------------------------------------------------------------------------------

def _guard():
    spec = importlib.util.spec_from_file_location("ollama_gpu_guard", Path(__file__).resolve().parent.parent / "scripts/ollama_gpu_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "ps,expect",
    [({"models": [{"name": "m", "size": 100, "size_vram": 100}]}, "OK"),
     ({"models": [{"name": "m", "size": 100, "size_vram": 83}]}, "split"),
     ({"models": [{"name": "other", "size": 100, "size_vram": 100}]}, "not loaded")],
)
def test_guard_dry_run_decisions(monkeypatch, capsys, ps, expect):
    g = _guard()
    monkeypatch.setattr(g, "call", lambda base, path, body=None, timeout=15.0: ps)
    assert g.main(["--dry-run", "--model", "m"]) == 0
    assert expect in capsys.readouterr().out


def test_guard_drop_page_cache(tmp_path):
    g = _guard()
    (tmp_path / "a").write_bytes(b"x" * 10)
    (tmp_path / "b").write_bytes(b"y" * 20)
    assert g.drop_page_cache(str(tmp_path)) == (2, 30)

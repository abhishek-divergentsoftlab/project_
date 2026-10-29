"""The AI value canonicalizer (LLM mocked): batching, persistent cache, never blocking a save, ignored by renderers,
used by the matcher; plus family-side dimension alignment."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from services import llm_client, match_scoring, rfq_indexing, spec_catalog, value_canon


@pytest.fixture(autouse=True)
def _cache(tmp_path):
    original = value_canon.CACHE_PATH
    value_canon.reset_cache(tmp_path / "canon.json")
    yield tmp_path / "canon.json"
    value_canon.reset_cache(original)


def fake_llm(monkeypatch, replies):
    calls = []

    async def generate_json(prompt, schema=None, *, timeout, model=None, temperature=0.0):
        calls.append(prompt)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(llm_client, "generate_json", generate_json)
    return calls


REPLY = {"product": "Onion", "values": {"color": "Red"}, "distinct_products": 1}


def run(coro):
    return asyncio.run(coro)


def test_one_batched_call_and_block_shape(monkeypatch):
    calls = fake_llm(monkeypatch, [REPLY])
    out = run(value_canon.add_canon({"name": "pyaz", "color": "lal", "gsm": "180", "grade": "A"}, "Lal pyaz"))
    assert len(calls) == 1 and "lal" in calls[0]
    assert out["_canon"] == {"v": 2, "product": "onion", "values": {"color": "red"}, "n": 1}
    assert out["_canon"]["values"]["color"] == "red"
    to_normalise = json.loads(calls[0].split("Listing JSON (use ALL of it to decide what the product is):")[1].split("Return JSON")[0])["attributes_to_normalise"]
    assert set(to_normalise) == {"color", "grade"}  # numbers are context, never normalised


def test_cache_is_persistent_and_stable(monkeypatch, _cache):
    calls = fake_llm(monkeypatch, [REPLY, {"product": "garlic", "values": {"color": "purple"}, "distinct_products": 1}])
    first = run(value_canon.add_canon({"name": "pyaz", "color": "lal"}, "Lal pyaz"))
    assert json.loads(_cache.read_text())["entries"]
    value_canon.reset_cache(_cache)  # a new process: reads the file
    second = run(value_canon.add_canon({"name": "Pyaz", "color": " LAL "}, "Lal pyaz"))
    assert len(calls) == 1  # nothing new to ask
    assert first["_canon"] == second["_canon"]


def test_failures_never_block_or_cache(monkeypatch, _cache):
    fake_llm(monkeypatch, [RuntimeError("boom")])
    details = {"name": "pyaz", "color": "lal"}
    out = run(value_canon.add_canon(details, "x"))
    assert out == {**details, "_canon_pending": True} and "_canon" not in out and not _cache.exists()  # retried by the job
    fake_llm(monkeypatch, [None])
    assert "_canon" not in run(value_canon.add_canon(details, "x"))
    fake_llm(monkeypatch, [{"product": 5, "values": "nope", "distinct_products": "many"}])
    assert "_canon" not in run(value_canon.add_canon(details, "x"))


def test_pending_mark_set_until_complete_and_cleared(monkeypatch):
    fake_llm(monkeypatch, [None])
    out = run(value_canon.add_canon({"name": "pyaz", "color": "lal"}, "t"))
    assert out["_canon_pending"] is True
    fake_llm(monkeypatch, [REPLY])
    fresh = run(value_canon.add_canon(out, "t"))  # the job re-runs it on the stored details
    assert "_canon_pending" not in fresh and fresh["_canon"]["product"] == "onion"
    # cache-only calls (candidates scored in bulk) never mark anything
    assert "_canon_pending" not in run(value_canon.add_canon({"name": "zzz", "color": "qqq"}, "t", allow_llm=False))
    # a partial answer keeps the mark so the job completes it
    fake_llm(monkeypatch, [{"product": "onion", "values": {}, "distinct_products": 1}])
    assert run(value_canon.add_canon({"name": "pyaz", "color": "rare"}, "t"))["_canon_pending"] is True


def test_partial_reply_only_caches_what_it_resolved(monkeypatch):
    fake_llm(monkeypatch, [{"product": "onion", "values": {}, "distinct_products": 1}])
    out = run(value_canon.add_canon({"name": "pyaz", "color": "lal"}, "Lal pyaz"))
    assert out["_canon"]["product"] == "onion" and out["_canon"]["values"] == {}


def test_cache_only_mode_makes_no_call(monkeypatch):
    calls = fake_llm(monkeypatch, [REPLY])
    out = run(value_canon.add_canon({"name": "pyaz", "color": "lal"}, "t", allow_llm=False))
    assert "_canon" not in out and not calls
    run(value_canon.add_canon({"name": "pyaz", "color": "lal"}, "t"))
    rfq = SimpleNamespace(product_details={"name": "pyaz", "color": "lal"}, title="t")
    value_canon.ensure_for_rfq_cached(rfq)
    assert value_canon.canon_product(rfq.product_details) == "onion"
    assert value_canon.canon_value(rfq.product_details, "color") == "red"


def test_lazy_canon_for_requester(monkeypatch):
    fake_llm(monkeypatch, [REPLY])
    rfq = SimpleNamespace(product_details={"name": "pyaz", "color": "lal"}, title="t")
    run(value_canon.ensure_for_rfq(rfq, allow_llm=True))
    assert value_canon.canon_product(rfq.product_details) == "onion"


def test_distinct_products_and_plurals(monkeypatch):
    fake_llm(monkeypatch, [{"product": "vegetables", "values": {}, "distinct_products": 9}])
    out = run(value_canon.add_canon({"name": "vegetables"}, "onion potato tomato garlic ginger rice wheat dal sugar"))
    assert value_canon.distinct_products(out) == 9
    assert value_canon.distinct_products({}) == 1
    assert value_canon.canon_text("Onions") == value_canon.canon_text("onion")
    assert value_canon.canon_text("Chilies") == value_canon.canon_text("chilly") or value_canon.canon_text("Chilies") == "chily"


def test_only_free_text_values_are_sent():
    got = value_canon.categorical_values({
        "name": "x", "color": "lal", "gsm": "180", "thickness": "2 mm", "grade": "A", "organic": True,
        "pack": "50 kg", "_negated": ["a"], "notes__must_match": True, "size": "40/50", "origin": "Nashik",
    })
    assert set(got) == {"color", "grade", "origin"}  # "40/50" is a size code, compared as an identifier


def test_renderers_ignore_canon():
    details = {"name": "pyaz", "color": "lal", "_canon": {"v": 1, "product": "onion", "values": {"color": "red"}, "n": 1}}
    rfq = SimpleNamespace(
        role=SimpleNamespace(value="seller"), category="Agriculture", title="Lal pyaz", product_details=details,
        quantity_value=None, quantity_unit=None, price_amount=None, price_currency="INR", price_per_unit=None,
        location_city=None, location_state=None, location_country=None, deadline_at=None, description=None,
    )
    text, match_text, tags = rfq_indexing.build_search_text(rfq), rfq_indexing.build_match_text(rfq), rfq_indexing.build_search_tags(rfq)
    for blob in (text, match_text, " ".join(tags)):
        assert "_canon" not in blob.lower() and "canon" not in blob.lower().replace("canonical", "")
        assert "'v'" not in blob and "values" not in blob
    assert "Also known as: onion" in text and "onion" in tags and "onion" in match_text


def test_normalize_details_drops_stale_canon():
    fam = spec_catalog.family_from_suggestions("cotton fabric", "Textiles")
    out = fam.normalize_details({"name": "cotton fabric", "color": "red", "_canon": {"product": "stale"}})
    assert "_canon" not in out and not any(k.startswith("canon") for k in out)


def test_family_moves_dimension_value_onto_canonical_key():
    fam = spec_catalog.Family("cotton fabric", attributes=[
        spec_catalog.make_attr("gsm", "Weight (gsm)", spec_catalog.NUMBER, unit="gsm"),
        spec_catalog.make_attr("width", "Width (cm)", spec_catalog.NUMBER, unit="cm"),
    ])
    out = fam.align_by_dimension({"name": "cloth", "fabric heft": "180 g/m2", "roll length": "50 m"})
    assert "gsm" in out and "fabric heft" not in out
    assert out["roll length"] == "50 m"  # length is shared by several specs: untouched


def test_matching_uses_canonical_product_equality(monkeypatch):
    # attribute comparison through the canonical terms of both sides (the product-level boost lives in match_service)
    w = {"name": "peanut", "type": "sella", "_canon": {"product": "peanut", "values": {"type": "parboiled"}}}
    o = {"name": "x", "type": "parboiled", "_canon": {"product": "peanut", "values": {"type": "parboiled"}}}
    assert match_scoring.attribute_score_with_breakdown(w, o)[0] == 1.0


def test_canonical_identifiers_normalized_alike_on_both_sides():
    assert value_canon.same_term("s 30", "s-30") and value_canon.same_term("S30", "s-30")
    assert not value_canon.same_term("s30", "m30")
    w = {"grade": "S 30", "_canon": {"values": {"grade": "s-30"}}}
    o = {"grade": "special", "_canon": {"values": {"grade": "s30"}}}
    assert match_scoring.attribute_score_with_breakdown(w, o)[0] == 1.0
    a = {"name": "x", "_canon": {"product": "Chilli-Pepper"}}
    b = {"name": "y", "_canon": {"product": "chilli pepper"}}
    assert value_canon.canon_product(a) == value_canon.canon_product(b)


def test_product_name_always_resolved_even_when_batch_reply_omits_it(monkeypatch):
    calls = fake_llm(monkeypatch, [
        {"values": {"grade": "s30"}, "distinct_products": 1},   # batch answer without a product
        {"product": "red chilli"},                              # dedicated product call
    ])
    out = run(value_canon.add_canon({"name": "lal mirch", "grade": "S 30"}, "Lal mirch"))
    assert len(calls) == 2
    assert out["_canon"]["product"] == "red chilli" and out["_canon"]["values"]["grade"] == "s30"
    assert "_canon_pending" not in out
    entry = next(e for k, e in value_canon._load().items() if k.startswith("|name|") and k.endswith("|lal mirch"))
    assert entry["term"] == "red chilli" and entry["v"] == 2


SUGAR_CTX = {"name": "chini", "grade": "S-30", "hsn_code": "1701"}


def test_context_is_in_the_prompt(monkeypatch):
    calls = fake_llm(monkeypatch, [{"product": "sugar", "product_confidence": 0.9, "values": {}, "distinct_products": 1}])
    run(value_canon.add_canon(dict(SUGAR_CTX), "Chini S-30 bags", category="Agriculture"))
    prompt = calls[0]
    for needle in ("Chini S-30 bags", "Agriculture", "S-30", "1701", "Sugars"):
        assert needle in prompt, needle
    assert "NEVER answer" in prompt  # the model is told not to answer from the spelling


def test_near_spelling_contradicting_context_is_rejected_and_reasked(monkeypatch, _cache):
    calls = fake_llm(monkeypatch, [
        {"product": "chili", "product_confidence": 0.9, "values": {}, "distinct_products": 1},
        {"product": "sugar", "product_confidence": 0.9},
    ])
    out = run(value_canon.add_canon(dict(SUGAR_CTX), "Chini S-30", category="Agriculture"))
    assert len(calls) == 2 and "rejected" in calls[1] and "chapter" in calls[1]
    assert out["_canon"]["product"] == "sugar"
    entries = json.loads(_cache.read_text())["entries"]
    stored = {e["term"]: e for k, e in entries.items() if k.startswith("|name|")}
    assert "chili" not in stored and stored["sugar"]["conf"] >= 0.6 and stored["sugar"]["exp"] is None
    assert stored["sugar"]["model"] is not None and stored["sugar"]["evidence"]["verdict"] == "agree"


def test_wrong_answer_twice_is_not_used_nor_cached_permanently(monkeypatch, _cache):
    fake_llm(monkeypatch, [{"product": "chili", "product_confidence": 0.95, "values": {}, "distinct_products": 1}])
    out = run(value_canon.add_canon(dict(SUGAR_CTX), "Chini S-30", category="Agriculture"))
    assert "product" not in (out.get("_canon") or {}) and out["_canon_pending"] is True
    entry = next(e for k, e in value_canon._load().items() if k.startswith("|name|"))
    assert entry["exp"] is not None  # short TTL, never permanent


def test_low_confidence_is_weak_and_expires(monkeypatch, _cache):
    fake_llm(monkeypatch, [{"product": "widget", "product_confidence": 0.5, "values": {}, "distinct_products": 1}])
    out = run(value_canon.add_canon({"name": "zorbex"}, "Zorbex", category="Industrial"))
    assert "product" not in (out.get("_canon") or {})  # used for nothing
    key, entry = next((k, e) for k, e in value_canon._load().items() if k.startswith("|name|"))
    assert entry["term"] == "widget" and entry["exp"] is not None and 0.35 <= entry["conf"] < 0.6
    # inside the TTL the model is not asked again
    calls = fake_llm(monkeypatch, [{"product": "other", "product_confidence": 0.9, "values": {}, "distinct_products": 1}])
    run(value_canon.add_canon({"name": "zorbex"}, "Zorbex", category="Industrial"))
    assert not calls  # a weak answer inside its TTL is not re-asked
    entry["exp"] = 1  # expired: asked again, and this time accepted
    run(value_canon.add_canon({"name": "zorbex"}, "Zorbex", category="Industrial"))
    assert value_canon._load()[key]["term"] == "other" and value_canon._load()[key]["exp"] is None


def test_same_word_in_another_context_is_a_separate_entry(monkeypatch):
    fake_llm(monkeypatch, [{"product": "sugar", "product_confidence": 0.9, "values": {}, "distinct_products": 1},
                           {"product": "zorbex", "product_confidence": 0.9, "values": {}, "distinct_products": 1}])
    run(value_canon.add_canon({"name": "chini", "hsn_code": "1701"}, "t", category="Agriculture"))
    run(value_canon.add_canon({"name": "chini", "hsn_code": "6911"}, "t", category="Furniture"))
    keys = [k for k in value_canon._load() if k.startswith("|name|")]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_spelling_variants_are_not_echoes():
    from services import value_canon_validate as v

    assert v.spelling_echo("chini", "chili")
    assert not v.spelling_echo("chilli", "chili")


def test_revalidate_and_purge_fix_a_wrong_entry_without_hand_editing(_cache):
    _cache.write_text(json.dumps({"entries": {
        "|name|chini": "chili", "|name|pyaz": "onion", "chini|agriculture|grade|s 30": "s30",
    }}))
    value_canon.reset_cache(_cache)
    dry = value_canon.revalidate_cache(apply=False)
    assert [p["key"] for p in dry["purged"]] == ["|name|chini"] and "|name|chini" in json.loads(_cache.read_text())["entries"]
    done = value_canon.revalidate_cache(apply=True)
    entries = json.loads(_cache.read_text())["entries"]
    assert "|name|chini" not in entries and done["purged"]
    assert entries["|name|pyaz"]["v"] == 2 and entries["chini|agriculture|grade|s 30"]["v"] == 2
    assert value_canon.purge_cache(term="onion") == ["|name|pyaz"]
    assert "|name|pyaz" not in json.loads(_cache.read_text())["entries"]


def test_legacy_product_entries_are_not_trusted_at_runtime(monkeypatch, _cache):
    _cache.write_text(json.dumps({"entries": {"|name|chini": "chili"}}))
    value_canon.reset_cache(_cache)
    rfq = SimpleNamespace(product_details={"name": "chini"}, title="t", category="Agriculture")
    value_canon.ensure_for_rfq_cached(rfq)
    assert value_canon.canon_product(rfq.product_details) is None

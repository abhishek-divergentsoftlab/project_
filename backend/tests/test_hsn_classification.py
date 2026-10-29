"""HSN classification pipeline: retrieve -> model decides -> validate -> learn. The model is mocked."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import settings  # noqa: E402
from services import hsn_service as hs  # noqa: E402
from services import llm_client  # noqa: E402

pytestmark = pytest.mark.skipif(not os.path.exists(hs._HSN_DB_PATH), reason="hsn master db not built")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "_LEARNED_DB_PATH", str(tmp_path / "learned.db"))
    monkeypatch.setattr(hs, "_learned_ready", False)
    monkeypatch.setattr(settings, "HSN_AI_ENABLED", True)
    hs._inflight.clear()


class FakeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def __call__(self, prompt, schema=None, *, timeout, model=None, temperature=0.0):
        self.prompts.append(prompt)
        if not self.replies:
            return None
        r = self.replies.pop(0)
        return r(prompt) if callable(r) else r


def reply(code, recognized=True, name="x", syn=(), why="fits", ptype="", material="", processed=False):
    return {"recognized": recognized, "generic_name": name, "synonyms": list(syn), "product_type": ptype,
            "material": material, "is_processed": processed, "hsn_code": code, "reason": why}


def run(coro):
    return asyncio.run(coro)


def use(monkeypatch, fake):
    monkeypatch.setattr(llm_client, "generate_json", fake)
    return fake


def test_processed_form_candidates_show_hierarchy_and_pick_wins(monkeypatch):
    fake = use(monkeypatch, FakeLLM(reply("07122000", name="dried onion powder"), reply("07122000")))  # 2nd = blind
    res = run(hs.classify("onion powder"))
    prompt = fake.prompts[0]
    # the model sees the dried-vegetables line with its hierarchy, so it can tell powder from fresh onion
    assert "07122000" in prompt and "powder" in prompt.lower()
    assert res.code == "07122000" and res.source == "ai"
    assert res.confidence == "high"  # an independent blind classification lands in the same heading


def test_head_noun_and_product_word_are_both_retrieved():
    cands, strong = hs.retrieve_candidates("onion seeds")
    headings = {c["code"][:4] for c in cands}
    assert "1209" in headings  # the "seeds" form
    assert any(c["source"].startswith("word:onion") or c["source"] == "phrase" for c in cands)


def test_ai_only_pick_is_medium_and_cached_persistently(monkeypatch):
    use(monkeypatch, FakeLLM(reply("120242", name="groundnut", syn=["peanut", "ground-nuts"])))
    res = run(hs.classify("peanut"))
    assert res.code and res.code.startswith("1202") and res.confidence == "medium"
    # second call never reaches the model: it is served from the persistent cache with the stored confidence
    fake2 = use(monkeypatch, FakeLLM())
    again = run(hs.classify("peanut"))
    assert again.code == res.code and again.source == "cache" and again.confidence == "medium"
    assert fake2.prompts == []


def test_low_confidence_when_ai_unavailable_and_nothing_cached(monkeypatch):
    use(monkeypatch, FakeLLM())  # every call returns None: timeout / model down
    res = run(hs.classify("onion powder"))
    assert res.code is not None and res.confidence == "low" and res.needs_confirmation
    assert res.ai_status == "unavailable" and res.source == "lexical"
    assert hs.learned_get(hs.norm_query("onion powder")) is None


def test_miss_on_timeout_is_not_cached(monkeypatch):
    use(monkeypatch, FakeLLM(None))
    run(hs.classify("peanut"))
    assert hs.learned_get(hs.norm_query("peanut")) is None
    # the model is back: the earlier failure must not have stuck
    use(monkeypatch, FakeLLM(reply("120242", name="groundnut")))
    res = run(hs.classify("peanut"))
    assert res.code and res.code.startswith("1202") and res.source == "ai"


def test_not_recognised_is_a_short_ttl_miss_only(monkeypatch):
    use(monkeypatch, FakeLLM(reply("", recognized=False)))
    res = run(hs.classify("qwertyzx plomb"))
    assert res.code is None and res.ai_status == "not_recognized"
    row = hs.learned_get(hs.norm_query("qwertyzx plomb"))
    assert row is not None and row["code"] is None
    import sqlite3
    exp = sqlite3.connect(hs._LEARNED_DB_PATH).execute("SELECT expires FROM hsn_learned").fetchone()[0]
    assert exp is not None  # misses expire; positives (expires NULL) do not


def test_pick_that_does_not_read_like_the_product_is_challenged(monkeypatch):
    fake = use(monkeypatch, FakeLLM(
        reply("070200", name="brinjal", syn=["eggplant", "aubergine"]),  # tomatoes: wrong
        reply("07093000", name="brinjal"),                               # re-decided from eggplant candidates
    ))
    res = run(hs.classify("brinjal"))
    assert len(fake.prompts) == 2 and "does not look like the product" in fake.prompts[1]
    assert res.code and res.code.startswith("0709")
    assert res.confidence in ("medium", "high")


def test_group_rows_are_never_returned(monkeypatch):
    g = hs.master_row("07031010")
    assert g is not None and g["is_group"] == 1
    assert hs.resolve_code("07031010") not in (None, "07031010")
    use(monkeypatch, FakeLLM(reply("07031010", name="onion")))
    res = run(hs.classify("onion bulbs"))
    row = hs.master_row(res.code)
    assert row is not None and not row["is_group"]
    for c in ("03041", "07031", "0703101"):  # 5/7-digit group rows are cut back to a real code
        r = hs.resolve_code(c)
        assert r is None or len(r) in (4, 6, 8)


def test_search_and_lookup_exclude_groups():
    from api.v1 import hsn as api

    assert api.lookup_hsn_item("07031010") is None
    assert api.lookup_hsn_item("03041") is None
    items = api.search_hsn("onion", 50)
    assert items and all(len(i.code) in (4, 6, 8) for i in items)
    assert all(i.code != "07031010" for i in items)
    browse = api.search_hsn("0703 10", 50)
    assert browse and "07031010" not in {i.code for i in browse}
    kids = api.get_hsn_children("070310", 50)
    assert {"07031011", "07031019"} <= {k.code for k in kids}  # group looked through


def test_fragment_titles_come_from_the_hierarchy():
    from api.v1 import hsn as api

    it = api.lookup_hsn_item("07031019")
    assert it is not None and "Onion" in it.formatted_title and it.formatted_title != "Other"
    assert it.path and it.path.endswith("Other")


def test_one_word_no_longer_hijacks_the_phrase():
    from api.v1 import hsn as api

    for q in ("roasted peanuts salted", "paracetamol tablets", "pp woven bags"):
        code, reason = api._resolve_commercial_intent(q)
        assert not reason or "Closest match" not in reason
    code, _ = api._resolve_commercial_intent("roasted peanuts salted")
    assert code != "620342"


def test_search_uses_learned_cache_for_vernacular_names(monkeypatch):
    from api.v1 import hsn as api

    hs.learned_put(hs.norm_query("zzz groundnut kernels"), "120242", "test", "medium", "m")
    items = api.search_hsn("zzz groundnut kernels", 5)
    assert items and items[0].code == "120242"


def test_timeout_is_at_least_twenty_seconds(monkeypatch):
    monkeypatch.delenv("HSN_SUGGEST_TIMEOUT_SECONDS", raising=False)
    assert hs.suggest_timeout() >= 20
    monkeypatch.setenv("HSN_SUGGEST_TIMEOUT_SECONDS", "3")
    assert hs.suggest_timeout() >= 20
    monkeypatch.setenv("HSN_SUGGEST_TIMEOUT_SECONDS", "45")
    assert hs.suggest_timeout() == 45


def test_high_needs_independent_blind_agreement(monkeypatch):
    # candidate-informed pick is right per the text, but the blind classification disagrees -> only medium
    use(monkeypatch, FakeLLM(reply("07122000", name="dried onion powder"), reply("0703")))
    assert run(hs.classify("onion powder")).confidence == "medium"


def test_processed_product_on_raw_material_line_is_challenged(monkeypatch):
    fake = use(monkeypatch, FakeLLM(
        reply("0702", name="tomato puree", ptype="puree", material="tomato", processed=True),  # fresh tomatoes
        reply("200290", name="tomato puree", ptype="puree", material="tomato", processed=True),
    ))
    res = run(hs.classify("tomato puree"))
    assert len(fake.prompts) == 2 and "processed product" in fake.prompts[1]
    assert res.code and res.code.startswith("2002") and res.confidence != "high"


def test_short_terms_match_whole_words_only():
    rows = hs.lexical_rows(["til"], 20, prefix_last=True)
    assert all(__import__("re").search(r"\btils?\b", r["fulltext"], __import__("re").I) for r in rows)
    assert all(not r["code"].startswith("2922") for r in rows)


def test_budget_bounds_worst_case(monkeypatch):
    monkeypatch.setenv("HSN_SUGGEST_BUDGET_SECONDS", "20")
    t = {"n": 0}

    async def slow(prompt, schema=None, *, timeout, model=None, temperature=0.0):
        t["n"] += 1
        assert timeout <= 20.0
        return reply("070200", name="brinjal")  # wrong pick forces a second look

    monkeypatch.setattr(llm_client, "generate_json", slow)
    import time as _t
    real = _t.monotonic
    base = real()
    # pretend 15 s already passed after the first call: not enough budget for a second round
    calls = iter([base, base, base + 15, base + 15, base + 15, base + 15, base + 15, base + 15])
    monkeypatch.setattr(hs.time, "monotonic", lambda: next(calls, base + 15))
    res = run(hs.classify("brinjal"))
    assert t["n"] == 1 and res.code and res.confidence == "low"


# ---- community votes -------------------------------------------------------------------------------------
def _vote(user, code="07122000", text="onion powder"):
    return run(hs.confirm_learned(text, code, user))


def test_single_vote_never_reaches_high(monkeypatch):
    use(monkeypatch, FakeLLM(reply("07122000")))
    res = _vote("u1")
    assert res["stored"] and res["status"] == "recorded"
    cached = hs.learned_get(hs.norm_query("onion powder"))
    assert cached is None or cached["confidence"] != "high"
    # a second vote by the SAME user replaces the first, it does not add a voter
    _vote("u1")
    assert hs.vote_tally(hs.norm_query("onion powder")) == {"07122000": 1}


def test_ineligible_accounts_do_not_count(monkeypatch):
    use(monkeypatch, FakeLLM(reply("07122000")))
    for u in ("a", "b", "c"):
        run(hs.confirm_learned("onion powder", "07122000", u, eligible=False))
    assert hs.vote_tally(hs.norm_query("onion powder")) == {}
    assert hs.learned_get(hs.norm_query("onion powder")) is None


def test_three_distinct_votes_make_it_high(monkeypatch):
    use(monkeypatch, FakeLLM(reply("07122000")))  # blind call agrees
    _vote("u1"); _vote("u2")
    assert hs.learned_get(hs.norm_query("onion powder")) is None
    assert _vote("u3")["status"] == "confirmed"
    cached = hs.learned_get(hs.norm_query("onion powder"))
    assert cached["code"] == "07122000" and cached["confidence"] == "high"
    use(monkeypatch, FakeLLM())
    assert run(hs.classify("onion powder")).confidence == "high"


def test_contradicting_blind_call_blocks_high(monkeypatch):
    use(monkeypatch, FakeLLM(reply("0703")))  # blind says fresh onions: different heading
    _vote("u1"); _vote("u2")
    assert _vote("u3")["status"] == "blocked_by_model"
    assert hs.learned_get(hs.norm_query("onion powder")) is None


def test_unavailable_blind_call_needs_no_conflicting_votes(monkeypatch):
    use(monkeypatch, FakeLLM())  # blind unavailable
    _vote("u1"); _vote("u2")
    assert _vote("u3")["status"] == "confirmed"  # nobody disagrees


def test_split_vote_is_disputed_medium_with_alternatives(monkeypatch):
    use(monkeypatch, FakeLLM(reply("07122000")))
    for u in ("u1", "u2", "u3"):
        _vote(u)
    assert hs.learned_get(hs.norm_query("onion powder"))["confidence"] == "high"
    _vote("u4", "0703"); res = _vote("u5", "0703")
    assert res["status"] == "disputed"
    use(monkeypatch, FakeLLM())
    out = run(hs.classify("onion powder"))
    assert out.confidence == "medium" and any(a["code"] == "0703" for a in out.alternatives)


def test_leader_needs_a_clear_margin(monkeypatch):
    use(monkeypatch, FakeLLM(reply("07122000")))
    for u in ("u1", "u2", "u3"):
        _vote(u)
    _vote("u4", "0703")  # 3 vs 1: margin 2 -> still clear
    assert hs.learned_get(hs.norm_query("onion powder"))["confidence"] == "high"
    assert hs._vote_verdict({"A": 3, "B": 2})[1] is True

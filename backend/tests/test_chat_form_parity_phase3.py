"""Phase 3: the form's HSN suggestion, spec suggestions and live moderation work in chat."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from models.conversation import Conversation
from services import ai_chat_service, answer_verifier, chat_draft_assist as cda, rfq_tool_service

pytestmark = [pytest.mark.asyncio, pytest.mark.filterwarnings("ignore::pytest.PytestWarning")]

BASE = {
    "role": "buyer",
    "category": "Agriculture",
    "product_details": {"name": "Basmati Rice"},
    "quantity": {"value": 500.0, "unit": "kg"},
}
HSN = {
    "code": "1006", "description": "Rice", "formatted_title": "1006 - Rice",
    "category": "Agriculture", "confidence": "high",
}
SPECS = [
    {"key": "grade", "label": "Grade", "placeholder": "e.g. 1121", "input_type": "text", "options": [],
     "explanation": None, "must_match": True},
    {"key": "origin", "label": "Origin", "placeholder": "e.g. Punjab", "input_type": "text", "options": [],
     "explanation": None, "must_match": False},
]
SAFE = {"is_safe": True, "reason": None, "category": None}


def _draft(**over):
    d = json.loads(json.dumps(BASE))
    d.update(over)
    return d


def _patch_helpers(hsn=HSN, specs=SPECS, mod=SAFE):
    return (
        patch.object(cda, "_fetch_hsn_suggestion", AsyncMock(return_value=hsn)),
        patch.object(cda, "_fetch_spec_suggestions", AsyncMock(return_value=specs)),
        patch.object(cda, "_fetch_moderation", AsyncMock(return_value=mod)),
    )


class _Helpers:
    def __init__(self, **kw):
        self.ps = _patch_helpers(**kw)

    def __enter__(self):
        self.mocks = [p.__enter__() for p in self.ps]
        self.hsn, self.specs, self.mod = self.mocks
        return self

    def __exit__(self, *a):
        for p in self.ps:
            p.__exit__(*a)


def _script(content="ok", seen=None):
    async def chat(messages, tools):
        if seen is not None:
            seen.append(messages[0]["content"])
        return {"role": "assistant", "content": content}

    return patch("services.answer_engine._chat", chat)


def _tool_chat(args, seen=None):
    calls = {"n": 0}

    async def chat(messages, tools):
        if seen is not None:
            seen.append(messages[0]["content"])
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "update_rfq_draft", "arguments": args}}]}
        return {"role": "assistant", "content": "Noted."}

    return patch("services.answer_engine._chat", chat)


async def _state(db, user):
    conv = (await db.execute(select(Conversation).where(Conversation.user_id == user.id))).scalars().one()
    await db.refresh(conv)
    return conv.state


async def _msg(user, text, draft=None, cid=None, previous=None):
    """One chat request; ``previous`` is the assistant text the client would send as history."""
    history = [{"role": "assistant", "content": previous}] if previous else []
    body = {"messages": [*history, {"role": "user", "content": text}]}
    if draft is not None:
        body["current_rfq"] = draft
    if cid:
        body["conversation_id"] = cid
    res = await user.post("/ai-chat/message", json=body)
    assert res.status_code == 200, res.text
    return res.json()


# ---- 1: HSN -------------------------------------------------------------
async def test_suggestion_is_stored_not_applied_and_offered_in_prompt(client, db, make_actor):
    user = await make_actor("buyer")
    seen: list[str] = []
    with _Helpers() as h, _script("Suggested HSN 1006 - Rice; use it?", seen):
        data = await _msg(user, "I need 600 kg for delivery in Indore", BASE)
    assert h.hsn.await_count == 1
    st = await _state(db, user)
    assert st["draft_suggestions"]["hsn"]["code"] == "1006"
    assert st["draft_suggestions"]["hsn_for"] == "Basmati Rice"
    assert "hsn_code" not in (st["rfq_draft"].get("product_details") or {})
    assert data["draft_suggestions"]["hsn"]["code"] == "1006"
    # The suggestion is fetched while the model works, so this turn's prompt cannot carry it; the flow makes the offer.
    assert "Suggested HSN 1006" in data["reply"]
    # Offered once: the reply quoted the code, so the next prompt does not re-offer.
    assert st["draft_suggestions"].get("hsn_offered") is True


async def test_no_recompute_when_product_unchanged(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers() as h, _script("ok"):
        first = await _msg(user, "I need 600 kg for delivery in Indore", BASE)
        await _msg(user, "and 20 days deadline please", cid=first["conversation_id"])
    assert h.hsn.await_count == 1
    assert h.specs.await_count == 1


async def test_bare_yes_after_hsn_offer_applies_code_and_category(client, db, make_actor):
    user = await make_actor("buyer")
    d = _draft(category="General Goods")
    with _Helpers(hsn={**HSN, "category": "Agriculture"}), _script("Suggested HSN 1006 - Rice; use it?"):
        first = await _msg(user, "I need 600 kg for delivery in Indore", d)
    with _Helpers(), _script("Applied HSN 1006."):
        data = await _msg(user, "yes", cid=first["conversation_id"], previous="Suggested HSN 1006 - Rice; use it?")
    pd = data["rfq_draft"]["product_details"]
    assert pd["hsn_code"] == "1006" and pd["hsn_source"] == "manual"
    assert data["rfq_draft"]["category"] == "Agriculture"


async def test_bare_yes_without_hsn_offer_does_not_apply(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), _script("Which city?"):
        first = await _msg(user, "I need 600 kg for delivery in Indore", BASE)
    with _Helpers(), _script("Which city?"):
        data = await _msg(user, "yes", cid=first["conversation_id"], previous="Which city?")
    assert "hsn_code" not in data["rfq_draft"]["product_details"]


def test_accepts_hsn_phrases():
    sug = {"code": "1006"}
    for t in ("yes use that HSN", "use the suggested hsn", "haan wahi HSN", "apply the suggested HSN please"):
        assert cda.accepts_hsn(t, "", sug), t
    assert cda.accepts_hsn("yes", "Suggested HSN 1006 - Rice; use it?", sug)
    assert not cda.accepts_hsn("yes", "Which city?", sug)
    assert not cda.accepts_hsn("no don't use the suggested hsn", "", sug)
    assert not cda.accepts_hsn("use that hsn", "", None)


async def test_explicit_valid_code_sets_code_and_category(client, db, make_actor):
    user = await make_actor("buyer")
    item = SimpleNamespace(code="100630", description="Rice, semi-milled", formatted_title="100630 - Rice", category="Agriculture")
    with _Helpers(), patch("api.v1.hsn.lookup_hsn_item", return_value=item), _script("Set HSN 100630."):
        data = await _msg(user, "HSN 100630", _draft(category="General Goods"))
    pd = data["rfq_draft"]["product_details"]
    assert pd["hsn_code"] == "100630" and pd["hsn_source"] == "manual"
    assert data["rfq_draft"]["category"] == "Agriculture"


async def test_explicit_unknown_code_is_reported_and_not_set(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), patch("api.v1.hsn.lookup_hsn_item", return_value=None), _script("Okay."):
        data = await _msg(user, "hsn code 9999", BASE)
    assert "hsn_code" not in data["rfq_draft"]["product_details"]
    assert "wasn't recognised" in data["reply"]


def test_tool_arg_hsn_code_validated_like_the_route():
    item = SimpleNamespace(code="1006", description="Rice", formatted_title="1006 - Rice", category="Agriculture")
    with patch("api.v1.hsn.lookup_hsn_item", return_value=item):
        out = rfq_tool_service.update_rfq_draft(_draft(), hsn_code="1006")
    assert out["product_details"]["hsn_code"] == "1006" and out["product_details"]["hsn_source"] == "manual"
    notes: list[str] = []
    with patch("api.v1.hsn.lookup_hsn_item", return_value=None):
        out = rfq_tool_service.update_rfq_draft(_draft(), hsn_code="1234", _notes=notes)
    assert "hsn_code" not in out["product_details"] and notes
    schema = rfq_tool_service.UPDATE_RFQ_DRAFT_TOOL["function"]["parameters"]["properties"]
    assert "hsn_code" in schema and schema["must_match"]["type"] == "array"


async def test_hsn_route_unchanged_when_called_through_shared_function():
    from api.v1 import hsn as hsn_api

    with patch.object(hsn_api, "_resolve_commercial_intent", return_value=(None, None)), \
         patch.object(hsn_api, "classify_hsn_with_ai", AsyncMock(return_value=None)):
        assert await hsn_api.suggest_hsn_for_product("zzqq") is None
    assert hsn_api.suggest_hsn_for_product.__name__ == "suggest_hsn_for_product"


async def test_verifier_accepts_the_suggested_hsn_number():
    reply = "Suggested HSN 100630 for this product; use it?"
    assist = cda.DraftAssist({"draft_suggestions": {"hsn": {**HSN, "code": "100630"}}})
    assert not answer_verifier.allowed_values([], "", _draft()) & {100630.0}
    extra = assist.extra_allowed_text(_draft())
    allowed = answer_verifier.allowed_values([], "need rice", _draft(), extra)
    assert 100630.0 in allowed
    assert "100630" in extra and reply


# ---- 2: specs and must-match --------------------------------------------
async def test_specs_stored_and_unfilled_labels_in_prompt(client, db, make_actor):
    user = await make_actor("buyer")
    seen: list[str] = []
    with _Helpers(), _script("ok", seen):
        data = await _msg(user, "I need 600 kg for delivery in Indore", BASE)
    st = await _state(db, user)
    assert [s["label"] for s in st["draft_suggestions"]["specs"]] == ["Grade", "Origin"]
    assert st["draft_suggestions"]["specs_for"] == "Basmati Rice"
    assert data["draft_suggestions"]["specs"]
    # the questions are the flow's: the model is not told to ask about specs (the price question comes first here)
    assert not any("Optional specifications" in s for s in seen)
    assert "target price" in data["reply"]


async def test_spec_value_filed_under_catalogue_key_with_preset_must_match(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), _tool_chat({"product_details": {"grade": "1121"}}):
        data = await _msg(user, "grade is 1121 and I need 600 kg", BASE)
    pd = data["rfq_draft"]["product_details"]
    assert pd["grade"] == "1121" and "Grade" not in pd
    assert pd["grade__must_match"] is True
    assert "origin__must_match" not in pd


def test_apply_spec_suggestions_without_preset():
    d = _draft()
    d["product_details"]["origin"] = "Punjab"
    cda.apply_spec_suggestions(d, SPECS)
    assert d["product_details"]["origin"] == "Punjab"
    assert "origin__must_match" not in d["product_details"]


def test_apply_spec_suggestions_skips_the_preset_for_a_spec_the_user_answered():
    d = _draft()
    d["product_details"]["Grade"] = "1121"
    cda.apply_spec_suggestions(d, SPECS, answered=["grade"])
    assert d["product_details"] == {"name": "Basmati Rice", "grade": "1121"}  # no False flag, no preset


@pytest.mark.parametrize("text", [
    "grade must match", "Grade is mandatory", "the grade is strict", "grade zaroori hai", "GRADE is non-negotiable",
    "grade of the rice must match",
])
def test_must_match_phrases(text):
    d = _draft()
    d["product_details"]["Grade"] = "1121"
    assert cda.must_match_from_text(d, text) == ["Grade"]
    assert d["product_details"]["Grade__must_match"] is True


def test_must_match_ignores_unknown_attributes_and_plain_text():
    d = _draft()
    d["product_details"]["Grade"] = "1121"
    assert cda.must_match_from_text(d, "origin must match") == []
    assert cda.must_match_from_text(d, "I want grade 1121") == []
    assert not any(k.endswith("__must_match") for k in d["product_details"])


def test_must_match_tool_arg_case_insensitive_and_ignores_unknown():
    d = _draft()
    d["product_details"]["Grade"] = "1121"
    out = rfq_tool_service.update_rfq_draft(d, must_match=["grade", "Colour"])
    assert out["product_details"]["Grade__must_match"] is True
    assert "Colour__must_match" not in out["product_details"]


def test_must_match_in_same_call_as_value():
    out = rfq_tool_service.update_rfq_draft(_draft(), product_details={"Grade": "1121"}, must_match=["Grade"])
    assert out["product_details"]["Grade__must_match"] is True


# ---- 3: moderation --------------------------------------------------------
UNSAFE = {"is_safe": False, "reason": "Prohibited item: weapons", "category": "weapons"}


async def test_unsafe_listing_blocks_create_shortcut_and_warns(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(mod=UNSAFE), patch("services.rfq_service.create_rfq", AsyncMock()) as create, \
         patch("services.answer_engine._chat", side_effect=AssertionError("model must not be called")):
        data = await _msg(user, "create the rfq", BASE)
    create.assert_not_awaited()
    assert not data["created_rfq"]
    assert "Prohibited item: weapons" in data["reply"] and "content policy" in data["reply"]
    assert data["draft_moderation"]["is_safe"] is False
    st = await _state(db, user)
    assert st["draft_moderation"]["is_safe"] is False and st["draft_moderation"]["text_hash"]


async def test_unsafe_turn_appends_warning_to_reply(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(mod=UNSAFE), _script("Noted, anything else?"):
        data = await _msg(user, "I need 600 kg for delivery in Indore", BASE)
    assert data["reply"].startswith("Added: ") and "Noted, anything else?" not in data["reply"]  # the flow asks, not the model
    assert "may violate our content policy: Prohibited item: weapons" in data["reply"]


async def test_create_rfq_tool_refuses_unsafe_listing():
    assist = cda.DraftAssist()
    with _Helpers(mod=UNSAFE), patch("services.rfq_service.create_rfq", AsyncMock()) as create:
        created, err = await ai_chat_service.execute_create_rfq_call(_draft(), {}, assist=assist)
    create.assert_not_awaited()
    assert created is None and "Prohibited item: weapons" in err


async def test_safe_listing_still_creates_and_moderation_is_cached(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers() as h, patch("services.answer_engine._chat", side_effect=AssertionError("no model")):
        recap = await _msg(user, "create the rfq", BASE)
        assert recap["created_rfq"] is None and "Shall I publish it now" in recap["reply"]
        data = await _msg(user, "yes", cid=recap["conversation_id"])
    assert data["created_rfq"]
    assert h.mod.await_count == 1  # checked once, create reused the cache
    assert data["draft_moderation"] is None  # draft cleared after create


async def test_moderation_cached_by_text_hash():
    assist = cda.DraftAssist()
    with _Helpers() as h:
        await assist.refresh(_draft())
        await assist.refresh(_draft())
        assert h.mod.await_count == 1
        await assist.refresh(_draft(description="fresh stock"))
        assert h.mod.await_count == 2
    assert assist.moderation["text_hash"] and assist.moderation["is_safe"] is True


async def test_moderation_text_matches_the_endpoint_format():
    assert cda.moderation_text(_draft(description="d")) == "Basmati Rice\nd\nAgriculture"


# ---- 4: latency and failure --------------------------------------------------
async def test_failures_and_timeouts_are_swallowed(monkeypatch):
    monkeypatch.setattr(cda, "HSN_TIMEOUT", 0.05)
    monkeypatch.setattr(cda, "SPECS_TIMEOUT", 0.05)
    monkeypatch.setattr(cda, "MODERATION_TIMEOUT", 0.05)

    async def slow(*a, **k):
        await asyncio.sleep(5)

    async def boom(*a, **k):
        raise RuntimeError("down")

    assist = cda.DraftAssist()
    monkeypatch.setattr(cda, "_fetch_hsn_suggestion", slow)
    monkeypatch.setattr(cda, "_fetch_spec_suggestions", boom)
    monkeypatch.setattr(cda, "_fetch_moderation", slow)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await assist.refresh(_draft())
    assert loop.time() - t0 < 1.0
    assert assist.pending_hsn is None
    assert assist.suggestions["specs"] == []
    assert assist.moderation is None
    assert assist.prompt_section(_draft()) == ""


async def test_helpers_run_concurrently(monkeypatch):
    async def slow_hsn(name):
        await asyncio.sleep(0.2)
        return HSN

    async def slow_specs(*a):
        await asyncio.sleep(0.2)
        return SPECS

    async def slow_mod(text):
        await asyncio.sleep(0.2)
        return SAFE

    monkeypatch.setattr(cda, "_fetch_hsn_suggestion", slow_hsn)
    monkeypatch.setattr(cda, "_fetch_spec_suggestions", slow_specs)
    monkeypatch.setattr(cda, "_fetch_moderation", slow_mod)
    assist = cda.DraftAssist()
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await assist.refresh(_draft())
    assert loop.time() - t0 < 0.5
    assert assist.pending_hsn and assist.moderation


async def test_turn_survives_all_helpers_failing(client, db, make_actor):
    user = await make_actor("buyer")
    boom = AsyncMock(side_effect=RuntimeError("down"))
    with patch.object(cda, "_fetch_hsn_suggestion", boom), patch.object(cda, "_fetch_spec_suggestions", boom), \
         patch.object(cda, "_fetch_moderation", boom), _script("Which city?"):
        data = await _msg(user, "I need 600 kg for delivery in Indore", BASE)
    assert data["reply"].endswith("Which city?") and "RFQ draft" not in data["reply"]


async def test_nothing_runs_without_a_product():
    assist = cda.DraftAssist()
    with _Helpers() as h:
        await assist.refresh({"role": "buyer"})
    assert h.hsn.await_count == h.specs.await_count == h.mod.await_count == 0


async def test_user_set_hsn_suppresses_suggestion():
    assist = cda.DraftAssist()
    d = _draft()
    d["product_details"].update({"hsn_code": "1006", "hsn_source": "manual"})
    with _Helpers() as h:
        await assist.refresh(d)
    assert h.hsn.await_count == 0 and assist.pending_hsn is None


# ---- 5: API surface -------------------------------------------------------------
async def test_stream_done_chunk_carries_suggestions_and_moderation(client, db, make_actor):
    user = await make_actor("buyer")
    with _Helpers(), _script("ok"):
        res = await user.post("/ai-chat/stream", json={
            "messages": [{"role": "user", "content": "I need 600 kg for delivery in Indore"}], "current_rfq": BASE,
        })
    last = [json.loads(l[6:]) for l in res.text.splitlines() if l.startswith("data: ")][-1]
    assert last["done"] and last["draft_suggestions"]["hsn"]["code"] == "1006"
    assert last["draft_moderation"]["is_safe"] is True

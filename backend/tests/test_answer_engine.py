"""The tool loop and the verify-retry-fallback policy, with a scripted model (no Ollama)."""

import uuid

import httpx
import pytest

from models.enums import RFQRole
from models.user import User
from services import answer_engine
from tests.test_market_data_tools import seed


def _call(name: str, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def _say(text: str):
    return {"role": "assistant", "content": text}


class ScriptedModel:
    """Replaces the Ollama call; each call pops the next scripted assistant message."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    async def __call__(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        if not self.turns:
            raise AssertionError("model called more often than scripted")
        nxt = self.turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def script(monkeypatch):
    def _install(turns):
        model = ScriptedModel(turns)
        monkeypatch.setattr(answer_engine, "_chat", model)
        return model

    return _install


async def _setup(db, make_actor):
    viewer = await make_actor("buyer")
    for price in (20, 30, 40):
        seller = await make_actor("seller")
        await seed(db, seller, title="Appy Fizz lot", role=RFQRole.SELLER, price=price, per_unit="pcs", qty=1000)
    return await db.get(User, uuid.UUID(viewer.id))


async def _ask(db, user, text="how many sellers of appy fizz and what is the price?", **kw):
    return await answer_engine.answer(
        db, user, history_messages=[{"role": "user", "content": text}],
        system_prompt="sys", user_message=text, **kw,
    )


async def test_tool_call_then_correct_answer_passes(db, make_actor, script):
    user = await _setup(db, make_actor)
    model = script([
        _call("search_listings", product="Appy Fizz"),
        _call("market_price_stats", product="Appy Fizz"),
        _say("There are 3 seller listings from 3 users. Median price is ₹30 per pcs (range ₹20 to ₹40)."),
    ])
    res = await _ask(db, user)
    assert res.verification.ok and not res.used_fallback and not res.retried
    assert [r["tool"] for r in res.tool_results] == ["search_listings", "market_price_stats"]
    assert "₹30" in res.text
    # tool outputs were fed back as role=tool messages
    assert any(m["role"] == "tool" for m in model.seen[-1])


async def test_invented_number_is_retried_and_clean_retry_passes(db, make_actor, script):
    user = await _setup(db, make_actor)
    model = script([
        _call("search_listings", product="Appy Fizz"),
        _say("That is about 4,167 cases at a typical market rate."),
        _say("There are 3 seller listings for Appy Fizz on the platform."),
    ])
    res = await _ask(db, user)
    assert res.retried and not res.used_fallback and res.verification.ok
    assert res.text.startswith("There are 3 seller listings")
    retry_prompt = model.seen[-1][-1]["content"]
    assert "4,167" in retry_prompt and "typical market" in retry_prompt


async def test_invented_twice_falls_back_to_facts_only(db, make_actor, script):
    user = await _setup(db, make_actor)
    script([
        _call("search_listings", product="Appy Fizz"),
        _say("About 4,167 cases."),
        _say("Still 4,167 cases, market equilibrium."),
    ])
    res = await _ask(db, user)
    assert res.used_fallback and res.retried
    assert "4,167" not in res.text and "| distinct active listings | 3 |" in res.text


async def test_no_tools_and_invented_price_falls_back_to_no_data_message(db, make_actor, script):
    user = await _setup(db, make_actor)
    script([_say("Fair price is ₹18 per bottle."), _say("It is ₹18, which matches the market.")])
    res = await _ask(db, user, "is this fair?")
    assert res.used_fallback
    assert res.text.startswith("I don't have verified platform data to answer that yet.")


async def test_ollama_down_returns_gathered_facts(db, make_actor, script):
    user = await _setup(db, make_actor)
    script([_call("search_listings", product="Appy Fizz"), httpx.ConnectError("down")])
    res = await _ask(db, user)
    assert res.used_fallback and res.llm_error
    assert "distinct active listings" in res.text

    script([httpx.ConnectError("down")])
    empty = await _ask(db, user)
    assert empty.used_fallback and empty.llm_error and empty.tool_results == []
    assert empty.text.startswith("I don't have verified platform data")


async def test_action_tool_runs_through_executor(db, make_actor, script):
    user = await _setup(db, make_actor)
    seen = []

    async def executor(name, args):
        seen.append((name, args))
        return {"ok": True, "readiness": {"has_role": True}}

    extra = [{"type": "function", "function": {"name": "update_rfq_draft", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    script([
        _call("update_rfq_draft", product_name="appy fizz", quantity_value=50000),
        _say("Saved: 50,000 pcs of appy fizz."),
    ])
    res = await _ask(db, user, "I want 50000 pcs appy fizz", extra_tools=extra, tool_executor=executor)
    assert seen == [("update_rfq_draft", {"product_name": "appy fizz", "quantity_value": 50000})]
    assert res.action_results[0]["tool"] == "update_rfq_draft"
    assert res.verification.ok and not res.used_fallback


async def test_unoffered_tool_is_refused(db, make_actor, script):
    user = await _setup(db, make_actor)

    async def executor(name, args):  # pragma: no cover - must not run
        raise AssertionError("not offered")

    script([_call("create_rfq", title="x"), _say("I could not create that.")])
    res = await _ask(db, user, "hello", tool_executor=executor)
    assert res.text == "I could not create that." and res.action_results == []


async def test_rfq_context_is_added_to_system_prompt(db, make_actor, script):
    user = await _setup(db, make_actor)
    rid = str(uuid.uuid4())
    model = script([_say("Nothing to add.")])
    await _ask(db, user, "analyze", context=answer_engine.AnswerContext(rfq_id=rid))
    system = model.seen[0][0]["content"]
    assert f"listing {rid}" in system and "Call get_listing first" in system


async def test_round_cap_then_final_answer_without_tools(db, make_actor, script):
    user = await _setup(db, make_actor)
    turns = [_call("search_listings", product="Appy Fizz") for _ in range(answer_engine.MAX_TOOL_ROUNDS)]
    turns.append(_say("There are 3 seller listings."))
    model = script(turns)
    res = await _ask(db, user)
    assert len(model.seen) == answer_engine.MAX_TOOL_ROUNDS + 1
    assert res.verification.ok and res.text == "There are 3 seller listings."

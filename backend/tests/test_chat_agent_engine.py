"""Offline tests of the chat agent loop with a scripted model (no Ollama, no database).

The model decides; these tests script what it decides and check what the code does with it: grounding, the draft,
the two-phase binding actions, honest errors and the wire format.
"""
import copy
import uuid
from types import SimpleNamespace

import pytest

from models.enums import UserRole
from services import ai_chat_service as legacy
from services import categories, llm_client
from services.chat_agent import actions, draft_assist, persistence
from services.chat_agent import engine
from services.chat_agent.state import AgentState


def call(name, **arguments):
    return {"name": name, "arguments": arguments}


class ScriptedLLM:
    """Stands in for ``llm_client.chat``: returns the next scripted reply and records what it was sent."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen = []

    def __call__(self, messages, tools=None, *, timeout, stream=False):
        assert self.steps, "the model was called more often than scripted"
        self.seen.append(copy.deepcopy(messages))
        step = self.steps.pop(0)
        return self._reply(step)

    @staticmethod
    async def _reply(step):
        return {"content": step.get("content", ""), "thinking": step.get("thinking", ""), "tool_calls": step.get("calls", [])}

    def tool_results(self):
        return [m["content"] for msgs in self.seen for m in msgs if m["role"] == "tool"]


class World:
    """An in-memory conversation store behind the legacy persistence functions the engine uses."""

    def __init__(self, monkeypatch):
        self.conv = SimpleNamespace(id=uuid.uuid4(), state={}, title="", rfq_id=None)
        self.messages = []
        self.created = []
        self.user = SimpleNamespace(id=uuid.uuid4(), role=UserRole.BOTH, profile=None, can_post_as=lambda role: True)

        class Db:
            async def commit(self):
                return None

        self.db = Db()

        async def get_or_create(db, user_id, conversation_id=None, initial_title=None):
            return self.conv

        async def save(db, conversation_id, role, content, metadata=None):
            self.messages.append({"role": role, "content": content, "metadata": metadata or {}})

        async def history(db, conversation_id):
            return [{"role": m["role"], "content": m["content"]} for m in self.messages]

        async def update(db, conversation, draft=None, readiness=None, rfq_id=None, user_prompt=None, matched_candidates=None,
                         active_connection_id=None, last_counterparty_message=None, draft_suggestions=None,
                         draft_moderation=None, state_patch=None):
            state = dict(conversation.state)
            if draft is not None:
                state["rfq_draft"] = draft
            for key, value in (state_patch or {}).items():
                state[key] = value
            conversation.state = copy.deepcopy(state)

        async def create_rfq(draft, args, db=None, user=None, assist=None):
            rid = str(uuid.uuid4())
            made = {"id": rid, "title": (draft.get("product_details") or {}).get("name", "RFQ"), "role": draft.get("role"),
                    "status": "active" if args.get("status", "active") == "active" else "draft", "category": draft.get("category")}
            self.created.append(made)
            return made, None

        async def refresh(self_, draft, *, moderate=True):
            return None

        async def block_reason(self_, draft):
            return None

        async def resolve_category(category, title=None, description=None):
            return "Agriculture"

        monkeypatch.setattr(persistence, "get_or_create_conversation", get_or_create)
        monkeypatch.setattr(persistence, "save_message_to_conversation", save)
        monkeypatch.setattr(persistence, "_stored_history", history)
        monkeypatch.setattr(persistence, "update_conversation_after_turn", update)
        monkeypatch.setattr(actions, "execute_create_rfq_call", create_rfq)
        monkeypatch.setattr(draft_assist.DraftAssist, "refresh", refresh)
        monkeypatch.setattr(draft_assist.DraftAssist, "block_reason", block_reason)
        monkeypatch.setattr(categories, "resolve_category", resolve_category)
        self.monkeypatch = monkeypatch

    def script(self, *steps):
        llm = ScriptedLLM(*steps)
        self.monkeypatch.setattr(llm_client, "chat", llm)
        return llm

    async def say(self, text):
        """One user turn: every event, the last one is the final (or error) event."""
        events = [e async for e in engine.run_turn(
            [{"role": "user", "content": text}], db=self.db, user=self.user, conversation_id=self.conv.id
        )]
        return events

    @property
    def agent(self):
        return AgentState.from_dict(self.conv.state.get("agent"))


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


UPDATE_BUY = call(
    "update_rfq_draft",
    fields={
        "role": {"value": "buyer", "said": "want to buy"},
        "product_name": {"value": "Basmati Rice", "said": "basmati rice"},
        "quantity": {"value": 500, "unit": "kg", "said": "500 kg"},
        "price_target": {"amount": None, "currency": "", "per_unit": None, "said": ""},
        "location": {"city": None, "state": "", "country": None, "said": ""},
    },
)


async def test_fills_the_draft_over_two_turns(world):
    llm = world.script(
        {"calls": [UPDATE_BUY]},
        {"content": "Which city should it be delivered to?"},
        {"calls": [call("update_rfq_draft", fields={
            "location": {"city": "Indore", "said": "Indore"},
            "price_target": {"amount": 80, "currency": "INR", "per_unit": "kg", "said": "80 rs per kg"},
            "specifications": {"grain_length": {"value": "8 mm", "said": "8mm"}},
            "quantity": {"value": None, "unit": "", "said": ""},
        })]},
        {"content": "Noted. Any packaging preference?"},
    )
    first = (await world.say("I want to buy 500 kg basmati rice"))[-1]
    assert first.kind == "final" and first.chunk["done"] is True
    assert first.result.reply == "Which city should it be delivered to?"
    assert first.chunk["rfq_draft"]["role"] == "buyer"
    assert first.chunk["rfq_draft"]["product_details"]["name"] == "Basmati Rice"
    assert first.chunk["rfq_draft"]["quantity"] == {"value": 500.0, "unit": "kg"}
    assert first.chunk["rfq_draft"]["category"]  # inferred from the product by code

    second = (await world.say("Indore, 80 rs per kg"))[-1]
    draft = second.chunk["rfq_draft"]
    assert draft["location"]["city"] == "Indore"
    assert draft["price_target"]["amount"] == 80.0 and draft["price_target"]["per_unit"] == "kg"
    assert draft["quantity"] == {"value": 500.0, "unit": "kg"}  # null / "" left it unchanged
    assert "grain_length" not in draft["product_details"]  # the user never said 8 mm
    assert any("not stated" in r for r in llm.tool_results()[1:])
    assert world.agent.draft == draft and not world.created
    assert [m["role"] for m in world.messages] == ["user", "assistant", "user", "assistant"]


async def test_dont_create_yet_creates_nothing(world):
    world.script(
        {"calls": [call("update_rfq_draft", fields={
            "role": {"value": "buyer", "said": "chahiye"},
            "product_name": {"value": "Basmati Rice", "said": "basmati chawal"},
            "quantity": {"value": 500, "unit": "kg", "said": "500 kg"},
        })]},
        {"content": "Theek hai, abhi RFQ nahi banaunga. Kal bataiye."},
    )
    final = (await world.say("mujhe 500 kg basmati chawal chahiye, par abhi rfq mat banao, kal banana"))[-1]
    assert not world.created and final.result.created_rfq is None and final.chunk["created_rfq"] is None
    assert world.agent.pending is None and world.agent.draft["product_details"]["name"] == "Basmati Rice"
    assert "Kal" in final.result.reply


PROPOSE = call("propose_action", action="create_rfq", publish=True)


async def test_propose_then_confirm_creates_exactly_one_rfq(world):
    world.script(
        {"calls": [UPDATE_BUY, PROPOSE]},
        {"content": "Everything is ready. Shall I go ahead?"},  # not the recap: the code shows the real one
        {"calls": [call("confirm_action")]},
        {"content": "Done, it is live."},
        {"calls": [call("confirm_action")]},
        {"content": "Already done."},
    )
    proposal = await world.say("I want to buy 500 kg basmati rice")
    final = proposal[-1]
    assert not world.created and world.agent.pending is not None
    assert "Basmati Rice" in final.result.reply and "publish it now" in final.result.reply
    assert final.chunk["replace_content"] is True and final.chunk["content"] == final.result.reply
    assert final.chunk["quick_replies"] == ["Publish now", "Save as draft", "Change something"]

    confirmed = await world.say("yes, publish")
    last = confirmed[-1]
    assert len(world.created) == 1 and last.result.created_rfq["id"] == world.created[0]["id"]
    assert world.created[0]["status"] == "active"
    assert world.created[0]["id"] in last.result.reply  # the facts come from the tool result, not from the model
    assert any(e.kind == "tool_step" and e.chunk["tool_step"]["name"] == "create_rfq" and e.chunk["created_rfq"] for e in confirmed)
    assert last.chunk["rfq_draft"] == {} and world.agent.pending is None and world.agent.last_rfq_id == world.created[0]["id"]

    await world.say("yes")  # a repeated yes cannot create it again
    assert len(world.created) == 1


async def test_confirm_in_the_same_turn_as_the_proposal_is_refused(world):
    llm = world.script({"calls": [UPDATE_BUY, PROPOSE, call("confirm_action")]}, {"content": "Please confirm."})
    await world.say("I want to buy 500 kg basmati rice")
    assert not world.created and world.agent.pending is not None
    assert "has not answered" in llm.tool_results()[-1]


async def test_confirm_after_the_draft_changed_is_an_error(world):
    llm = world.script(
        {"calls": [UPDATE_BUY, PROPOSE]},
        {"content": "ok"},
        {"calls": [call("update_rfq_draft", fields={"quantity": {"value": 800, "unit": "kg", "said": "800 kg"}}), call("confirm_action")]},
        {"content": "I changed the quantity; please look at the new summary."},
    )
    await world.say("I want to buy 500 kg basmati rice")
    await world.say("yes but make it 800 kg")
    assert not world.created
    assert "draft changed" in llm.tool_results()[-1] and world.agent.pending is None
    assert world.agent.draft["quantity"]["value"] == 800.0


async def test_an_expired_pending_action_cannot_be_confirmed(world):
    world.script({"calls": [UPDATE_BUY, PROPOSE]}, {"content": "ok"}, *[{"content": "hm"} for _ in range(4)], {"calls": [call("confirm_action")]}, {"content": "?"})
    await world.say("I want to buy 500 kg basmati rice")
    for _ in range(4):
        await world.say("tell me about escrow")
    await world.say("yes")
    assert not world.created


async def test_a_prohibited_product_is_dropped_with_a_fixed_reply(world, monkeypatch):
    async def refresh(self_, draft, *, moderate=True):
        self_.moderation = {"text_hash": draft_assist.text_hash(self_._mtext(draft)), "is_safe": False,
                            "reason": "narcotics", "category": "narcotics_and_controlled_substances"}
        self_.moderation_checked_now = True

    monkeypatch.setattr(draft_assist.DraftAssist, "refresh", refresh)
    world.script({"calls": [call("update_rfq_draft", fields={
        "role": {"value": "buyer", "said": "buy"}, "product_name": {"value": "Cannabis", "said": "ganja"},
    })]}, {"content": "Sure, how much ganja?"})
    final = (await world.say("I want to buy ganja"))[-1]
    assert "prohibited" in final.result.reply and final.chunk["rfq_draft"] == {} and world.agent.draft == {}


async def test_llm_down_gives_an_honest_error_and_leaves_the_state_alone(world, monkeypatch):
    def down(messages, tools=None, *, timeout, stream=False):
        raise llm_client.LLMUnavailable("down")

    monkeypatch.setattr(llm_client, "chat", down)
    events = await world.say("I want to buy rice")
    final = events[-1]
    assert final.kind == "error" and "can't be reached" in final.result.reply and final.chunk["done"] is True
    assert "agent" not in world.conv.state and [m["role"] for m in world.messages] == ["user"]  # only the user's message

    monkeypatch.setattr(llm_client, "chat", lambda *a, **k: (_ for _ in ()).throw(llm_client.LLMTimeout("slow")))
    final = (await world.say("मुझे चावल चाहिए"))[-1]
    assert final.kind == "error" and "time laga" in final.result.reply  # Devanagari in, Hinglish error


async def test_the_final_chunk_has_every_field_the_legacy_final_chunk_has(world):
    world.script({"calls": [UPDATE_BUY]}, {"content": "Which city?"})
    events = await world.say("I want to buy 500 kg basmati rice")
    legacy_final = set(legacy._busy_event(world.conv.id).chunk) | {"draft_suggestions", "draft_moderation", "editing_rfq_id"}
    final = events[-1]
    assert legacy_final <= set(final.chunk), legacy_final - set(final.chunk)
    wire_keys = {"content", "thinking", "thinking_after", "tool_step", "rfq_draft", "readiness", "created_rfq",
                 "counterparty_message", "done", "routed_agent", "conversation_id"}
    for event in events:
        assert wire_keys <= set(event.chunk), (event.kind, wire_keys - set(event.chunk))
    assert final.chunk["conversation_id"] == str(world.conv.id)
    response = final.result.as_response()
    assert response["reply"] == "Which city?" and response["rfq_draft"]["role"] == "buyer"
    assert response["readiness"]["required_complete"] is True
    assert [e.kind for e in events].count("final") == 1 and any(e.kind == "tool_step" for e in events)


async def test_thinking_and_text_are_streamed_and_text_before_a_tool_call_is_cleared(world, monkeypatch):
    async def stream(messages, tools, timeout, stream_=True):
        yield {"thinking": "The user wants rice. ", "content": ""}
        yield {"content": "Let me save that. ", "thinking": ""}
        yield {"done": True, "tool_calls": [UPDATE_BUY]}

    async def answer(messages, tools, timeout, stream_=True):
        yield {"content": "Which ", "thinking": ""}
        yield {"content": "city?", "thinking": ""}
        yield {"done": True, "tool_calls": []}

    steps = [stream, answer]
    monkeypatch.setattr(llm_client, "chat", lambda messages, tools=None, *, timeout, stream=False: steps.pop(0)(messages, tools, timeout))
    events = await world.say("I want to buy 500 kg basmati rice")
    assert any(e.chunk["thinking"] for e in events)
    assert any(e.kind == "replace_content" and e.chunk["content"] == "" for e in events)
    assert "".join(e.chunk["content"] for e in events if e.kind == "status" and not e.chunk.get("replace_content")).endswith("Which city?")
    assert events[-1].result.reply == "Which city?" and events[-1].chunk["content"] == ""


async def test_the_role_comes_from_the_account_when_it_can_post_one_side_only(world):
    world.user.role = UserRole.SELLER
    world.user.can_post_as = lambda role: getattr(role, "value", role) == "seller"
    world.script(
        {"calls": [call("update_rfq_draft", fields={"product_name": {"value": "Cotton", "said": "cotton"}})]},
        {"content": "How much cotton?"},
        {"calls": [call("update_rfq_draft", fields={"role": {"value": "buyer", "said": "buy"}})]},
        {"content": "Your account can only sell."},
    )
    first = (await world.say("cotton"))[-1]
    assert first.chunk["rfq_draft"]["role"] == "seller" and world.agent.evidence["role"] == "account"
    llm = world.script({"calls": [call("update_rfq_draft", fields={"role": {"value": "buyer", "said": "buy"}})]}, {"content": "Your account can only sell."})
    second = (await world.say("no I want to buy cotton"))[-1]
    assert second.chunk["rfq_draft"]["role"] == "seller" and "can only post as a seller" in llm.tool_results()[-1]


async def test_run_chat_turn_runs_the_agent_engine(monkeypatch):
    async def fake_agent(*args):
        yield "a"

    monkeypatch.setattr(engine, "run_turn", fake_agent)
    assert [e async for e in legacy._run_chat_turn([{"role": "user", "content": "hi"}])] == ["a"]


async def test_a_stateless_turn_works_from_the_clients_draft(world, monkeypatch):
    world.script({"calls": [call("update_rfq_draft", fields={"quantity": {"value": 600, "unit": "kg", "said": "600 kg"}})]}, {"content": "Noted."})
    events = [e async for e in engine.run_turn(
        [{"role": "assistant", "content": "ignored"}, {"role": "user", "content": "make it 600 kg"}],
        current_rfq={"role": "buyer", "product_details": {"name": "Rice"}, "quantity": {"value": 500, "unit": "kg"}},
    )]
    final = events[-1]
    assert final.kind == "final" and final.chunk["rfq_draft"]["quantity"] == {"value": 600.0, "unit": "kg"}
    assert "conversation_id" not in final.chunk and final.result.conversation_id is None


def _mock_ollama(monkeypatch, lines):
    import json as _json

    import httpx

    def handler(request):
        body = _json.loads(request.content)
        assert body["stream"] is (len(lines) > 1) and body["options"].get("temperature") is not None
        return httpx.Response(200, content="\n".join(_json.dumps(x) for x in lines).encode())

    real = httpx.AsyncClient
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    llm_client.reset_backoff()


async def test_llm_client_chat_parses_a_non_streamed_reply(monkeypatch):
    msg = {"role": "assistant", "content": "", "thinking": "hm", "tool_calls": [
        {"function": {"name": "update_rfq_draft", "arguments": {"fields": {}}}}, {"function": {"name": "x", "arguments": "{\"a\": 1}"}}]}
    _mock_ollama(monkeypatch, [{"message": msg, "done": True}])
    out = await llm_client.chat([{"role": "user", "content": "hi"}], [], timeout=5)
    assert out["thinking"] == "hm" and out["tool_calls"] == [
        {"name": "update_rfq_draft", "arguments": {"fields": {}}}, {"name": "x", "arguments": {"a": 1}}]


async def test_llm_client_chat_streams_deltas_then_the_tool_calls(monkeypatch):
    lines = [
        {"message": {"thinking": "t1"}, "done": False}, {"message": {"content": "Hel"}, "done": False},
        {"message": {"content": "lo", "tool_calls": [{"function": {"name": "platform_info", "arguments": {}}}]}, "done": False},
        {"message": {"content": ""}, "done": True},
    ]
    _mock_ollama(monkeypatch, lines)
    parts = [p async for p in llm_client.chat([{"role": "user", "content": "hi"}], None, timeout=5, stream=True)]
    assert "".join(p.get("content", "") for p in parts) == "Hello" and parts[0]["thinking"] == "t1"
    assert parts[-1] == {"done": True, "tool_calls": [{"name": "platform_info", "arguments": {}}]}


async def test_llm_client_chat_maps_failures_to_typed_errors(monkeypatch):
    import httpx

    def boom(request):
        raise httpx.ConnectError("refused")

    real = httpx.AsyncClient
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(boom), **kw))
    llm_client.reset_backoff()
    with pytest.raises(llm_client.LLMUnavailable):
        await llm_client.chat([], None, timeout=5)
    with pytest.raises(llm_client.LLMUnavailable):  # now in back-off: no second attempt
        llm_client.chat([], None, timeout=5)
    llm_client.reset_backoff()


def _stored_rfq(status="draft"):
    from models.enums import RFQRole, RFQStatus

    return SimpleNamespace(
        id=uuid.uuid4(), user_id=None, title="Basmati Rice", role=RFQRole.BUYER, status=RFQStatus(status), category="Food",
        product_details={"name": "Basmati Rice"}, description=None, quantity_value=500, quantity_unit="kg", min_order_value=None,
        min_order_unit=None, price_amount=None, price_currency=None, price_per_unit=None, location_city="Indore",
        location_state=None, location_country=None, deadline_at=None,
    )


async def test_publishing_a_saved_draft_is_two_phase_too(world, monkeypatch):
    rfq = _stored_rfq("draft")
    rfq.user_id = world.user.id
    published = []

    async def resolve(db, user, ref, statuses):
        return rfq

    async def publish(rfq_id_str=None, conversation=None, db=None, user=None):
        published.append(rfq_id_str)
        return {"id": str(rfq.id), "title": rfq.title, "role": "buyer", "status": "active", "category": "Food"}, None

    monkeypatch.setattr(actions, "_resolve_user_rfq", resolve)
    monkeypatch.setattr(actions, "execute_publish_rfq_call", publish)
    world.script(
        {"calls": [call("propose_action", action="publish_rfq", rfq_id=str(rfq.id))]}, {"content": "Publish it?"},
        {"calls": [call("confirm_action")]}, {"content": "Published."},
    )
    proposal = (await world.say("publish my rice rfq"))[-1]
    assert not published and "publish the saved draft" in proposal.result.reply and proposal.chunk["quick_replies"] == [
        "Yes, publish it", "No, keep it as a draft"]
    done = (await world.say("yes"))[-1]
    assert published == [str(rfq.id)] and done.result.created_rfq["status"] == "active" and str(rfq.id) in done.result.reply
    assert world.agent.last_rfq_id == str(rfq.id)


async def test_publishing_someone_elses_rfq_is_refused(world, monkeypatch):
    rfq = _stored_rfq("draft")
    rfq.user_id = uuid.uuid4()

    async def resolve(db, user, ref, statuses):
        return rfq

    monkeypatch.setattr(actions, "_resolve_user_rfq", resolve)
    llm = world.script({"calls": [call("propose_action", action="publish_rfq", rfq_id=str(rfq.id))]}, {"content": "Not yours."})
    await world.say("publish that rfq")
    assert "not yours" in llm.tool_results()[0] and world.agent.pending is None


# ---------------------------------------------------------------------------
# Phase C: fixes from the first live run
# ---------------------------------------------------------------------------
async def test_rejected_fields_are_listed_prominently_while_the_grounded_ones_apply(world):
    llm = world.script(
        {"calls": [call("update_rfq_draft", fields={
            "role": {"value": "buyer", "said": "मुझे ... चाहिए"},
            "quantity": {"value": 800, "unit": "tonne", "said": "10 टन"},
        })]},
        {"content": "ok"},
    )
    await world.say("मुझे 10 टन चावल चाहिए")
    result = llm.tool_results()[0]
    assert '"ok": true' in result and result.index("REJECTED_FIELDS_NOT_SAVED") < result.index('"result"')
    assert '"field": "quantity"' in result and "10 टन" in result and "re-send" in result
    assert world.agent.draft["role"] == "buyer" and "quantity" not in world.agent.draft


async def test_a_publish_choice_after_the_recap_needs_no_second_confirmation_round(world):
    llm = world.script(
        {"calls": [UPDATE_BUY, call("propose_action", action="create_rfq", publish=False)]},
        {"content": "recap"},
        # "haan, publish kar do": the model re-proposes with publish=True and confirms in the same turn
        {"calls": [call("propose_action", action="create_rfq", publish=True), call("confirm_action")]},
        {"content": "Ho gaya."},
    )
    await world.say("I want to buy 500 kg basmati rice")
    first_turn = world.agent.pending.turn
    last = (await world.say("haan, publish kar do"))[-1]
    assert "already proposed" in llm.tool_results()[-2]
    assert len(world.created) == 1 and world.created[0]["status"] == "active"
    assert last.result.created_rfq["id"] == world.created[0]["id"] and first_turn == 1


async def test_a_reproposal_with_a_changed_draft_still_resets_the_gate(world):
    llm = world.script(
        {"calls": [UPDATE_BUY, PROPOSE]}, {"content": "recap"},
        {"calls": [call("update_rfq_draft", fields={"quantity": {"value": 800, "unit": "kg", "said": "800 kg"}}), PROPOSE, call("confirm_action")]},
        {"content": "please confirm"},
    )
    await world.say("I want to buy 500 kg basmati rice")
    await world.say("make it 800 kg")
    assert not world.created and "has not answered" in llm.tool_results()[-1]


async def test_own_rfqs_are_in_the_prompt_and_the_query_runs_in_a_savepoint(world, monkeypatch):
    from services import market_data_tools

    rid = str(uuid.uuid4())
    events = []

    class Savepoint:
        async def __aenter__(self):
            events.append("begin")

        async def __aexit__(self, *exc):
            events.append("end")
            return False

    world.db.begin_nested = lambda: Savepoint()

    async def my_rfqs(db, *, user, status=None, limit=20, **_):
        return {"ok": True, "data": {"rfqs": [{"id": rid, "title": "Rice 500 kg", "role": "buyer", "status": "draft",
                                               "quantity": "500", "quantity_unit": "kg"}]}}

    monkeypatch.setattr(market_data_tools, "get_my_rfqs", my_rfqs)
    llm = world.script({"content": "Which one?"})
    await world.say("publish my rice rfq")
    system = llm.seen[0][0]["content"]
    assert "YOUR RFQS" in system and rid[:8] in system and "Rice 500 kg" in system
    assert events.count("begin") == events.count("end") >= 1


async def test_a_failing_brief_query_rolls_its_savepoint_back_and_the_turn_goes_on(world, monkeypatch):
    from services import connection_service

    events = []

    class Savepoint:
        async def __aenter__(self):
            events.append("begin")

        async def __aexit__(self, exc_type, *rest):
            events.append(("end", exc_type.__name__ if exc_type else None))
            return False

    world.db.begin_nested = lambda: Savepoint()

    async def boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(connection_service, "list_user_connections", boom)
    world.script({"content": "Hello!"})
    final = (await world.say("hi"))[-1]
    assert final.kind == "final" and final.result.reply == "Hello!"
    assert ("end", "RuntimeError") in events


async def test_a_reply_in_the_users_language_with_every_value_is_shown_as_written(world):
    world.script(
        {"calls": [UPDATE_BUY, PROPOSE]},
        {"content": "Aapka RFQ: Basmati Rice, 500 kg. Ise abhi publish karun ya draft mein save karun?"},
    )
    final = (await world.say("mujhe 500 kg basmati rice chahiye, I want to buy"))[-1]
    assert final.result.reply == "Aapka RFQ: Basmati Rice, 500 kg. Ise abhi publish karun ya draft mein save karun?"


async def test_a_reply_missing_a_value_gets_the_recap_appended_in_the_users_language(world):
    world.script({"calls": [UPDATE_BUY, call("propose_action", action="create_rfq", publish=True, reply_language="hinglish")]},
                 {"content": "Sab kuch ready hai."})
    final = (await world.say("mujhe 500 kg basmati rice chahiye, I want to buy"))[-1]
    assert final.result.reply.startswith("Sab kuch ready hai.") and "Aapka RFQ draft yeh hai" in final.result.reply
    assert "500" in final.result.reply and final.chunk["replace_content"] is True
    assert final.chunk["quick_replies"] == ["Abhi publish karo", "Draft mein save karo", "Kuch badlo"]


async def test_tool_markup_in_the_answer_becomes_chips_and_leaves_the_text(world):
    world.script({"content": 'Are you looking to **buy** or sell? <set_quick_replies options=["Buyer","Seller"]/>'})
    final = (await world.say("hello"))[-1]
    assert final.result.reply == "Are you looking to **buy** or sell?"
    assert final.chunk["quick_replies"] == ["Buyer", "Seller"] and final.result.quick_replies == ["Buyer", "Seller"]
    assert final.chunk["replace_content"] is True and final.chunk["content"] == final.result.reply  # already streamed: replaced


def test_strip_tool_markup_forms():
    from services.chat_agent.tools import strip_tool_markup as strip

    assert strip("Hi <set_quick_replies options=[\"A\", \"B\"]/>") == ("Hi", ["A", "B"], ["set_quick_replies"])
    assert strip("Hi set_quick_replies({\"options\": [\"A\", \"B (x)\"]})") == ("Hi", ["A", "B (x)"], ["set_quick_replies"])
    text, options, found = strip("ok <propose_action action=\"create_rfq\"/> done <b>bold</b>")
    assert text == "ok  done <b>bold</b>" and options == [] and found == ["propose_action"]
    assert strip("Nothing to strip here.") == ("Nothing to strip here.", [], [])


async def test_market_prices_returns_figures_or_an_explicit_no_data(world, monkeypatch):
    from services import market_data_tools

    answers = iter([
        {"ok": True, "data": {"n_listings": 7, "n_distinct_users": 4, "median": 82.5, "p25": 78, "p75": 90, "min": 70, "max": 99,
                              "currency": "INR", "unit_basis": "kg", "by_role": {"seller": {"n": 5, "median": 84}}}, "notes": []},
        {"ok": True, "data": {"n_listings": 0}, "notes": ["No priced active listings found on the platform for this query."]},
    ])
    seen = []

    async def stats(db, *, user, product=None, role=None, city=None, **_):
        seen.append((product, role, city))
        return next(answers)

    monkeypatch.setattr(market_data_tools, "market_price_stats", stats)
    llm = world.script(
        {"calls": [call("market_prices", product="basmati rice", location="Indore", role="seller")]}, {"content": "About 82.5 INR/kg."},
        {"calls": [call("market_prices", product="unobtainium")]}, {"content": "No data."},
    )
    await world.say("what is the current price of basmati rice on the platform?")
    await world.say("and unobtainium?")
    first, second = llm.tool_results()
    assert seen == [("basmati rice", "seller", "Indore"), ("unobtainium", None, None)]
    assert '"median": 82.5' in first and "INR/kg" in first and '"listings": 7' in first
    assert "no data on the platform" in second and '"listings": 0' in second


async def test_cancel_with_discard_says_draft_discarded_and_empties_the_wire_draft(world):
    llm = world.script(
        {"calls": [UPDATE_BUY]}, {"content": "ok"},
        {"calls": [call("cancel_action", discard_draft=True)]}, {"content": "Draft discarded."},
    )
    await world.say("I want to buy 500 kg basmati rice")
    events = await world.say("cancel it")
    assert "draft discarded" in llm.tool_results()[-1]
    assert events[-1].chunk["rfq_draft"] == {} and world.agent.draft == {}
    assert any(e.kind == "status" and e.chunk["rfq_draft"] == {} for e in events[:-1])


def test_the_prompt_tells_the_model_the_phase_c_rules():
    from services.chat_agent import prompt

    text = prompt.build_system_prompt(
        user_name=None, postable_roles=["buyer"], draft={}, readiness={}, open_specs=[],
        own_rfqs=[{"id": "x", "short_id": "abcd1234", "title": "Rice"}],
    )
    for needle in ("fix them in the same turn", "confirm_action in THIS turn", "YOUR RFQS", "market_prices", "never start a new draft"):
        assert needle in text or needle.lower() in text.lower()


# ---------------------------------------------------------------------------
# Phase D: live-run fixes
# ---------------------------------------------------------------------------
def _prompt(**kw):
    from services.chat_agent import prompt

    base = dict(user_name=None, postable_roles=["buyer"], draft={}, readiness={}, open_specs=[])
    return prompt.build_system_prompt(**{**base, **kw})


def test_the_language_rule_is_the_last_thing_in_the_prompt_and_hinglish_is_not_forced():
    from services.chat_agent import prompt

    long_msg = "I want to buy 1,00,000 kg basmati rice delivered to Pune"
    text = _prompt(lang_hint="hinglish", latest_message=long_msg)
    assert text.rstrip().endswith(prompt.LANGUAGE_RULE) and "language AND script of the user's LATEST message" in text
    assert "conversation so far is in Hinglish" not in text
    assert "conversation so far is in Hinglish" in _prompt(lang_hint="hinglish", latest_message="haan")
    assert "'haan'" not in text.split("LANGUAGE:")[0]  # the only 'haan' example is inside the language rule


def test_the_prompt_carries_the_phase_d_rules():
    text = _prompt().lower()
    for needle in ("never list them in your text", "standard english name", "never ask for them first", "search_listings",
                   "publish_rfq with its id", "most_recent_active"):
        assert needle in text


def test_tool_specs_say_what_they_must():
    from services.chat_agent import tools

    specs = {s["function"]["name"]: s["function"] for s in tools.tool_specs()}
    assert "never ask the user for them first" in specs["market_prices"]["description"]
    assert "standard English" in specs["update_rfq_draft"]["parameters"]["properties"]["fields"]["description"]
    assert specs["search_listings"]["parameters"]["required"] == ["product"]


def test_a_known_city_gets_its_standard_spelling_state_and_country():
    from services.chat_agent import tools

    kw = {"city": "pune"}
    tools._ground_location(kw)
    assert kw["city"] == "Pune" and kw["country"] == "India" and kw.get("state")
    kw = {"city": "Pune", "state": "Said State"}
    tools._ground_location(kw)
    assert kw["state"] == "Said State"  # what the user named is kept
    kw = {"city": "Nowhereville"}
    tools._ground_location(kw)
    assert kw == {"city": "Nowhereville"}


def test_quick_reply_options_may_be_objects_and_a_trailing_chip_block_is_stripped():
    from services.chat_agent import tools

    options = tools._clean_options([{"payload": {"x": 1}, "title": "Delhi (buyer)"}, {"label": "Mumbai (buyer)"}, "Pune", {"payload": 1}])
    assert options == ["Delhi (buyer)", "Mumbai (buyer)", "Pune"]
    text = "Kis shehar mein? Yeh bataiye.\n\n**Quick replies:**\n- Delhi (buyer)\n- Mumbai (buyer)\n* Pune\n"
    assert tools.strip_chip_block(text, options) == "Kis shehar mein? Yeh bataiye."
    assert tools.strip_chip_block("Which city?\n1. Delhi (buyer)\n2. Mumbai (buyer)", options) == "Which city?"
    keep = "I can help with Delhi (buyer) or Mumbai (buyer) listings. Which?"
    assert tools.strip_chip_block(keep, options) == keep
    one = "Pick one:\n- Delhi (buyer)"
    assert tools.strip_chip_block(one, options) == one  # a single matching line is prose, not a chip block


async def test_chips_given_as_objects_show_and_the_text_does_not_repeat_them(world):
    world.script(
        {"calls": [call("set_quick_replies", options=[{"payload": {"city": "Delhi"}, "title": "Delhi (buyer)"}, {"title": "Mumbai (buyer)"}])]},
        {"content": "Which city, please?\n\n**Quick replies:**\n- Delhi (buyer)\n- Mumbai (buyer)"},
    )
    final = (await world.say("hello"))[-1]
    assert final.result.reply == "Which city, please?"
    assert final.chunk["quick_replies"] == ["Delhi (buyer)", "Mumbai (buyer)"]


async def test_the_update_stores_the_english_city_with_state_and_country(world):
    world.script({"calls": [call("update_rfq_draft", fields={
        "role": {"value": "buyer", "said": "खरीदना"}, "product_name": {"value": "Tomato", "said": "टमाटर"},
        "location": {"city": "Indore", "said": "इंदौर में"},
    })]}, {"content": "ok"})
    await world.say("मुझे इंदौर में टमाटर खरीदना है")
    loc = world.agent.draft["location"]
    assert loc["city"] == "Indore" and loc.get("country") == "India" and loc.get("state")


async def test_search_listings_shows_cards_and_never_starts_a_draft(world, monkeypatch):
    from services import market_data_tools

    rid = str(uuid.uuid4())
    seen = []

    async def search(db, *, user, product=None, role=None, city=None, **_):
        seen.append((product, role, city))
        rows = [{"id": rid, "title": "Haldi 2 t", "role": "seller", "price": 90, "currency": "INR", "price_per_unit": "kg",
                 "quantity": 2, "quantity_unit": "tonne", "city": "Erode"}] if product == "turmeric" else []
        return {"ok": True, "tool": "search_listings", "data": {"samples": rows}, "notes": []}

    async def payloads(db, user, results):
        return [{"id": rid, "title": "Haldi 2 t", "score": None}]

    monkeypatch.setattr(market_data_tools, "search_listings", search)
    monkeypatch.setattr(actions, "_match_payloads", payloads)
    llm = world.script(
        {"calls": [call("search_listings", product="turmeric", side="seller")]}, {"content": "One seller in Erode."},
        {"calls": [call("search_listings", product="unobtainium", side="seller")]}, {"content": "No sellers of unobtainium."},
    )
    final = (await world.say("show me sellers of haldi"))[-1]
    assert seen == [("turmeric", "seller", None)]
    assert final.result.matched_candidates == [{"id": rid, "title": "Haldi 2 t", "score": None}]
    assert world.agent.draft == {}
    await world.say("and unobtainium sellers")
    assert "nothing is listed for 'unobtainium'" in llm.tool_results()[-1]


async def test_own_rfqs_show_price_and_place_and_mark_the_most_recent_active(world, monkeypatch):
    from services import market_data_tools
    from services.chat_agent import tools

    a, b, c = (str(uuid.uuid4()) for _ in range(3))

    async def my_rfqs(db, *, user, status=None, limit=20, **_):
        return {"ok": True, "data": {"rfqs": [
            {"id": a, "title": "Wheat", "role": "buyer", "status": "closed", "quantity": 5, "quantity_unit": "t"},
            {"id": b, "title": "Rice 500 kg WP4A", "role": "buyer", "status": "active", "quantity": "500.0", "quantity_unit": "kg",
             "price": "50.00", "currency": "INR", "price_per_unit": "kg", "city": "Pune"},
            {"id": c, "title": "Sugar", "role": "buyer", "status": "active"},
        ]}}

    monkeypatch.setattr(market_data_tools, "get_my_rfqs", my_rfqs)
    ctx = tools.ToolContext(state=AgentState(), assist=draft_assist.DraftAssist({}), user_texts=[], db=world.db, user=world.user)
    world.db.begin_nested = lambda: _NoSavepoint()
    rows = await tools.own_rfqs_brief(ctx)
    assert [r["id"] for r in rows] == [a, b, c]
    assert rows[1]["price"] == "50 INR/kg" and rows[1]["place"] == "Pune" and rows[1]["qty"] == "500 kg"
    assert [r.get("most_recent_active") for r in rows] == [None, True, None]
    assert "most recently updated first" in _prompt(own_rfqs=rows)


class _NoSavepoint:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


async def test_publishing_a_draft_that_lacks_the_essentials_says_what_blocks_it(world, monkeypatch):
    rfq = _stored_rfq("draft")
    rfq.user_id = world.user.id
    rfq.title = "Rice 500 kg WP4A"

    async def resolve(db, user, ref, statuses):
        return rfq

    monkeypatch.setattr(actions, "_resolve_user_rfq", resolve)
    monkeypatch.setattr("services.chat_agent.rfq_draft.evaluate_rfq_readiness",
                        lambda d: {"required_complete": False, "missing_required": ["product"], "missing_optional": []})
    llm = world.script({"calls": [call("propose_action", action="publish_rfq", rfq_id=str(rfq.id))]}, {"content": "It needs a product."})
    await world.say("publish my rice rfq")
    assert "cannot publish 'Rice 500 kg WP4A' yet, missing: product" in llm.tool_results()[-1]


async def test_publish_and_close_recaps_name_the_rfq_and_carry_the_chips(world, monkeypatch):
    rfq = _stored_rfq("draft")
    rfq.user_id = world.user.id
    rfq.title = "Rice 500 kg WP4A"

    async def resolve(db, user, ref, statuses):
        return rfq

    monkeypatch.setattr(actions, "_resolve_user_rfq", resolve)
    world.script({"calls": [call("propose_action", action="publish_rfq", rfq_id=str(rfq.id))]}, {"content": "Publish?"})
    pub = (await world.say("publish my rice rfq"))[-1]
    assert "Rice 500 kg WP4A" in pub.result.reply and str(rfq.id)[:8] in pub.result.reply
    assert pub.chunk["quick_replies"] == ["Yes, publish it", "No, keep it as a draft"]

    rfq.status = type(rfq.status)("active")
    world.script({"calls": [call("cancel_action")]}, {"content": "ok"}, {"calls": [call("propose_action", action="close_rfq", rfq_id=str(rfq.id))]}, {"content": "Close?"})
    await world.say("not now")
    close = (await world.say("cancel this request"))[-1]
    assert "Rice 500 kg WP4A" in close.result.reply and str(rfq.id)[:8] in close.result.reply
    assert "close" in close.chunk["quick_replies"][0].lower()


# ---------------------------------------------------------------------------
# Phase E: live run 3 fixes
# ---------------------------------------------------------------------------
async def test_llm_client_chat_normalises_exotic_spaces_and_hyphens_in_content_and_tool_args(monkeypatch):
    dirty = "Rice\u202f500\u00a0kg WP4A\u2011draft “ok” — x\u200by\ufeffz\u2009\u2010\u2012"
    clean = "Rice 500 kg WP4A-draft “ok” — xyz --"
    msg = {"role": "assistant", "content": dirty, "thinking": "a\u202fb", "tool_calls": [
        {"function": {"name": "update_rfq_draft", "arguments": {"fields": {"title": dirty, "n": [dirty], "k": 5}}}}]}
    _mock_ollama(monkeypatch, [{"message": msg, "done": True}])
    out = await llm_client.chat([{"role": "user", "content": "hi"}], [], timeout=5)
    assert out["content"] == clean and out["thinking"] == "a\u202fb"
    assert out["tool_calls"][0]["arguments"] == {"fields": {"title": clean, "n": [clean], "k": 5}}


async def test_llm_client_chat_normalises_streamed_deltas(monkeypatch):
    _mock_ollama(monkeypatch, [{"message": {"content": "Rice\u202f500"}, "done": False}, {"message": {"content": "\u2011x"}, "done": True}])
    parts = [p async for p in llm_client.chat([{"role": "user", "content": "hi"}], None, timeout=5, stream=True)]
    assert "".join(p.get("content", "") for p in parts) == "Rice 500-x"


@pytest.mark.parametrize("user_text, model_reply, fact", [
    ("cancel it", "Your draft has been cancelled and cleared.", "Draft discarded."),
    ("cancel it", "Done: draft discarded.", None),
    ("mujhe ye chawal wala draft cancel karna hai", "Theek hai, ho gaya.", "Draft discard kar diya."),
    ("ये ड्राफ्ट रद्द करो", "ठीक है।", "ड्राफ्ट हटा दिया गया।"),
])
async def test_discarding_the_draft_is_always_stated(world, user_text, model_reply, fact):
    lang = "hinglish" if "mujhe" in user_text else "en"
    world.script({"calls": [call("cancel_action", discard_draft=True, reply_language=lang)]}, {"content": model_reply})
    reply = (await world.say(user_text))[-1].result.reply
    if fact:
        assert fact in reply and model_reply in reply
    else:
        assert reply == model_reply


def test_with_no_draft_and_a_live_rfq_the_prompt_says_what_this_request_means():
    rows = [{"id": "x", "short_id": "ab12cd34", "title": "Rice 500 kg WP4A", "status": "active", "most_recent_active": True}]
    text = _prompt(own_rfqs=rows)
    assert "No draft is open" in text and "ab12cd34 'Rice 500 kg WP4A'" in text and "propose_action close_rfq" in text
    assert "No draft is open" not in _prompt(own_rfqs=rows, draft={"role": "buyer"})
    assert "No draft is open" not in _prompt(own_rfqs=[{**rows[0], "most_recent_active": False}])
    assert "No draft is open" not in _prompt()


def test_the_prompt_forbids_invented_price_chips():
    text = _prompt()
    assert "Never offer prices, quantities or dates as chips unless they come from a tool result" in text


async def test_a_repeated_close_proposal_keeps_the_chips_under_the_message(world, monkeypatch):
    rfq = _stored_rfq("active")
    rfq.user_id = world.user.id
    rfq.title = "Rice 500 kg WP4A"

    async def resolve(db, user, ref, statuses):
        return rfq

    monkeypatch.setattr(actions, "_resolve_user_rfq", resolve)
    propose = call("propose_action", action="close_rfq", rfq_id=str(rfq.id))
    world.script({"calls": [propose]}, {"content": "Close?"}, {"calls": [propose]}, {"content": "Should I close it?"})
    first = (await world.say("cancel this request"))[-1]
    again = (await world.say("hmm"))[-1]
    assert "close" in first.chunk["quick_replies"][0].lower()
    assert again.chunk["quick_replies"] == first.chunk["quick_replies"]


def test_pick_lang_uses_the_models_language_then_the_script_never_words():
    from services.chat_agent import recaps

    assert recaps.pick_lang("mujhe chawal chahiye", None) == "en"  # no word lists: Latin text alone says nothing
    assert recaps.pick_lang("mujhe chawal chahiye", None, "hinglish") == "hinglish"
    assert recaps.pick_lang("मुझे चावल चाहिए", None) == "hindi_devanagari"
    assert recaps.pick_lang("मुझे चावल चाहिए", None, "english") == "en"
    assert recaps.pick_lang("मुझे चावल चाहिए", None, "hindi_devanagari") == "hindi_devanagari"
    assert recaps.pick_lang("500 kilo ka quote bhej do", None, "hindi_devanagari") == "hinglish"  # script guard: no Devanagari in, none out
    assert recaps.pick_lang("500 kilo ka quote bhej do", None, "hi") == "hinglish"
    assert recaps.pick_lang("मुझे चावल", None, "hi") == "hindi_devanagari" and recaps.pick_lang("x", None, "en") == "en"
    assert recaps.pick_lang("haan", "hinglish") == "hinglish" and recaps.pick_lang("yes please publish it now", "hinglish") == "en"
    

def test_reply_language_is_a_parameter_of_the_three_tools_that_write_text():
    from services.chat_agent import tools

    for name in ("propose_action", "cancel_action", "set_quick_replies"):
        spec = tools.TOOLS[name].schema["function"]["parameters"]["properties"]["reply_language"]
        assert spec["enum"] == ["english", "hinglish", "hindi_devanagari"]


async def test_reply_language_switches_the_chips_and_the_conversations_hint(world):
    world.script({"calls": [UPDATE_BUY, call("propose_action", action="create_rfq", publish=True, reply_language="hinglish")]}, {"content": "ok"})
    final = (await world.say("mujhe 500 kg basmati rice chahiye, I want to buy"))[-1]
    assert final.chunk["quick_replies"] == ["Abhi publish karo", "Draft mein save karo", "Kuch badlo"]
    assert world.conv.state["agent"]["lang_hint"] == "hinglish"


async def test_role_as_a_bare_string_is_accepted_without_a_quote(world):
    llm = world.script(
        {"calls": [call("update_rfq_draft", fields={
            "role": "buyer", "product_name": {"value": "Onion", "said": "pyaz"},
            "quantity": {"value": 50, "unit": "quintal", "said": "50 quintal"}})]},
        {"content": "Which city?"},
    )
    out = (await world.say("mujhe 50 quintal pyaz chahiye nashik delivery"))[-1]
    assert out.chunk["rfq_draft"]["role"] == "buyer"
    assert not any("role" in r and "rejected" in r.lower() for r in llm.tool_results())

"""/message and /stream run the same turn: same text, same stored messages and metadata."""

import json
import re
from unittest.mock import patch

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

RICE_DRAFT = {
    "role": "buyer",
    "category": "Agriculture",
    "product_details": {"name": "basmati rice"},
    "quantity": {"value": 500.0, "unit": "kg"},
}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _script(content: str):
    """The model's scripted reply (the answer engine calls services.answer_engine._chat)."""

    async def chat(messages, tools):
        return {"role": "assistant", "content": content}

    return patch("services.answer_engine._chat", chat)


def _fold(body: str) -> dict:
    """What the frontend shows after the stream: content appends, replace_content replaces."""
    shown, last = "", {}
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        chunk = json.loads(line[6:])
        if chunk.get("replace_content"):
            shown = chunk["content"]
        else:
            shown += chunk.get("content") or ""
        last = chunk
    assert last.get("done") is True
    return {"text": shown, "final": last}


async def _stored(db, user_id) -> list[dict]:
    from sqlalchemy import select

    from models.conversation import Conversation, Message

    rows = (await db.execute(
        select(Message.role, Message.content, Message.msg_metadata)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.user_id == user_id)
        .order_by(Message.created_at)
    )).all()
    out = []
    for role, content, meta in rows:
        meta = json.loads(UUID_RE.sub("<id>", json.dumps(meta or {}, sort_keys=True)))
        out.append({"role": getattr(role, "value", str(role)), "content": UUID_RE.sub("<id>", content), "meta": meta})
    return out


async def _run_both(make_actor, db, make_rfq, body, setup=None):
    """Run the same request through both endpoints as two identical users."""
    results = {}
    for path in ("message", "stream"):
        user = await make_actor("buyer")
        if setup:
            await setup(user, make_rfq)
        res = await user.post(f"/ai-chat/{path}", json=body)
        assert res.status_code == 200, res.text
        if path == "message":
            data = res.json()
            results[path] = {"text": data["reply"], "routed": data["routed_agent"], "created": data["created_rfq"]}
        else:
            folded = _fold(res.text)
            results[path] = {
                "text": folded["text"], "routed": folded["final"]["routed_agent"],
                "created": folded["final"]["created_rfq"],
            }
        results[path]["stored"] = await _stored(db, user.id)
    return results["message"], results["stream"]


def _normalise(result: dict) -> dict:
    return {
        "text": UUID_RE.sub("<id>", result["text"]).strip(),
        "routed": result["routed"],
        "created": bool(result["created"]),
        "stored": result["stored"],
    }


async def test_a_lookup_turn_is_identical_on_both_endpoints(client, db, make_actor, make_rfq):
    body = {"messages": [{"role": "user", "content": "what does the rice market look like?"}]}
    # The scripted reply makes a marketplace claim without a tool call, so the verifier swaps in the facts-only answer.
    with _script("There are 12 seller listings for rice at INR 80 per kg."):
        msg, stream = await _run_both(make_actor, db, make_rfq, body)
    assert _normalise(msg) == _normalise(stream)
    assert [m["role"] for m in msg["stored"]] == ["user", "assistant"]
    assert "12" not in msg["text"] and "verified platform data" in msg["text"]
    assert msg["stored"][1]["meta"]["routed_agent"]["id"] == "market_research"


async def test_close_rfq_shortcut_works_on_message_and_stream(client, db, make_actor, make_rfq):
    async def setup(user, make_rfq):
        await make_rfq(user, role="buyer", title="Need basmati rice", product="basmati rice")

    body = {"messages": [{"role": "user", "content": "close my basmati rfq"}]}
    # No model reply is scripted: the shortcut must not need the model.
    with patch("services.answer_engine._chat", side_effect=AssertionError("model must not be called")):
        msg, stream = await _run_both(make_actor, db, make_rfq, body, setup)
    assert "RFQ Closed" in msg["text"]
    assert _normalise(msg) == _normalise(stream)
    assert msg["stored"][1]["meta"]["tool_step"]["name"] == "close_rfq"
    assert msg["stored"][1]["meta"]["tool_step"]["status"] == "completed"
    closed = (await db.execute(text("select count(*) from rfqs where status = 'closed'"))).scalar_one()
    assert closed == 2


async def test_create_rfq_shortcut_is_identical_on_both_endpoints(client, db, make_actor, make_rfq):
    body = {"messages": [{"role": "user", "content": "create the rfq"}], "current_rfq": RICE_DRAFT}
    with patch("services.answer_engine._chat", side_effect=AssertionError("model must not be called")):
        msg, stream = await _run_both(make_actor, db, make_rfq, body)
    assert msg["created"] and stream["created"]
    assert _normalise(msg) == _normalise(stream)
    assert msg["stored"][1]["meta"]["created_rfq"] is not None


async def test_unbacked_creation_claim_is_corrected_identically(client, db, make_actor, make_rfq):
    body = {"messages": [{"role": "user", "content": "looks good"}], "current_rfq": RICE_DRAFT}
    with _script("✅ **RFQ Created Successfully**\nRFQ ID: `rfq_created_1`"):
        msg, stream = await _run_both(make_actor, db, make_rfq, body)
    assert "has not been created yet" in msg["text"]
    assert _normalise(msg) == _normalise(stream)
    assert (await db.execute(text("select count(*) from rfqs"))).scalar_one() == 0


async def test_model_outage_reply_is_identical(client, db, make_actor, make_rfq):
    async def down(messages, tools):
        raise RuntimeError("ollama down")

    body = {"messages": [{"role": "user", "content": "what is the price of cotton per kg"}]}
    with patch("services.answer_engine._chat", down):
        msg, stream = await _run_both(make_actor, db, make_rfq, body)
    assert _normalise(msg) == _normalise(stream)


# ---- match cards reach the frontend, in the MatchCandidate shape ---------------------------

MATCH_KEYS = {
    "rfq_id", "role", "rank", "title", "category", "description", "quantity", "price", "location", "deadline",
    "product_details", "search_tags", "distance_km", "logistics", "counterparty", "score",
}


def _scripted_tool_turn(tool: str, **args):
    """Round 1: the model calls a read tool. Round 2: it answers from the result."""
    state = {"n": 0}

    async def chat(messages, tools):
        state["n"] += 1
        if state["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": tool, "arguments": args}}]}
        return {"role": "assistant", "content": "Matches found."}

    return patch("services.answer_engine._chat", chat)


async def _seed(make_actor, make_rfq):
    seller = await make_actor("seller", company_name="Rice Traders")
    await make_rfq(seller, role="seller", title="Supplying basmati rice", product="basmati rice", category="Agriculture")
    buyer = await make_actor("buyer")
    mine = await make_rfq(buyer, role="buyer", title="Need basmati rice", product="basmati rice", category="Agriculture")
    return buyer, mine


def _stream_cards(body: str):
    done = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")][-1]
    return done.get("matched_candidates")


async def test_find_matches_turn_sends_matchcandidate_cards_on_both_endpoints(client, db, make_actor, make_rfq):
    buyer, mine = await _seed(make_actor, make_rfq)
    body = {"messages": [{"role": "user", "content": "find suppliers for my rice rfq"}]}
    with _scripted_tool_turn("find_matches", rfq_id=mine["id"]):
        res = await buyer.post("/ai-chat/stream", json=body)
    cards = _stream_cards(res.text)
    assert cards and set(cards[0]) == MATCH_KEYS
    assert cards[0]["counterparty"]["company_name"] == "Rice Traders"
    assert 0 <= cards[0]["score"]["total"] <= 1
    assert cards[0]["counterparty"]["email"] is None  # contact stays hidden until accepted

    with _scripted_tool_turn("find_matches", rfq_id=mine["id"]):
        res = await buyer.post("/ai-chat/message", json=body)
    assert res.status_code == 200
    assert res.json()["matched_candidates"][0]["rfq_id"] == cards[0]["rfq_id"]
    state = (await db.execute(text("select state from conversations order by created_at desc limit 1"))).scalar_one()
    assert state["matched_candidates"][0]["rfq_id"] == cards[0]["rfq_id"]


async def test_search_listings_cards_are_unscored_not_invented(client, db, make_actor, make_rfq):
    buyer, _ = await _seed(make_actor, make_rfq)
    with _scripted_tool_turn("search_listings", product="basmati rice", role="seller"):
        res = await buyer.post("/ai-chat/stream", json={"messages": [{"role": "user", "content": "sellers of basmati rice"}]})
    cards = _stream_cards(res.text)
    assert cards and set(cards[0]) == MATCH_KEYS and cards[0]["score"] is None


async def test_turn_without_tool_rows_sends_no_cards(client, db, make_actor, make_rfq):
    buyer, _ = await _seed(make_actor, make_rfq)
    body = {"messages": [{"role": "user", "content": "hello there"}]}
    with _script("Noted."):
        stream = await buyer.post("/ai-chat/stream", json=body)
        msg = await buyer.post("/ai-chat/message", json=body)
    assert not _stream_cards(stream.text)
    assert msg.json()["matched_candidates"] is None


async def test_client_cards_are_still_echoed_when_no_tool_rows(client, db, make_actor, make_rfq):
    buyer, _ = await _seed(make_actor, make_rfq)
    client_cards = [{"rfq_id": "11111111-1111-1111-1111-111111111111", "title": "x"}]
    body = {"messages": [{"role": "user", "content": "hello there"}], "matched_candidates": client_cards}
    with _script("Noted."):
        stream = await buyer.post("/ai-chat/stream", json=body)
    assert _stream_cards(stream.text) == client_cards

"""Regressions from a seller's conversation that the Super Agent mishandled.

Conversation 33d2d7de: a seller drafted "suppy basmati rice", which became a
BUYER RFQ; "rais the rfq" produced a fake "RFQ Created" reply with no RFQ;
"send the message to this buyer ..." was refused as off-topic.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.conversation import Message
from models.enums import RFQRole
from models.rfq import RFQ
from services import agent_router, query_extractor, rfq_tool_service
from services.ai_chat_service import (
    _connection_relevance,
    claims_rfq_created,
    is_false_positive_refusal,
)

SUPPY_DRAFT_MSG = "can you draft the rfq for suppy basmati rice in indore at 93000 ruppes per tonnes organic farmered rice"
QUANTITY_MSG = "quantity will be 200 tonnes and can delevered in next 4 weeks"
RAISE_MSG = "rais the rfq"
ROLE_SWITCH_MSG = "udpate the rfq fron buyer to seller"
MESSAGE_BUYER_MSG = (
    'send the message to this buyer "Thanks to connecting with you " and add more from your side '
    "that buy the basmati rice from us...and so on"
)
SELLER_DRAFT = {"role": "seller", "category": "Agriculture", "product_details": {"name": "basmati rice"}}


# =============================================================================
# Role detection: no stated role must never mean BUYER
# =============================================================================

@pytest.mark.parametrize(
    "message,expected",
    [
        (SUPPY_DRAFT_MSG, RFQRole.SELLER),
        (QUANTITY_MSG, None),
        (RAISE_MSG, None),
        ("create rfq", None),
        (ROLE_SWITCH_MSG, RFQRole.SELLER),
        ("udpate the rfq fron seller to buyer", RFQRole.BUYER),
        ("make it a seller listing", RFQRole.SELLER),
        (MESSAGE_BUYER_MSG, RFQRole.SELLER),
        ("i want to buy 500kg apples in indore", RFQRole.BUYER),
        ("i want to supply rice", RFQRole.SELLER),
    ],
)
def test_detect_role_only_reports_a_stated_role(message, expected):
    assert query_extractor.detect_role(message) == expected


@pytest.mark.parametrize("message", [QUANTITY_MSG, "delivery to indore please", "price is 93000 per tonne"])
def test_neutral_messages_keep_a_sellers_draft_role(message):
    draft = rfq_tool_service.preprocess_message_to_rfq(message, dict(SELLER_DRAFT))
    assert draft["role"] == "seller"


def test_suppy_typo_drafts_a_seller_rfq():
    draft = rfq_tool_service.preprocess_message_to_rfq(SUPPY_DRAFT_MSG, {})
    assert draft["role"] == "seller"


@pytest.mark.parametrize("message", [QUANTITY_MSG, RAISE_MSG, ROLE_SWITCH_MSG, "create rfq"])
def test_typos_and_commands_are_not_products(message):
    assert query_extractor.extract(message).product is None


# =============================================================================
# Intent detection
# =============================================================================

@pytest.mark.parametrize("message", [RAISE_MSG, "raise it", "please raise the rfq"])
def test_raise_is_a_create_request(message):
    assert rfq_tool_service.check_create_rfq_intent(message) is True


@pytest.mark.parametrize(
    "message",
    [MESSAGE_BUYER_MSG, "write a note for the suppliers", "reply to this buyer", "send a message to the buyer"],
)
def test_messaging_a_counterparty_is_detected_without_a_connection(message):
    assert rfq_tool_service.check_counterparty_message_intent(message) is True


def test_editing_own_rfq_is_not_a_counterparty_message_during_a_thread():
    assert rfq_tool_service.check_counterparty_message_intent(ROLE_SWITCH_MSG, has_active_connection=True) is False


# =============================================================================
# Router: rules win over the small model for unambiguous actions
# =============================================================================

def _mock_ai(data):
    return patch.object(agent_router, "_extract_semantic_intent_with_ai", AsyncMock(return_value=data))


def _tool_names(tools):
    return {t["function"]["name"] for t in tools}


@pytest.mark.asyncio
async def test_message_request_routes_to_negotiation_even_when_model_says_rfq_drafting():
    ai = {"product": "basmati rice", "role": "buyer", "intent": "rfq_drafting", "action_needed": "none"}
    with _mock_ai(ai):
        agent, tools, _ = await agent_router.route_query_to_agent_async(MESSAGE_BUYER_MSG, current_draft=SELLER_DRAFT)
    assert agent.id == "negotiation"
    assert "draft_counterparty_message" in _tool_names(tools)
    assert "create_rfq" not in _tool_names(tools)


@pytest.mark.asyncio
async def test_raise_the_rfq_gets_the_create_tool_even_when_model_offers_none():
    ai = {"product": "rais", "role": "buyer", "intent": "rfq_drafting", "action_needed": "none"}
    with _mock_ai(ai):
        agent, tools, _ = await agent_router.route_query_to_agent_async(RAISE_MSG, current_draft=SELLER_DRAFT)
    assert agent.id == "rfq_drafting"
    assert "create_rfq" in _tool_names(tools)


@pytest.mark.asyncio
async def test_off_schema_send_message_action_is_understood():
    ai = {"product": None, "role": None, "intent": None, "action_needed": "send_message"}
    with _mock_ai(ai):
        agent, tools, _ = await agent_router.route_query_to_agent_async("pls draft something nice for them")
    assert agent.id == "negotiation"
    assert _tool_names(tools) == {"draft_counterparty_message"}


@pytest.mark.asyncio
async def test_quantity_update_mid_draft_is_not_market_research():
    ai = {"product": "delevered", "role": "buyer", "intent": "market_research", "action_needed": "none"}
    with _mock_ai(ai):
        agent, tools, meta = await agent_router.route_query_to_agent_async(QUANTITY_MSG, current_draft=SELLER_DRAFT)
    assert agent.id == "rfq_drafting"
    assert "update_rfq_draft" in _tool_names(tools)
    # The model's role guess is not reconciled with a role the user never stated.
    assert meta["semantic_extraction"]["role"] == "buyer"


# =============================================================================
# Guards on the model's reply
# =============================================================================

@pytest.mark.parametrize(
    "reply",
    [
        "✅ **RFQ Created Successfully**\n- Product: Rice\nRFQ ID: `rfq_created_20240527_1623`",
        "🎉 Your RFQ has been published!",
        "The listing was created successfully.",
    ],
)
def test_creation_claims_are_detected(reply):
    assert claims_rfq_created(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        "All key commercial terms are recorded! Would you like to add any technical specifications, or proceed to create this RFQ?",
        "Once the RFQ is created, suppliers can respond.",
        "✅ **RFQ Closed:** 'Rice' has been closed.",
    ],
)
def test_ordinary_replies_are_not_creation_claims(reply):
    assert claims_rfq_created(reply) is False


def _sse_chunks(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def _mock_stream(content: str):
    async def lines():
        yield json.dumps({"message": {"role": "assistant", "content": content}, "done": True}).encode()

    resp = AsyncMock()
    resp.status_code = 200
    resp.aiter_lines = lines

    class Ctx:
        async def __aenter__(self):
            return resp

        async def __aexit__(self, *args):
            pass

    return patch.object(httpx.AsyncClient, "stream", lambda self, method, url, *a, **k: Ctx())


@pytest.mark.asyncio
async def test_stream_replaces_a_fake_rfq_created_reply(client: AsyncClient, make_actor, db: AsyncSession):
    actor = await make_actor("seller")
    fake = "✅ **RFQ Created Successfully**\nRFQ ID: `rfq_created_20240527_1623`"
    ai = {"product": "rice", "role": "seller", "intent": "rfq_drafting", "action_needed": "none"}
    # "looks good" is not a create request, so no create_rfq runs this turn.
    with _mock_ai(ai), _mock_stream(fake):
        res = await actor.post(
            "/ai-chat/stream",
            json={"messages": [{"role": "user", "content": "looks good"}], "current_rfq": SELLER_DRAFT},
        )
    assert res.status_code == 200
    chunks = _sse_chunks(res.text)
    replaced = [c for c in chunks if c.get("replace_content")]
    assert replaced and "has not been created yet" in replaced[-1]["content"]
    assert await db.scalar(select(func.count(RFQ.id))) == 0


@pytest.mark.asyncio
async def test_stream_recovers_from_a_false_refusal(client: AsyncClient, make_actor):
    actor = await make_actor("seller")
    refusal = "I only assist with business, procurement, and B2B trade inquiries."
    recovered = "Here is a courteous note for the buyer about our basmati rice."

    orig_post = httpx.AsyncClient.post

    async def mock_post(self, url, *args, **kwargs):
        if "api/chat" in str(url):
            resp = AsyncMock()
            resp.status_code = 200
            resp.json = lambda: {"message": {"role": "assistant", "content": recovered}, "done": True}
            return resp
        return await orig_post(self, url, *args, **kwargs)

    ai = {"product": "basmati rice", "role": "seller", "intent": "general", "action_needed": "none"}
    with _mock_ai(ai), _mock_stream(refusal), patch.object(httpx.AsyncClient, "post", mock_post):
        res = await actor.post(
            "/ai-chat/stream",
            json={"messages": [{"role": "user", "content": "what should i tell the buyer about rice quality"}]},
        )
    assert res.status_code == 200
    replaced = [c for c in _sse_chunks(res.text) if c.get("replace_content")]
    assert replaced and replaced[-1]["content"] == recovered


def test_refusal_detector_flags_the_logged_refusal():
    assert is_false_positive_refusal(
        "I only assist with business, procurement, and B2B trade inquiries.", MESSAGE_BUYER_MSG
    )


# =============================================================================
# "this buyer" resolves to the matching connection
# =============================================================================

def _conn(user_id, other_company, rfq_role, rfq_title, updated_day):
    other = SimpleNamespace(profile=SimpleNamespace(company_name=other_company, name=None))
    rfq = SimpleNamespace(role=rfq_role, user_id=uuid.uuid4(), title=rfq_title)
    return SimpleNamespace(
        sender_id=user_id,
        receiver=other,
        sender=None,
        rfq=rfq,
        updated_at=datetime(2026, 9, updated_day, tzinfo=timezone.utc),
    )


def test_this_buyer_prefers_the_rice_buyer_connection_over_a_newer_unrelated_one():
    me = uuid.uuid4()
    rice_buyer = _conn(me, "Apex Industrial Procurement", RFQRole.BUYER, "Need 25 metric tons of basmati rice", 27)
    newer_other = _conn(me, "Cable Traders", RFQRole.SELLER, "Supplying USB Type-C cable", 28)
    hint = {"side": "buyer", "product": "basmati rice"}
    best = max([newer_other, rice_buyer], key=lambda c: _connection_relevance(c, me, hint, ""))
    assert best is rice_buyer


# =============================================================================
# Stored message order
# =============================================================================

@pytest.mark.asyncio
async def test_reply_is_stored_after_the_question(client: AsyncClient, make_actor, db: AsyncSession):
    actor = await make_actor("seller")
    ai = {"product": "rice", "role": "seller", "intent": "general", "action_needed": "none"}
    with _mock_ai(ai), _mock_stream("Noted."):
        res = await actor.post("/ai-chat/stream", json={"messages": [{"role": "user", "content": "hello there"}]})
    assert res.status_code == 200
    rows = (await db.execute(select(Message.role, Message.created_at).order_by(Message.created_at))).all()
    assert [r.role.value for r in rows] == ["user", "assistant"]
    assert rows[0].created_at < rows[1].created_at


# =============================================================================
# The model's draft edits and counterparty letters stay grounded
# =============================================================================

def _update_call(**args):
    return {"function": {"name": "update_rfq_draft", "arguments": args}}


def test_model_cannot_flip_a_sellers_draft_to_buyer():
    from services.ai_chat_service import _process_tool_call

    draft = _process_tool_call(
        _update_call(role="buyer", product_name="Organic Basmati Rice"),
        dict(SELLER_DRAFT),
        locked_role="seller",
        user_text=SUPPY_DRAFT_MSG,
    )
    assert draft["role"] == "seller"


def test_model_cannot_invent_a_quantity():
    from services.ai_chat_service import _process_tool_call

    invented = _process_tool_call(
        _update_call(quantity_value=1, quantity_unit="tonne"), dict(SELLER_DRAFT), user_text=SUPPY_DRAFT_MSG
    )
    assert not invented.get("quantity")

    given = _process_tool_call(
        _update_call(quantity_value=200, quantity_unit="tonnes"), dict(SELLER_DRAFT), user_text=QUANTITY_MSG
    )
    assert given["quantity"]["value"] == 200


@pytest.mark.asyncio
async def test_this_buyer_is_resolved_into_the_prompt(db: AsyncSession, make_actor):
    from sqlalchemy import update
    from sqlalchemy.orm import selectinload

    from models.user import User, UserProfile
    from services import connection_service
    from services.ai_chat_service import _resolve_counterparty_context
    from tests.test_qa_negative_dynamic_cases import _seed_rice_market

    await _seed_rice_market(make_actor)
    me = await make_actor("seller")
    buyer_rfq = await db.scalar(select(RFQ).where(RFQ.role == RFQRole.BUYER))
    await db.execute(
        update(UserProfile).where(UserProfile.user_id == buyer_rfq.user_id).values(company_name="Apex Industrial Procurement")
    )
    await db.execute(update(UserProfile).where(UserProfile.user_id == me.id).values(company_name="Sterling Industrial Works"))
    await db.commit()
    user = (await db.execute(select(User).options(selectinload(User.profile)).where(User.id == me.id))).scalar_one()
    conn = await connection_service.create_connection(db, user.id, buyer_rfq.id)

    focus = {"product": "basmati rice", "category": "Agriculture", "role": "seller", "listing_role": "buyer"}
    context, conn_id = await _resolve_counterparty_context(db, user, MESSAGE_BUYER_MSG, focus)

    assert conn_id == str(conn.id)
    assert "Apex Industrial Procurement" in context
    assert "sign the message as): Sterling Industrial Works" in context
    assert "quantity 25 tonne" in context

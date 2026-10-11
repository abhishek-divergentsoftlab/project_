"""The AI chat must create exactly what the web form would, with the same rules."""

import json
from unittest.mock import patch

import pytest

from models.enums import RFQRole, RFQStatus
from services import ai_chat_service, chat_permissions, rfq_tool_service
from services.rfq_tool_service import RFQDraftInvalid, build_rfq_create_payload, evaluate_rfq_readiness

BASE = {
    "role": "buyer",
    "category": "Agriculture",
    "product_details": {"name": "  Basmati Rice "},
    "quantity": {"value": 500.0, "unit": "kg"},
}


def _draft(**over):
    d = json.loads(json.dumps(BASE))
    d.update(over)
    return d


# ---- A: no silent drops -------------------------------------------------
@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", {"value": 0, "unit": "kg"}),
        ("price_target", {"amount": 10, "currency": "ZZZ", "per_unit": "kg"}),
        ("deadline", {"in_days": 0}),
        ("deadline", {"date": "2001-01-01"}),
    ],
)
def test_invalid_values_raise_instead_of_vanishing(field, value):
    with pytest.raises(RFQDraftInvalid) as exc:
        build_rfq_create_payload(_draft(**{field: value}))
    assert field in [f for f, _ in exc.value.errors]
    assert all(m for _, m in exc.value.errors)


def test_missing_role_raises_not_buyer_fallback():
    d = _draft()
    d.pop("role")
    with pytest.raises(RFQDraftInvalid) as exc:
        build_rfq_create_payload(d)
    assert ("role", "buyer or seller is required") in exc.value.errors


@pytest.mark.asyncio
async def test_execute_create_reports_fields_and_creates_nothing():
    created, err = await ai_chat_service.execute_create_rfq_call(_draft(quantity={"value": 0, "unit": "kg"}), {})
    assert created is None
    assert err.startswith("I couldn't create the RFQ yet — please fix:")
    assert "quantity" in err


# ---- B / C: title and description ---------------------------------------
def test_title_is_product_name_as_written_stripped():
    p = build_rfq_create_payload(_draft())
    assert p.title == "Basmati Rice"
    assert p.description is None


def test_title_precedence_arg_then_draft_then_product_then_category():
    assert build_rfq_create_payload(_draft(title="Draft T"), title="Arg T").title == "Arg T"
    assert build_rfq_create_payload(_draft(title="Draft T")).title == "Draft T"
    d = _draft()
    d["product_details"] = {}
    assert build_rfq_create_payload(d).title == "Agriculture"


def test_description_is_notes_or_draft_only():
    assert build_rfq_create_payload(_draft(description="from draft")).description == "from draft"
    assert build_rfq_create_payload(_draft(description="from draft"), notes="n").description == "n"


def test_corrupted_product_name_is_still_salvaged():
    d = _draft()
    d["product_details"] = {"name": "crate rfq"}
    p = build_rfq_create_payload(d)
    assert "rfq" not in p.title.lower()
    assert p.product_details["name"] == p.title


# ---- D: MOQ seller only -------------------------------------------------
def test_minimum_order_dropped_for_buyer_kept_for_seller():
    moq = {"value": 50.0, "unit": "kg"}
    assert build_rfq_create_payload(_draft(minimum_order=moq)).minimum_order is None
    seller = build_rfq_create_payload(_draft(role="seller", minimum_order=moq))
    assert seller.role == RFQRole.SELLER and seller.minimum_order.value == 50.0


# ---- E: readiness = role + product name ---------------------------------
def test_readiness_needs_product_name_not_just_category():
    r = evaluate_rfq_readiness({"role": "buyer", "category": "Agriculture"})
    assert not r["required_complete"] and r["missing_required"] == ["product"]
    assert r["next_required_prompt"] == "product" and not r["has_product"]
    assert evaluate_rfq_readiness(BASE)["required_complete"]
    assert evaluate_rfq_readiness({"product_details": {"name": "rice"}})["next_required_prompt"] == "role"


@pytest.mark.asyncio
async def test_category_only_draft_cannot_be_created():
    created, err = await ai_chat_service.execute_create_rfq_call({"role": "buyer", "category": "Agriculture"}, {})
    assert created is None and "Product name" in err


# ---- F: save as draft ---------------------------------------------------
@pytest.mark.parametrize(
    "msg",
    ["save as draft", "save it as a draft", "keep it as draft", "don't publish yet", "draft me save karo",
     "abhi publish mat karo", "draft bana do", "Save draft"],
)
def test_wants_draft_status_positive(msg):
    assert chat_permissions.wants_draft_status(msg)


@pytest.mark.parametrize("msg", ["create the rfq", "publish it", "rfq bana do", "", "draft a message to the seller"])
def test_wants_draft_status_negative(msg):
    assert not chat_permissions.wants_draft_status(msg)


@pytest.mark.parametrize("msg", ["save as draft", "save it as a draft", "draft bana do", "draft me save karo"])
def test_save_as_draft_is_a_create_intent(msg):
    assert chat_permissions.check_create_rfq_intent(msg)


@pytest.mark.parametrize("msg", ["don't save it as a draft", "should I save as draft?", "how do I save a draft?"])
def test_save_as_draft_respects_negation_and_question_guards(msg):
    assert not chat_permissions.check_create_rfq_intent(msg)


@pytest.mark.asyncio
async def test_draft_status_reaches_payload_and_reply_says_draft():
    created, err = await ai_chat_service.execute_create_rfq_call(_draft(), {"status": "draft"})
    assert err is None and created["status"] == RFQStatus.DRAFT.value
    text = ai_chat_service.format_created_rfq_text(created)
    assert "draft" in text.lower() and "Published" not in text
    live, _ = await ai_chat_service.execute_create_rfq_call(_draft(), {})
    assert "Published" in ai_chat_service.format_created_rfq_text(live)


# ---- G: no hard-coded country ------------------------------------------
def test_draft_location_uses_gazetteer_and_never_invents_country():
    known = ai_chat_service._draft_location("Indore")
    assert known["city"] and known["country"] and known.get("state")
    unknown = ai_chat_service._draft_location("Zzyzxville")
    assert unknown == {"city": "Zzyzxville"}
    assert ai_chat_service._draft_location("Zzyzxville", "Kenya") == {"city": "Zzyzxville", "country": "Kenya"}


# ---- H / I: _run_chat_turn behaviour -----------------------------------


@pytest.mark.asyncio
async def test_server_draft_wins_over_client_draft_and_is_cleared_after_create(client, db, make_actor):
    from sqlalchemy import select

    from models.conversation import Conversation

    user = await make_actor("buyer")
    first = {"messages": [{"role": "user", "content": "hello"}], "current_rfq": BASE}
    with patch("services.answer_engine._chat", side_effect=AssertionError("model must not be called")):
        res = await user.post("/ai-chat/message", json={
            "messages": [{"role": "user", "content": "create the rfq"}], "current_rfq": BASE,
        })
    assert res.status_code == 200, res.text
    recap = res.json()
    assert recap["created_rfq"] is None and "Shall I publish it now" in recap["reply"]  # the recap comes first
    res = await user.post("/ai-chat/message", json={
        "messages": [{"role": "user", "content": "yes"}], "conversation_id": recap["conversation_id"],
    })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["created_rfq"] and data["created_rfq"]["title"] == "Basmati Rice"
    assert data["rfq_draft"] == {}
    conv = (await db.execute(select(Conversation).where(Conversation.user_id == user.id))).scalars().one()
    await db.refresh(conv)
    assert conv.state.get("rfq_draft") == {}

    # Second create in the same conversation: the stale client draft is ignored (server draft is empty).
    with patch("services.answer_engine._chat", side_effect=AssertionError("model must not be called")):
        res2 = await user.post("/ai-chat/message", json={
            "messages": [{"role": "user", "content": "create the rfq"}], "current_rfq": BASE,
            "conversation_id": data["conversation_id"],
        })
    assert res2.status_code == 200, res2.text
    assert not res2.json()["created_rfq"]
    assert "Role" in res2.json()["reply"] or "Product" in res2.json()["reply"]


@pytest.mark.asyncio
async def test_save_as_draft_message_creates_draft_status(client, make_actor):
    user = await make_actor("buyer")
    with patch("services.answer_engine._chat", side_effect=AssertionError("model must not be called")):
        res = await user.post("/ai-chat/message", json={
            "messages": [{"role": "user", "content": "save it as a draft"}], "current_rfq": BASE,
        })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["created_rfq"]["status"] == "draft"
    assert "draft" in data["reply"].lower()

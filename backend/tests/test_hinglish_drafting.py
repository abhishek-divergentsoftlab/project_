"""Hinglish drafting regression: "Me sabut moong kharidna chahta hu ?" must draft a buyer RFQ."""

import time
from unittest.mock import patch

import pytest
from sqlalchemy import select

from core.config import settings
from models.conversation import Message
from services import ai_query_parser, answer_engine, drafting_reply
from services.query_grounding import ground

MOONG = "Me sabut moong kharidna chahta hu ?"
FACTS_ONLY = "I don't have verified platform data"


# ---- grounding of evidence with ellipses ------------------------------------

def _role(evidence, message, role="buyer"):
    return ground({"role": role, "role_evidence": evidence, "intent": "buy_request"}, message).role


def test_ellipsis_evidence_is_grounded_in_order():
    assert _role("mujhe ... chahiye", "mujhe 50 quintal basmati chawal chahiye indore me") is not None
    assert _role("mujhe … chahiye", "mujhe 50 quintal basmati chawal chahiye indore me") is not None


def test_fragment_missing_or_out_of_order_fails():
    msg = "mujhe 50 quintal basmati chawal chahiye indore me"
    assert _role("mujhe ... bechna hai", msg) is None
    assert _role("chahiye ... mujhe", msg) is None


def test_devanagari_evidence_passes():
    msg = "मुझे 10 टन प्याज़ बेचना है"
    assert _role("बेचना है", msg, "seller") is not None


def test_plain_evidence_still_works():
    assert _role("kharidna chahta hu", MOONG) is not None


# ---- model choice -----------------------------------------------------------

def test_parser_model_defaults_to_chat_model(monkeypatch):
    monkeypatch.setattr(settings, "QUERY_AI_MODEL", "")
    assert ai_query_parser._model_name() == settings.AI_CHAT_MODEL
    monkeypatch.setattr(settings, "QUERY_AI_MODEL", "custom:1b")
    assert ai_query_parser._model_name() == "custom:1b"


# ---- deterministic drafting reply -------------------------------------------

def _ready(draft):
    from services import rfq_tool_service

    return rfq_tool_service.evaluate_rfq_readiness(draft)


def test_drafting_reply_asks_quantity_without_numbers():
    draft = {"role": "buyer", "product_details": {"name": "sabut moong"}}
    text = drafting_reply.build_drafting_reply(draft, _ready(draft), MOONG)
    assert "sabut moong" in text and "kharidna" in text
    assert "?" in text
    assert not any(ch.isdigit() for ch in text)


def test_drafting_reply_english_and_role_question():
    assert drafting_reply.build_drafting_reply({}, _ready({}), "hello").startswith("Are you looking")
    draft = {"role": "buyer", "product_details": {"name": "rice"}}
    text = drafting_reply.build_drafting_reply(draft, _ready(draft), "I want rice")
    assert text.startswith("Got it: you want to buy rice.")


# ---- end to end through /message ---------------------------------------------

def _call(**args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "update_rfq_draft", "arguments": args}}]}


def _say(text):
    return {"role": "assistant", "content": text}


@pytest.fixture
def moong_parser(monkeypatch):
    monkeypatch.setattr(settings, "QUERY_AI_ENABLED", True)
    monkeypatch.setattr(ai_query_parser, "_down_until", 0.0)
    ai_query_parser._cache.clear()

    async def fake(message, known):
        return {
            "role": "buyer", "role_evidence": "kharidna chahta hu", "intent": "buy_request",
            "product": "moong", "product_as_written": "sabut moong", "category": "Agriculture",
        }

    monkeypatch.setattr(ai_query_parser, "_call_model", fake)
    yield
    ai_query_parser._cache.clear()


def _script(monkeypatch, turns):
    seen = []

    async def chat(messages, tools):
        seen.append(messages)
        nxt = turns.pop(0)
        return nxt

    monkeypatch.setattr(answer_engine, "_chat", chat)
    return seen


async def _post(actor, text=MOONG):
    res = await actor.post("/ai-chat/message", json={"messages": [{"role": "user", "content": text}]})
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.asyncio
async def test_moong_sets_buyer_and_product_with_clean_model_reply(client, make_actor, monkeypatch, moong_parser):
    actor = await make_actor("buyer")
    _script(monkeypatch, [_say("Samajh gaya, aap sabut moong kharidna chahte hain. Kitni quantity chahiye?")])
    data = await _post(actor)
    assert data["rfq_draft"]["role"] == "buyer"
    assert "moong" in data["rfq_draft"]["product_details"]["name"].lower()
    assert FACTS_ONLY not in data["reply"]


@pytest.mark.asyncio
async def test_moong_failed_verification_twice_uses_drafting_fallback(
    client, make_actor, db, monkeypatch, moong_parser
):
    actor = await make_actor("buyer")
    bad = "Kitni quantity chahiye? For example 100 kg or 500 kg?"
    seen = _script(monkeypatch, [_call(role="buyer", product_name="sabut moong"), _say(bad), _say(bad)])
    data = await _post(actor)
    assert FACTS_ONLY not in data["reply"]
    assert "sabut moong" in data["reply"] and "?" in data["reply"]
    assert not any(ch.isdigit() for ch in data["reply"])
    assert data["rfq_draft"]["role"] == "buyer"
    assert "moong" in data["rfq_draft"]["product_details"]["name"].lower()
    assert any("remove example numbers" in str(m.get("content", "")).lower() for m in seen[-1])

    msg = (await db.execute(
        select(Message).where(Message.role == "assistant").order_by(Message.created_at.desc())
    )).scalars().first()
    v = msg.msg_metadata["verification"]
    assert v["used_fallback"] is True and v["retried"] is True and v["ok"] is False
    assert v["unverified"] and v["llm_error"] is False


@pytest.mark.asyncio
async def test_moong_without_tool_call_still_gets_drafting_fallback(client, make_actor, monkeypatch, moong_parser):
    actor = await make_actor("buyer")
    bad = "Sure! How about 250 kg?"
    _script(monkeypatch, [_say(bad), _say(bad)])
    data = await _post(actor)
    assert FACTS_ONLY not in data["reply"]
    assert data["rfq_draft"]["role"] == "buyer"
    assert "kharidna" in data["reply"]


# ---- product grounding through synonyms --------------------------------------

def _prod(product, message, as_written=None):
    return ground({"product": product, "product_as_written": as_written}, message).product


def test_local_word_grounds_english_product_without_as_written():
    assert _prod("turmeric", "hamare paas 200 bori haldi padi hai, bechni hai") == "turmeric"


def test_sabut_moong_to_moong_beans_is_kept():
    assert _prod("moong beans", MOONG) == "moong beans"


def test_invented_product_is_still_dropped():
    assert _prod("saffron", "hamare paas 200 bori haldi padi hai, bechni hai") is None

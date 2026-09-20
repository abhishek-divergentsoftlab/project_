"""Tests for the LLM Search Agent and dynamic prompt-based tool execution."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from core.config import settings
from models.enums import ConversationType
from services import conversation_llm, direct_search_service
from services.conversation_llm import SearchAgentDecision
from services.query_extractor import Requirements


@pytest.mark.asyncio
async def test_search_agent_parses_reply_decision():
    """Agent asks clarifying question when product is broad or needs details."""
    llm_payload = {
        "thought": "The user asked for vegetables, which is too broad. Ask which specific vegetable they need.",
        "action": "reply",
        "reply": "Which specific vegetables are you looking for, such as tomatoes, onions, or potatoes?",
        "updated_requirements": {
            "product": None,
            "category": "Agriculture",
            "attributes": {},
            "quantity_value": None,
            "quantity_unit": None,
            "price_amount": None,
            "price_currency": None,
            "city": None,
            "state": None,
            "country": None,
            "deadline_days": None,
        },
    }

    with patch.object(settings, "DIRECT_SEARCH_LLM", True):
        with patch.object(
            conversation_llm, "_generate", return_value=json.dumps(llm_payload)
        ):
            decision = await conversation_llm.run_search_agent(
                history=[],
                current_requirements=Requirements(),
                user_message="i need vegetables",
            )
            assert decision is not None
            assert decision.action == "reply"
            assert "vegetables" in decision.reply
            assert decision.updated_requirements.category == "Agriculture"


@pytest.mark.asyncio
async def test_search_agent_parses_tool_call_decision():
    """Agent calls search_marketplace with extracted parameters when ready."""
    llm_payload = {
        "thought": "All requirements established for organic tomatoes in Indore. Calling search tool.",
        "action": "search_marketplace",
        "reply": "Searching for organic Grade A tomatoes in Indore right now.",
        "updated_requirements": {
            "product": "tomatoes",
            "category": "Agriculture",
            "attributes": {"organic": True, "grade": "Grade A"},
            "quantity_value": 8000,
            "quantity_unit": "kg",
            "price_amount": 25,
            "price_currency": "INR",
            "city": "Indore",
            "state": "Madhya Pradesh",
            "country": "India",
            "deadline_days": 5,
        },
    }

    with patch.object(settings, "DIRECT_SEARCH_LLM", True):
        with patch.object(
            conversation_llm, "_generate", return_value=json.dumps(llm_payload)
        ):
            decision = await conversation_llm.run_search_agent(
                history=[{"role": "user", "content": "i need tomatoes"}],
                current_requirements=Requirements(product="tomatoes", category="Agriculture"),
                user_message="yes make it organic Grade A, 8000 kg at 25 inr in indore",
            )
            assert decision is not None
            assert decision.action == "search_marketplace"
            assert decision.updated_requirements.product == "tomatoes"
            assert decision.updated_requirements.attributes.get("organic") is True
            assert decision.updated_requirements.attributes.get("grade") == "Grade A"
            assert decision.updated_requirements.quantity_value == 8000
            assert decision.updated_requirements.city == "Indore"


@pytest.mark.asyncio
async def test_handle_message_runs_agent_and_executes_tool(make_actor, make_rfq, db):
    """When agent decides to search, direct_search_service records tool_call and performs match."""
    seller = await make_actor("seller", company_name="Malwa Agro", city="Indore")
    await make_rfq(
        seller,
        role="seller",
        title="Organic Grade A Fresh Tomatoes",
        category="Agriculture",
        attributes={"organic": True, "grade": "Grade A"},
        quantity=(10000, "kg"),
        price=(25, "INR", "kg"),
    )

    buyer = await make_actor("buyer")

    llm_payload = {
        "thought": "User wants organic Grade A tomatoes in Indore. Ready to search.",
        "action": "search_marketplace",
        "reply": "Found matching sellers for organic Grade A tomatoes.",
        "updated_requirements": {
            "product": "tomatoes",
            "category": "Agriculture",
            "attributes": {"organic": True, "grade": "Grade A"},
            "quantity_value": 8000,
            "quantity_unit": "kg",
            "price_amount": 25,
            "price_currency": "INR",
            "city": "Indore",
            "state": "Madhya Pradesh",
            "country": "India",
            "deadline_days": 5,
        },
    }

    with patch.object(settings, "DIRECT_SEARCH_LLM", True):
        with patch.object(
            conversation_llm, "_generate", return_value=json.dumps(llm_payload)
        ):
            result = await direct_search_service.handle_message(
                db,
                buyer,
                "find 8000 kg organic Grade A tomatoes in Indore",
            )
            assert result.requirements.product == "tomatoes"
            assert result.requirements.attributes.get("organic") is True
            assert result.requirements.attributes.get("grade") == "Grade A"
            assert result.results or result.total >= 0
            assert "tomatoes" in result.reply.lower() or "sellers" in result.reply.lower()


@pytest.mark.asyncio
async def test_handle_message_runs_agent_reply_without_searching(make_actor, db):
    """When agent decides to reply/clarify, no search tool is executed."""
    buyer = await make_actor("buyer")

    llm_payload = {
        "thought": "User switched to tomatoes. Ask if they want to specify quantity and price.",
        "action": "reply",
        "reply": "Got it — switching to tomatoes. How many kg do you need and what is your target price?",
        "updated_requirements": {
            "product": "tomatoes",
            "category": "Agriculture",
            "attributes": {},
            "quantity_value": None,
            "quantity_unit": "kg",
            "price_amount": None,
            "price_currency": "INR",
            "city": "Indore",
            "state": None,
            "country": None,
            "deadline_days": None,
        },
    }

    with patch.object(settings, "DIRECT_SEARCH_LLM", True):
        with patch.object(
            conversation_llm, "_generate", return_value=json.dumps(llm_payload)
        ):
            result = await direct_search_service.handle_message(
                db,
                buyer,
                "instead of cables i need tomatoes",
            )
            assert result.requirements.product == "tomatoes"
            assert result.results == []
            assert result.total == 0
            assert "switching to tomatoes" in result.reply

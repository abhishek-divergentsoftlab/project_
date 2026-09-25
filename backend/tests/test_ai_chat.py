"""Tests for AI Business Chat endpoint and service."""

from unittest.mock import AsyncMock, patch

import pytest
import httpx
from httpx import AsyncClient

from services import ai_chat_service


@pytest.mark.asyncio
async def test_ai_chat_requires_auth(client: AsyncClient):
    """Anonymous calls must be rejected with 401."""
    res = await client.post(
        "/ai-chat/message",
        json={"messages": [{"role": "user", "content": "How do I draft an RFQ?"}]},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_ai_chat_validation(client: AsyncClient, make_actor):
    """Empty messages or invalid payload should return 422."""
    actor = await make_actor("buyer")
    res = await actor.post("/ai-chat/message", json={"messages": []})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_ai_chat_message_success(client: AsyncClient, make_actor):
    """Authenticated user sends message and receives AI response."""
    actor = await make_actor("buyer")

    with patch.object(
        ai_chat_service,
        "generate_business_chat_reply",
        new=AsyncMock(return_value={
            "reply": "To draft a strong RFQ, clearly state specifications, quantity, delivery location, and inspection criteria.",
            "thinking": "Evaluated user question regarding RFQs.",
        }),
    ):
        res = await actor.post(
            "/ai-chat/message",
            json={
                "messages": [
                    {"role": "user", "content": "How should I structure an RFQ for corrugated cartons?"}
                ]
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert "reply" in data
        assert "corrugated" in data["reply"].lower() or "rfq" in data["reply"].lower()
        assert data["thinking"] == "Evaluated user question regarding RFQs."


@pytest.mark.asyncio
async def test_ai_chat_stream_success(client: AsyncClient, make_actor):
    """Authenticated user receives SSE streaming chunks."""
    actor = await make_actor("buyer")

    async def fake_stream(_messages, *args, **kwargs):
        yield {
            "content": "FOB means Free On Board.",
            "thinking": "analyzing",
            "rfq_draft": None,
            "readiness": None,
            "done": False,
        }
        yield {
            "content": " Risk transfers at ship rail.",
            "thinking": "",
            "rfq_draft": None,
            "readiness": None,
            "done": True,
        }

    with patch.object(ai_chat_service, "stream_business_chat_reply", side_effect=fake_stream):
        res = await actor.post(
            "/ai-chat/stream",
            json={
                "messages": [
                    {"role": "user", "content": "Explain FOB."}
                ]
            },
        )
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        body = res.text
        assert "Free On Board" in body
        assert "done" in body


def test_rfq_structure_pydantic_schema():
    """Verify RFQStructure has exact required/non-required fields and no title/notes."""
    from schemas.rfq_structure import RFQStructure

    schema = RFQStructure.model_json_schema()
    properties = schema.get("properties", {})

    # Must NOT have title or notes
    assert "title" not in properties
    assert "notes" not in properties

    # Required fields in schema
    assert "role" in schema["required"]
    assert "category" in schema["required"]

    # Optional fields
    assert "quantity" in properties
    assert "price_target" in properties
    assert "location" in properties
    assert "deadline" in properties
    assert "product_details" in properties
    assert "description" in properties

    # Valid instantiation with required fields only
    obj = RFQStructure(role="buyer", category="Agriculture")
    assert obj.role.value == "buyer"
    assert obj.category == "Agriculture"
    assert obj.quantity is None
    assert obj.location is None


def test_rfq_tool_single_and_multi_field_extraction():
    """Verify update_rfq_draft with **kwargs and multi-field compound preprocessing."""
    from services.rfq_tool_service import (
        DEFAULT_CONFIG,
        evaluate_rfq_readiness,
        preprocess_message_to_rfq,
        update_rfq_draft,
    )

    # 1. Direct tool invocation with **kwargs
    draft = update_rfq_draft(
        role="buyer",
        product_name="Apple",
        category="Agriculture",
        city="Indore",
        quantity_value=500,
        quantity_unit="kg",
        price_amount=60,
        price_currency="INR",
    )
    assert draft["role"] == "buyer"
    assert draft["product_details"]["name"] == "Apple"
    assert draft["category"] == "Agriculture"
    assert draft["location"]["city"] == "Indore"
    assert draft["quantity"]["value"] == 500.0
    assert draft["price_target"]["amount"] == 60.0

    # 2. Compound sentence extraction: 'I want to buy apple in indore'
    compound_msg = "I want to buy apple in indore"
    parsed_draft = preprocess_message_to_rfq(compound_msg)
    assert parsed_draft["role"] == "buyer"
    assert parsed_draft["product_details"]["name"].lower() == "apple"
    assert parsed_draft["category"] == "Agriculture"
    assert parsed_draft["location"]["city"] == "Indore"

    readiness = evaluate_rfq_readiness(parsed_draft)
    assert readiness["has_role"] is True
    assert readiness["has_product"] is True
    assert readiness["required_complete"] is True
    assert readiness["next_required_prompt"] is None


def test_rfq_collector_strict_sequencing():
    """Verify strict sequence: Role first, then Product, then optional fields."""
    from services.rfq_tool_service import evaluate_rfq_readiness, update_rfq_draft

    # Empty draft
    r0 = evaluate_rfq_readiness({})
    assert r0["required_complete"] is False
    assert r0["next_required_prompt"] == "role"

    # Role collected, product missing
    d1 = update_rfq_draft(role="seller")
    r1 = evaluate_rfq_readiness(d1)
    assert r1["has_role"] is True
    assert r1["has_product"] is False
    assert r1["required_complete"] is False
    assert r1["next_required_prompt"] == "product"

    # Role and product collected -> required complete!
    d2 = update_rfq_draft(existing_draft=d1, product_name="Corrugated Boxes", category="Packaging")
    r2 = evaluate_rfq_readiness(d2)
    assert r2["has_role"] is True
    assert r2["has_product"] is True
    assert r2["required_complete"] is True
    assert r2["next_required_prompt"] is None
    # Suggests optional fields
    assert "quantity" in r2["missing_optional"]
    assert "price_target" in r2["missing_optional"]


def test_rfq_summary_acknowledgment_multi_turn():
    """Verify exact 2-turn flow matches user expectations without repetition."""
    from services.ai_chat_service import format_rfq_summary_text
    from services.rfq_tool_service import evaluate_rfq_readiness, preprocess_message_to_rfq

    # Turn 1: User says '🍎 I want to buy apple in Indore'
    t1_msg = "🍎 I want to buy apple in Indore"
    d1 = preprocess_message_to_rfq(t1_msg)
    r1 = evaluate_rfq_readiness(d1)
    summary_1 = format_rfq_summary_text(d1, r1)

    assert d1["role"] == "buyer"
    assert d1["product_details"]["name"] == "apple"
    assert d1["category"] == "Agriculture"
    assert d1["location"]["city"] == "Indore"
    assert "Apple" in summary_1
    assert "Indore" in summary_1
    # Turn 1 must prompt for quantity / budget / deadline
    assert "quantity" in summary_1.lower()
    assert "budget" in summary_1.lower() or "price" in summary_1.lower()

    # Turn 2: User says 'i want to buy 1000kg at 10dollar par kg before 23 oct'
    t2_msg = "i want to buy 1000kg at 10dollar par kg before 23 oct"
    d2 = preprocess_message_to_rfq(t2_msg, current_draft=d1)
    r2 = evaluate_rfq_readiness(d2)
    summary_2 = format_rfq_summary_text(d2, r2)

    # Product must remain 'apple' and not be overwritten by 'par'
    assert d2["product_details"]["name"] == "apple"
    assert d2["quantity"]["value"] == 1000.0
    assert d2["quantity"]["unit"] == "kg"
    assert d2["price_target"]["amount"] == 10.0
    assert d2["price_target"]["currency"] == "USD"
    assert d2["location"]["city"] == "Indore"
    assert d2.get("deadline") is not None

    # Summary 2 must reflect all newly captured commercial details
    assert "1,000 kg" in summary_2 or "1000 kg" in summary_2
    assert "USD 10" in summary_2
    assert "Indore" in summary_2
    # Summary 2 must NOT ask for quantity or target budget again!
    assert "Would you like to specify **quantity" not in summary_2
    assert "All key commercial terms are recorded!" in summary_2


@pytest.mark.asyncio
async def test_ai_chat_stream_yields_tool_step_and_thinking_stages(client: AsyncClient, make_actor):
    """Verify stream yields thinking_1, tool_step [updating preferences], and thinking_2 stages."""
    actor = await make_actor("buyer")

    async def fake_step_stream(_messages, *args, **kwargs):
        # 1. First thinking phase
        yield {
            "content": "",
            "thinking": "Assessing user procurement request for apples.",
            "thinking_after": "",
            "tool_step": None,
            "rfq_draft": None,
            "readiness": None,
            "done": False,
        }
        # 2. Tool calling step
        yield {
            "content": "",
            "thinking": "",
            "thinking_after": "",
            "tool_step": {
                "name": "update_rfq_draft",
                "title": "Updating RFQ Preferences & Draft",
                "status": "completed",
                "args": {"product_name": "apples", "quantity_value": 1000},
            },
            "rfq_draft": {"product_details": {"name": "apples"}, "quantity": {"value": 1000.0}},
            "readiness": {"has_role": True, "has_product": True},
            "done": False,
        }
        # 3. Follow-up thinking phase
        yield {
            "content": "",
            "thinking": "",
            "thinking_after": "Draft updated with 1000kg apples. Formulating concise reply.",
            "tool_step": None,
            "rfq_draft": {"product_details": {"name": "apples"}, "quantity": {"value": 1000.0}},
            "readiness": {"has_role": True, "has_product": True},
            "done": False,
        }
        # 4. Actual output content
        yield {
            "content": "Recorded 1,000 kg apples. Ready to proceed.",
            "thinking": "",
            "thinking_after": "",
            "tool_step": None,
            "rfq_draft": {"product_details": {"name": "apples"}, "quantity": {"value": 1000.0}},
            "readiness": {"has_role": True, "has_product": True},
            "done": True,
        }

    with patch.object(ai_chat_service, "stream_business_chat_reply", side_effect=fake_step_stream):
        res = await actor.post(
            "/ai-chat/stream",
            json={
                "messages": [
                    {"role": "user", "content": "i want 1000kg apples"}
                ]
            },
        )
        assert res.status_code == 200
        body = res.text
        assert "Assessing user procurement request" in body
        assert "update_rfq_draft" in body
        assert "Updating RFQ Preferences & Draft" in body
        assert "Formulating concise reply" in body
        assert "Recorded 1,000 kg apples" in body


def test_create_rfq_tool_definition():
    """Verify CREATE_RFQ_TOOL schema structure."""
    from services.rfq_tool_service import CREATE_RFQ_TOOL

    assert CREATE_RFQ_TOOL["type"] == "function"
    fn = CREATE_RFQ_TOOL["function"]
    assert fn["name"] == "create_rfq"
    props = fn["parameters"]["properties"]
    assert "title" in props
    assert "status" in props
    assert "notes" in props


def test_build_rfq_create_payload():
    """Verify build_rfq_create_payload generates title and valid RFQCreate model."""
    from services.rfq_tool_service import build_rfq_create_payload
    from models.enums import RFQRole, RFQStatus

    draft = {
        "role": "buyer",
        "category": "Agriculture",
        "product_details": {"name": "apple"},
        "quantity": {"value": 1000.0, "unit": "kg"},
        "price_target": {"amount": 10.0, "currency": "USD", "per_unit": "kg"},
        "location": {"city": "Indore", "state": "Madhya Pradesh", "country": "India"},
        "deadline": {"in_days": 30},
    }

    payload = build_rfq_create_payload(draft, status="active")
    assert payload.role == RFQRole.BUYER
    assert payload.category == "Agriculture"
    assert "Procurement of" in payload.title
    assert "Apple" in payload.title
    assert "Indore" in payload.title
    assert payload.quantity.value == 1000.0
    assert payload.price_target.amount == 10.0
    assert payload.status == RFQStatus.ACTIVE


@pytest.mark.asyncio
async def test_ai_chat_create_rfq_execution(client: AsyncClient, make_actor):
    """Verify AI Chat calls create_rfq and returns created_rfq metadata."""
    actor = await make_actor("buyer")

    draft = {
        "role": "buyer",
        "category": "Agriculture",
        "product_details": {"name": "apple"},
        "quantity": {"value": 1000.0, "unit": "kg"},
    }

    res = await actor.post(
        "/ai-chat/message",
        json={
            "messages": [
                {"role": "user", "content": "Please proceed to create the RFQ now"}
            ],
            "current_rfq": draft,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["created_rfq"] is not None
    assert "id" in data["created_rfq"]
    assert data["created_rfq"]["role"] == "buyer"
    assert "🎉" in data["reply"]
    assert "Successfully Created" in data["reply"]


@pytest.mark.asyncio
async def test_ai_chat_create_this_rfq_exact_phrase(client: AsyncClient, make_actor):
    """Verify 'create this RFQ' preserves product and creates RFQ immediately."""
    actor = await make_actor("buyer")

    draft = {
        "role": "buyer",
        "category": "Agriculture",
        "product_details": {"name": "apple"},
        "quantity": {"value": 1000.0, "unit": "kg"},
        "price_target": {"amount": 10.0, "currency": "USD", "per_unit": "kg"},
        "location": {"city": "Indore"},
    }

    res = await actor.post(
        "/ai-chat/message",
        json={
            "messages": [
                {"role": "user", "content": "I want to buy apple in Indore"},
                {"role": "assistant", "content": "Recorded request for apple in Indore."},
                {"role": "user", "content": "create this RFQ"},
            ],
            "current_rfq": draft,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["created_rfq"] is not None
    assert "apple" in data["created_rfq"]["title"].lower()
    assert data["created_rfq"]["role"] == "buyer"
    assert "🎉" in data["reply"]


@pytest.mark.asyncio
async def test_ai_chat_conversations_unauthorized(client: AsyncClient):
    """Anonymous access to conversation history endpoints must return 401."""
    import uuid
    dummy_id = str(uuid.uuid4())
    res = await client.get("/ai-chat/conversations")
    assert res.status_code == 401
    res = await client.get(f"/ai-chat/conversations/{dummy_id}")
    assert res.status_code == 401
    res = await client.delete(f"/ai-chat/conversations/{dummy_id}")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_ai_chat_conversation_persistence_and_crud(client: AsyncClient, make_actor):
    """Sending a message creates conversation and messages, which can be listed and retrieved."""
    actor = await make_actor("buyer")

    # Initially empty
    list_res = await actor.get("/ai-chat/conversations")
    assert list_res.status_code == 200
    initial_count = len(list_res.json())

    # Send message with mocked Ollama
    import httpx
    orig_post = httpx.AsyncClient.post

    async def mock_ollama_post(self, url, *args, **kwargs):
        if "api/chat" in str(url):
            m = AsyncMock()
            m.status_code = 200
            m.json = lambda: {
                "message": {
                    "role": "assistant",
                    "content": "For 500 corrugated boxes, target price should include 3-ply kraft.",
                    "thinking": "Evaluated packaging request",
                }
            }
            return m
        return await orig_post(self, url, *args, **kwargs)

    with patch.object(httpx.AsyncClient, "post", mock_ollama_post):
        send_res = await actor.post(
            "/ai-chat/message",
            json={
                "messages": [
                    {"role": "user", "content": "I need 500 corrugated boxes in Delhi"}
                ]
            },
        )
        assert send_res.status_code == 200
        send_data = send_res.json()
        assert send_data["conversation_id"] is not None
        conv_id = send_data["conversation_id"]

    # List conversations now includes the new conversation
    list_res = await actor.get("/ai-chat/conversations")
    assert list_res.status_code == 200
    convs = list_res.json()
    assert len(convs) == initial_count + 1
    found_conv = next((c for c in convs if c["id"] == conv_id), None)
    assert found_conv is not None
    assert "corrugated boxes" in found_conv["title"].lower()

    # Get conversation details
    detail_res = await actor.get(f"/ai-chat/conversations/{conv_id}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["id"] == conv_id
    assert len(detail["messages"]) >= 2
    user_msg = next((m for m in detail["messages"] if m["role"] == "user"), None)
    asst_msg = next((m for m in detail["messages"] if m["role"] == "assistant"), None)
    assert user_msg is not None
    assert "500 corrugated boxes" in user_msg["content"]
    assert asst_msg is not None
    assert "3-ply kraft" in asst_msg["content"]
    assert asst_msg["thinking"] == "Evaluated packaging request"

    # Delete conversation
    del_res = await actor.delete(f"/ai-chat/conversations/{conv_id}")
    assert del_res.status_code == 204

    # Verify 404 after deletion
    get_again = await actor.get(f"/ai-chat/conversations/{conv_id}")
    assert get_again.status_code == 404


@pytest.mark.asyncio
async def test_ai_chat_conversation_tenant_isolation(client: AsyncClient, make_actor):
    """User B cannot access or delete User A's conversation."""
    actor_a = await make_actor("buyer")
    actor_b = await make_actor("seller")

    import httpx
    orig_post = httpx.AsyncClient.post

    async def mock_private_post(self, url, *args, **kwargs):
        if "api/chat" in str(url):
            m = AsyncMock()
            m.status_code = 200
            m.json = lambda: {
                "message": {
                    "role": "assistant",
                    "content": "Secret procurement specs.",
                }
            }
            return m
        return await orig_post(self, url, *args, **kwargs)

    with patch.object(httpx.AsyncClient, "post", mock_private_post):
        send_res = await actor_a.post(
            "/ai-chat/message",
            json={"messages": [{"role": "user", "content": "Private trade query"}]},
        )
        assert send_res.status_code == 200
        conv_id = send_res.json()["conversation_id"]

    # Actor B attempts to fetch Actor A's conversation
    unauth_get = await actor_b.get(f"/ai-chat/conversations/{conv_id}")
    assert unauth_get.status_code == 404

    # Actor B attempts to delete Actor A's conversation
    unauth_del = await actor_b.delete(f"/ai-chat/conversations/{conv_id}")
    assert unauth_del.status_code == 404

    # Actor A can still fetch it
    auth_get = await actor_a.get(f"/ai-chat/conversations/{conv_id}")
    assert auth_get.status_code == 200


@pytest.mark.asyncio
async def test_ai_chat_stream_persistence(client: AsyncClient, make_actor):
    """Streaming chat saves user and assistant messages to DB with conversation_id emitted."""
    actor = await make_actor("buyer")

    async def fake_stream_lines():
        lines = [
            '{"message": {"role": "assistant", "content": "Payment terms "}, "done": false}',
            '{"message": {"role": "assistant", "content": "can be LC 60 days."}, "done": true}',
        ]
        for l in lines:
            yield l.encode("utf-8")

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = fake_stream_lines

    class MockStreamContext:
        async def __aenter__(self):
            return mock_resp
        async def __aexit__(self, *args):
            pass

    with patch("httpx.AsyncClient.stream", return_value=MockStreamContext()):
        res = await actor.post(
            "/ai-chat/stream",
            json={
                "messages": [
                    {"role": "user", "content": "What are common payment terms?"}
                ]
            },
        )
        assert res.status_code == 200
        body = res.text
        assert "Payment terms" in body
        assert "conversation_id" in body

    # Verify conversation was persisted
    list_res = await actor.get("/ai-chat/conversations")
    assert list_res.status_code == 200
    convs = list_res.json()
    assert len(convs) >= 1
    recent_conv = convs[0]
    assert "payment terms" in recent_conv["title"].lower()

    # Verify messages inside conversation
    detail_res = await actor.get(f"/ai-chat/conversations/{recent_conv['id']}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert len(detail["messages"]) == 2
    assert detail["messages"][0]["role"] == "user"
    assert "payment terms" in detail["messages"][0]["content"].lower()
    assert detail["messages"][1]["role"] == "assistant"
    assert "LC 60 days" in detail["messages"][1]["content"]




@pytest.mark.asyncio
async def test_match_query_intent_detection():
    """Verify match query intent detector correctly identifies counterparty inquiry phrases."""
    from services.rfq_tool_service import check_match_query_intent, preprocess_message_to_rfq

    # Positive matches
    assert check_match_query_intent("give me mail id of top three") is True
    assert check_match_query_intent("which one suits best from this top three") is True
    assert check_match_query_intent("compare top 3 suppliers") is True
    assert check_match_query_intent("what are their email ids?") is True
    assert check_match_query_intent("who is the cheapest seller?") is True
    assert check_match_query_intent("show matching suppliers") is True

    # Negative matches (regular RFQ drafting messages)
    assert check_match_query_intent("I want to buy apples in Indore") is False
    assert check_match_query_intent("5000 kg at 45 rs per kg") is False
    assert check_match_query_intent("create the rfq") is False

    # Verify preprocessing does not corrupt an existing draft with match questions
    initial_draft = {
        "role": "buyer",
        "product_details": {"name": "Fresh Apples"},
        "quantity": {"value": 5000, "unit": "kg"},
    }
    updated_draft = preprocess_message_to_rfq("give me mail id of top three", current_draft=initial_draft)
    assert updated_draft["product_details"]["name"] == "Fresh Apples"
    assert updated_draft["quantity"]["value"] == 5000


@pytest.mark.asyncio
async def test_build_rfq_system_prompt_with_matched_candidates():
    """Verify system prompt includes matched candidates details and direct inquiry instructions."""
    from services.ai_chat_service import build_rfq_system_prompt

    draft = {"role": "buyer", "product_details": {"name": "Corrugated Boxes"}}
    readiness = {"has_role": True, "has_product": True}
    candidates = [
        {
            "rank": 1,
            "title": "A-Grade Corrugated Boxes 5-Ply",
            "score": {"total": 0.94},
            "price": {"amount": 42.0, "currency": "INR", "per_unit": "piece"},
            "quantity": {"value": 10000, "unit": "pcs"},
            "location": {"city": "Indore", "state": "MP", "country": "India"},
            "distance_km": 15.4,
            "counterparty": {
                "company_name": "Apex Packtech Ltd",
                "email": "contact@apexpack.com",
                "phone": "+91 9876543210",
                "average_rating": 4.8,
                "total_reviews": 12,
                "gst_verified": True,
            },
        },
        {
            "rank": 2,
            "title": "Industrial Cartons Kraft Paper",
            "score": {"total": 0.88},
            "price": {"amount": 45.0, "currency": "INR", "per_unit": "piece"},
            "quantity": {"value": 8000, "unit": "pcs"},
            "location": {"city": "Bhopal", "state": "MP", "country": "India"},
            "distance_km": 185.0,
            "counterparty": {
                "company_name": "Central Paper Mills",
                "email": "sales@centralpaper.in",
                "average_rating": 4.5,
                "total_reviews": 7,
                "gst_verified": False,
            },
        },
    ]

    prompt = build_rfq_system_prompt(draft, readiness, matched_candidates=candidates)
    assert "CURRENT LOADED MATCHED COUNTERPARTIES" in prompt
    assert "Apex Packtech Ltd" in prompt
    assert "contact@apexpack.com" in prompt
    assert "Central Paper Mills" in prompt
    assert "sales@centralpaper.in" in prompt
    assert "give me mail id of top three" in prompt
    assert "which one suits best from this top three" in prompt


@pytest.mark.asyncio
async def test_ai_chat_message_with_matched_candidates_persisted(client: AsyncClient, make_actor):
    """Verify POST /ai-chat/message accepts matched_candidates and persists to state."""
    actor = await make_actor("buyer")
    candidates = [
        {
            "rank": 1,
            "title": "Apples",
            "score": {"total": 0.92},
            "counterparty": {
                "company_name": "Himalaya Orchards",
                "email": "info@himalayaorchards.com",
                "gst_verified": True,
            },
        }
    ]

    import httpx
    orig_post = httpx.AsyncClient.post

    mock_reply = {
        "message": {
            "role": "assistant",
            "content": "The best suited supplier is Himalaya Orchards (Rank #1, 92% match) with verified GST credentials.",
        },
        "done": True,
    }

    async def mock_post(self, url, *args, **kwargs):
        if "api/chat" in str(url):
            resp = AsyncMock()
            resp.status_code = 200
            resp.json = lambda: mock_reply
            return resp
        return await orig_post(self, url, *args, **kwargs)

    with patch.object(httpx.AsyncClient, "post", mock_post):
        res = await actor.post(
            "/ai-chat/message",
            json={
                "messages": [
                    {"role": "user", "content": "which one suits best from this top three"}
                ],
                "matched_candidates": candidates,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert "Himalaya Orchards" in data["reply"]
        assert data.get("conversation_id") is not None

        # Verify matched_candidates stored in conversation state
        conv_res = await actor.get(f"/ai-chat/conversations/{data['conversation_id']}")
        assert conv_res.status_code == 200
        conv_data = conv_res.json()
        assert "matched_candidates" in conv_data["state"]
        saved_candidates = conv_data["state"]["matched_candidates"]
        assert len(saved_candidates) == 1
        assert saved_candidates[0]["counterparty"]["company_name"] == "Himalaya Orchards"


async def test_ai_chat_payload_includes_num_ctx(client: AsyncClient, make_actor):
    """Ensure num_ctx is configured and sent in Ollama options for both chat and stream."""
    from core.config import settings
    actor = await make_actor("buyer")
    captured_payloads = []

    import httpx
    orig_post = httpx.AsyncClient.post

    async def mock_post(self, url, *args, **kwargs):
        if "api/chat" in str(url):
            payload = kwargs.get("json") or {}
            captured_payloads.append(payload)
            resp = AsyncMock()
            resp.status_code = 200
            resp.json = lambda: {
                "message": {"role": "assistant", "content": "Acknowledged."},
                "done": True,
            }
            return resp
        return await orig_post(self, url, *args, **kwargs)

    with patch.object(httpx.AsyncClient, "post", mock_post):
        res = await actor.post(
            "/ai-chat/message",
            json={"messages": [{"role": "user", "content": "Testing context size"}]},
        )
        assert res.status_code == 200
        assert len(captured_payloads) == 1
        options = captured_payloads[0].get("options", {})
        assert options.get("num_ctx") == 65536
        assert options.get("num_ctx") == settings.AI_CHAT_NUM_CTX

    # Test stream payload options
    captured_stream_payloads = []

    async def fake_stream_lines():
        yield b'{"message": {"role": "assistant", "content": "Stream reply"}, "done": true}'

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = fake_stream_lines

    class MockStreamContext:
        async def __aenter__(self):
            return mock_resp
        async def __aexit__(self, *args):
            pass

    def mock_stream(self, method, url, *args, **kwargs):
        if "api/chat" in str(url):
            captured_stream_payloads.append(kwargs.get("json") or {})
        return MockStreamContext()

    with patch.object(httpx.AsyncClient, "stream", mock_stream):
        s_res = await actor.post(
            "/ai-chat/stream",
            json={"messages": [{"role": "user", "content": "Testing stream context size"}]},
        )
        assert s_res.status_code == 200
        assert len(captured_stream_payloads) == 1
        stream_opts = captured_stream_payloads[0].get("options", {})
        assert stream_opts.get("num_ctx") == 65536
        assert stream_opts.get("num_ctx") == settings.AI_CHAT_NUM_CTX


def test_crate_rfq_typo_preserves_product_name():
    """Ensure typos like 'yes please crate rfq' are treated as creation intent, not product name."""
    from services import rfq_tool_service

    draft = {
        "role": "buyer",
        "category": "Agriculture",
        "product_details": {"name": "Fuji Apples", "color": "green", "organic": True},
    }
    msg = "yes please crate rfq"

    # Intent check
    assert rfq_tool_service.check_create_rfq_intent(msg) is True

    # Preprocessing does not overwrite established product
    new_draft = rfq_tool_service.preprocess_message_to_rfq(msg, draft)
    assert new_draft["product_details"]["name"] == "Fuji Apples"

    # Even on an empty draft, 'crate rfq' is rejected as a product name
    empty_draft = {}
    extracted = rfq_tool_service.preprocess_message_to_rfq("crate rfq", empty_draft)
    assert extracted.get("product_details", {}).get("name") is None

    # Safety net in build_rfq_create_payload salvages product from title if draft had corrupted name
    corrupted_draft = {
        "role": "buyer",
        "category": "Agriculture",
        "product_details": {"name": "crate rfq", "color": "green", "organic": True},
    }
    payload = rfq_tool_service.build_rfq_create_payload(
        corrupted_draft,
        title="RFQ: 1,299 kg Organic Green Fuji Apples – Indore (23-day delivery)",
    )
    assert payload.product_details["name"] == "Organic Green Fuji Apples"


def test_counterparty_message_intent_detection():
    """Verify that negotiation and counterparty messaging intents are accurately detected and don't mutate RFQs."""
    from services import rfq_tool_service

    negotiation_msg = (
        "send message to the seller and the behaviour should be like politely and "
        "ask can you reduce the price from 1 dollar to 0.9"
    )
    assert rfq_tool_service.check_counterparty_message_intent(negotiation_msg) is True
    assert rfq_tool_service.check_create_rfq_intent(negotiation_msg) is False

    # Variations
    assert rfq_tool_service.check_counterparty_message_intent("ask the seller can we do 5% off?") is True
    assert rfq_tool_service.check_counterparty_message_intent("tell the supplier we accept the offer") is True
    assert rfq_tool_service.check_counterparty_message_intent("negotiate with the vendor politely") is True
    assert rfq_tool_service.check_counterparty_message_intent("message the buyer with updated quote") is True

    # Negative cases
    assert rfq_tool_service.check_counterparty_message_intent("i want to buy 1000kg apples") is False
    assert rfq_tool_service.check_counterparty_message_intent("find me the best seller in Mumbai") is False

    # Does not corrupt existing draft
    draft = {
        "role": "buyer",
        "category": "Agriculture",
        "product_details": {"name": "Fuji Apples"},
        "price_target": {"amount": 1.0, "currency": "USD"},
    }
    updated_draft = rfq_tool_service.preprocess_message_to_rfq(negotiation_msg, draft)
    assert updated_draft["product_details"]["name"] == "Fuji Apples"
    assert updated_draft["price_target"]["amount"] == 1.0


@pytest.mark.asyncio
async def test_execute_send_counterparty_message_simulation():
    """Verify execute_draft_counterparty_message_call formats and simulates message drafting when without db."""
    from services.ai_chat_service import execute_draft_counterparty_message_call, format_counterparty_message_text

    tool_args = {
        "recipient_name": "Agro Fresh Ltd",
        "message": "Dear Agro Fresh team, could you please consider reducing the unit price from $1.00 to $0.90 for this 1,299 kg order? We would appreciate your best offer.",
        "negotiation_objective": "Price reduction to $0.90",
    }
    result, err = await execute_draft_counterparty_message_call(tool_args)
    assert err is None
    assert result is not None
    assert result["status"] == "draft"
    assert result["counterparty_name"] == "Agro Fresh Ltd"
    assert "$0.90" in result["message"]

    formatted = format_counterparty_message_text(result)
    assert "Agro Fresh Ltd" in formatted
    assert "$0.90" in formatted
    assert "Drafted for Agro Fresh Ltd" in formatted


@pytest.mark.asyncio
async def test_ai_chat_message_endpoint_handles_send_counterparty_message(client: AsyncClient, make_actor, make_rfq):
    """Verify /ai-chat/message handles draft_counterparty_message tool call and returns draft without directly sending."""
    buyer = await make_actor("buyer", name="Anil Buyer", company_name="Anil Traders")
    seller = await make_actor("seller", name="Rekha Seller", company_name="Premium Agro Supplies")
    listing = await make_rfq(seller, role="seller", title="Supplying 5000 kg Fresh Fuji Apples")

    # Connect buyer with seller listing
    conn_res = await buyer.post("/connections", json={"rfq_id": listing["id"]})
    assert conn_res.status_code == 201
    conn_data = conn_res.json()
    conn_id = conn_data["id"]

    orig_post = httpx.AsyncClient.post

    async def mock_ollama_post(self, url, *args, **kwargs):
        if "api/chat" in str(url):
            m = AsyncMock()
            m.status_code = 200
            m.json = lambda: {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "thinking": "Negotiating polite price reduction with the seller.",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "draft_counterparty_message",
                                "arguments": {
                                    "recipient_name": "Premium Agro Supplies",
                                    "message": "Dear team, would it be possible to adjust the unit price from $1.00 to $0.90 for this order? Thank you.",
                                    "negotiation_objective": "Price reduction",
                                    "connection_id": conn_id,
                                },
                            }
                        }
                    ],
                }
            }
            return m
        return await orig_post(self, url, *args, **kwargs)

    with patch.object(httpx.AsyncClient, "post", mock_ollama_post):
        res = await buyer.post(
            "/ai-chat/message",
            json={
                "active_connection_id": conn_id,
                "messages": [
                    {
                        "role": "user",
                        "content": "send natotiation message to the counter user and ask can you reduce the price from 1 dollar to 0.9",
                    }
                ]
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["counterparty_message"] is not None
        cp_msg = data["counterparty_message"]
        assert cp_msg["counterparty_name"] == "Premium Agro Supplies"
        assert "$0.90" in cp_msg["message"]
        assert cp_msg["status"] == "draft"
        assert "Negotiation Message Drafted for Premium Agro Supplies" in data["reply"]
        assert data["conversation_id"] is not None

        # Verify that the message was NOT directly sent to the connection messages table!
        conn_msgs_res = await buyer.get(f"/connections/{conn_id}/messages")
        assert conn_msgs_res.status_code == 200
        assert len(conn_msgs_res.json()) == 0, "AI must NOT directly send message; it should remain a draft"

        # Check conversation history contains counterparty_message metadata with draft status
        conv_id = data["conversation_id"]
        detail_res = await buyer.get(f"/ai-chat/conversations/{conv_id}")
        assert detail_res.status_code == 200
        detail = detail_res.json()
        last_msg = detail["messages"][-1]
        assert last_msg["role"] == "assistant"
        assert last_msg.get("counterparty_message") is not None
        assert last_msg["counterparty_message"]["counterparty_name"] == "Premium Agro Supplies"
        assert last_msg["counterparty_message"]["status"] == "draft"

        # Now simulate user clicking "Send" button on the draft card:
        # 1. User sends message via connection service
        send_res = await buyer.post(
            f"/connections/{conn_id}/messages",
            json={"content": cp_msg["message"]},
        )
        assert send_res.status_code == 201
        sent_data = send_res.json()

        # 2. Mark draft as sent in AI chat conversation
        mark_res = await buyer.post(
            f"/ai-chat/conversations/{conv_id}/messages/{last_msg['id']}/mark-sent",
            json={"sent_message_id": sent_data["id"]},
        )
        assert mark_res.status_code == 200

        # Verify conversation message now shows delivered
        detail_res2 = await buyer.get(f"/ai-chat/conversations/{conv_id}")
        detail2 = detail_res2.json()
        assert detail2["messages"][-1]["counterparty_message"]["status"] == "delivered"


@pytest.mark.asyncio
async def test_counterparty_message_typos_amendments_and_guarding():
    """Verify counterparty intent detects typos, amendments, and protects RFQ draft from corruption."""
    from services import rfq_tool_service

    t1 = "send nagitiation message to that user that can we deal it in 180 inr per kg"
    t2 = "aslo mantion that the location is not indore its 10kg away from indore"
    t3 = "also mention that the location is not indore"
    t4 = "tell them we need delivery within 20 days"
    t5 = "and add that we need organic certificate"

    assert rfq_tool_service.check_counterparty_message_intent(t1) is True
    assert rfq_tool_service.check_counterparty_message_intent(t2) is True
    assert rfq_tool_service.check_counterparty_message_intent(t3) is True
    assert rfq_tool_service.check_counterparty_message_intent(t4) is True
    assert rfq_tool_service.check_counterparty_message_intent(t5) is True

    # Preprocessing does not corrupt product or extract conversational directives
    initial_draft = {
        "role": "buyer",
        "category": "Agriculture",
        "product_details": {"name": "Fuji Apples"},
        "location": {"city": "Indore", "state": "Madhya Pradesh", "country": "India"},
    }
    d1 = rfq_tool_service.preprocess_message_to_rfq(t1, current_draft=initial_draft)
    assert d1["product_details"]["name"] == "Fuji Apples"

    d2 = rfq_tool_service.preprocess_message_to_rfq(t2, current_draft=initial_draft)
    assert d2["product_details"]["name"] == "Fuji Apples"
    assert "aslo" not in d2["product_details"]["name"].lower()
    assert "mantion" not in d2["product_details"]["name"].lower()


@pytest.mark.asyncio
async def test_counterparty_followup_deterministic_fallback(client: AsyncClient, make_actor, make_rfq):
    """Verify follow-up amendment dispatches message even when model outputs text without tool_calls."""
    buyer = await make_actor("buyer", name="Rajesh Buyer", company_name="Rajesh Fruits")
    seller = await make_actor("seller", name="Aurora Seller", company_name="Aurora Enterprises")
    listing = await make_rfq(seller, role="seller", title="Supplying 10000 kg Premium Fuji Apples")

    # Connect buyer with seller listing
    conn_res = await buyer.post("/connections", json={"rfq_id": listing["id"]})
    assert conn_res.status_code == 201
    conn_id = conn_res.json()["id"]

    orig_post = httpx.AsyncClient.post

    # Turn 2 model output: plain text with confirmation but NO tool_calls emitted by model
    model_text_reply = (
        "✉️ Updated Message Sent to Aurora Enterprises\n\n"
        "*\"Dear Aurora Enterprises,\n"
        "Thank you for your quotation of 186 INR/kg for Fuji apples (Extra Class). "
        "We are interested in procuring 1,299 kg. Please note that the location is 10 km away from Indore.\n\n"
        "Could you kindly consider reducing the price to 180 INR per kg? We hope to finalize a mutually beneficial deal.\"*\n\n"
        "The message has been dispatched — monitor replies in the seller chat pane."
    )

    async def mock_ollama_post(self, url, *args, **kwargs):
        if "api/chat" in str(url):
            m = AsyncMock()
            m.status_code = 200
            m.json = lambda: {
                "message": {
                    "role": "assistant",
                    "content": model_text_reply,
                    "thinking": "Amending negotiation details with counterparty location note.",
                    "tool_calls": [],  # Model forgot to emit tool_call
                }
            }
            return m
        return await orig_post(self, url, *args, **kwargs)

    with patch.object(httpx.AsyncClient, "post", mock_ollama_post):
        res = await buyer.post(
            "/ai-chat/message",
            json={
                "active_connection_id": conn_id,
                "current_rfq": {
                    "role": "buyer",
                    "product_details": {"name": "Fuji apples"},
                    "location": {"city": "Indore"},
                },
                "messages": [
                    {
                        "role": "user",
                        "content": "aslo mantion that the location is not indore its 10kg away from indore",
                    }
                ],
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["counterparty_message"] is not None
        cp_msg = data["counterparty_message"]
        assert cp_msg["counterparty_name"] == "Aurora Enterprises"
        assert "10 km away from Indore" in cp_msg["message"]
        assert cp_msg["status"] == "draft"
        # Product draft is not corrupted to "aslo mantion location"
        assert data["rfq_draft"]["product_details"]["name"] == "Fuji apples"


def test_is_truncated_message_detection():
    """Verify is_truncated_message identifies truncated messages and accepts complete messages."""
    from services.ai_chat_service import is_truncated_message

    truncated_1 = "Dear Aurora Enterprises, Thank you for your quotation of 186 INR/kg for Fuji apples (Extra Class). We are interested in procuring 1,299 kg for delivery within"
    truncated_2 = "Dear Supplier, We would like to purchase for"
    complete_1 = "Dear Aurora Enterprises, Thank you for your quotation of 186 INR/kg. Could you please reduce the price to 180 INR/kg? We value your cooperation."
    complete_2 = "Dear team, we accept the quotation."

    assert is_truncated_message(truncated_1) is True
    assert is_truncated_message(truncated_2) is True
    assert is_truncated_message(complete_1) is False
    assert is_truncated_message(complete_2) is False






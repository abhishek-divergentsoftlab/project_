"""Comprehensive QA Engineer Negative & Dynamic Test Suite.

Adversarial testing covering:
1. Ground truth marketplace grounding for non-existent/unlisted commodities (zero false leaks).
2. Natural conversational doubt and multi-clause query handling (zero entity pollution).
3. Compound seller/buyer role discrimination across tricky and Hinglish phrasings.
4. Tool compulsion defense: ensuring tools are stripped on analytical turns and enabled on actions.
5. Catalog multi-term search with single-character specifiers ("Type C", "5-ply").
6. Malformed/adversarial inputs (special characters, numbers only, casing, huge strings).
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import RFQRole, RFQStatus
from models.rfq import RFQ
from services.agent_router import route_query_to_agent, route_query_to_agent_async
from services.ai_chat_service import (
    _fetch_live_marketplace_aggregates,
    format_marketplace_aggregates_prompt,
)
from services.catalog_service import get_catalog
from services.query_extractor import extract
from tests.conftest import rfq_body


# =============================================================================
# 1. Ground Truth Live Database Grounding (Zero Hallucination Test)
# =============================================================================

@pytest.mark.asyncio
async def test_qa_unlisted_product_reports_zero_listings(db: AsyncSession, make_actor):
    """Negative Test: Searching for an unlisted commodity (e.g. 'banana') must report 0 listings.

    CRITICAL QA INVARIANT:
    Must NOT fall back to stopword leaks or partial matching that pulls in 38 unrelated listings
    (tiles, chairs, basmati rice) and hallucinates prices up to ₹152,433.
    """
    seller = await make_actor("seller")
    # Seed known non-banana listings
    await seller.post(
        "/rfqs",
        json=rfq_body(
            role="seller",
            category="Agriculture",
            title="Premium 1121 Basmati Rice",
            status="active",
        ),
    )
    await seller.post(
        "/rfqs",
        json=rfq_body(
            role="seller",
            category="Packaging",
            title="5-Ply Heavy Corrugated Boxes",
            status="active",
        ),
    )

    # User asks about bananas in Indore
    query = (
        "i want to supply banana from indore but i am confused what price i should decide "
        "and how many sellers are there can you analyze the market and tell me suitable price"
    )

    # 1. Test aggregate fetching directly
    aggs = await _fetch_live_marketplace_aggregates(
        db=db,
        query_text=query,
        category="Agriculture",
        product_name="banana",
    )

    assert aggs["match_count"] == 0, f"Expected 0 matches for banana, got {aggs['match_count']}"
    assert aggs["seller_count"] == 0
    assert aggs["buyer_count"] == 0
    assert aggs["min_price"] is None
    assert aggs["max_price"] is None

    # 2. Test LLM grounding prompt instruction
    prompt_grounding = format_marketplace_aggregates_prompt(aggs)
    assert "0 active listings found in this marketplace database for 'banana'" in prompt_grounding
    assert "You MUST explicitly inform the user that there are currently 0 active listings" in prompt_grounding
    assert "Do NOT invent fake seller counts" in prompt_grounding


# =============================================================================
# 2. Natural Conversational Doubt & Role Classification
# =============================================================================

def test_qa_conversational_doubt_and_role_classification():
    """Verify that conversational doubt phrases do not pollute the product entity or invert the role."""
    query = (
        "i want to supply banana from indore but i am confused what price i should decide "
        "and how many sellers are there can you analyze the market and tell me suitable price"
    )
    r = extract(query)

    # Role must be SELLER (not BUYER!)
    assert r.role == RFQRole.SELLER, f"Expected SELLER, got {r.role}"

    # Product entity must be cleanly 'banana', NOT 'banana am confused have decide analyze'
    assert r.product == "banana", f"Expected 'banana', got '{r.product}'"

    # Category must be correctly inferred
    assert r.category == "Agriculture"

    # City must be correctly recognized
    assert r.city == "Indore"


@pytest.mark.parametrize(
    "query,expected_role,expected_product",
    [
        ("i want to supply banana from indore", RFQRole.SELLER, "banana"),
        ("want to sell 500 pcs corrugated boxes in surat", RFQRole.SELLER, "corrugated boxes"),
        ("looking to supply cotton yarn in tiruppur", RFQRole.SELLER, "yarn"),
        ("planning to sell 1000 kg wheat from ludhiana", RFQRole.SELLER, "wheat"),
        ("ready to supply 50 tonnes steel sheet in mumbai", RFQRole.SELLER, "sheet"),
        ("i can supply 200 office chairs in pune", RFQRole.SELLER, "chairs"),
        ("want to buy 1000 pcs usb type-c cable in delhi", RFQRole.BUYER, "cable"),
        ("looking to buy 500 shipping boxes in jaipur", RFQRole.BUYER, "boxes"),
        ("need to purchase 200 kg basmati rice", RFQRole.BUYER, "rice"),
    ],
)
def test_qa_compound_role_and_product_invariants(query, expected_role, expected_product):
    """Test compound verbs across various products to guarantee role and commodity accuracy."""
    r = extract(query)
    assert r.role == expected_role, f"Failed role for: {query}"
    assert expected_product in (r.product or "").lower(), f"Expected '{expected_product}' in '{r.product}'"


# =============================================================================
# 3. Tool Compulsion Defense (Analytical vs Action Turns)
# =============================================================================

@pytest.mark.asyncio
async def test_qa_tool_compulsion_defense():
    """Analytical turns must receive empty tools ([]) to prevent hallucinated tool calls.

    Action turns ('create rfq', 'update draft') must receive corresponding tools.
    """
    # 1. Pure market analysis query -> tools must be empty
    analysis_query = "can you analyze the market density and pricing trends for corrugated boxes in surat?"
    agent, tools, meta = await route_query_to_agent_async(analysis_query)
    assert agent.id in ("market_research", "price_analyst")
    assert tools == [], f"Expected empty tools on analytical query, got: {[t['function']['name'] for t in tools]}"

    # 2. Pure price comparison query -> tools must be empty
    price_query = "compare prices and tell me which seller is cheapest for office chairs"
    agent_p, tools_p, _ = await route_query_to_agent_async(price_query)
    assert agent_p.id == "price_analyst"
    assert tools_p == []

    # 3. Action query -> tools must be present
    action_query = "please create rfq for 500 pcs corrugated boxes at 20 inr per piece"
    agent_a, tools_a, _ = await route_query_to_agent_async(action_query)
    tool_names = [t["function"]["name"] for t in tools_a]
    assert "create_rfq" in tool_names or "update_rfq_draft" in tool_names


# =============================================================================
# 4. Catalog Multi-Term Search & Single-Character Specifiers
# =============================================================================

@pytest.mark.asyncio
async def test_qa_catalog_search_with_specifiers(db: AsyncSession, make_actor):
    """Verify search across multi-word queries with location and single-character specifiers."""
    seller = await make_actor("seller")
    buyer = await make_actor("buyer")

    # Create listing with 'Type C' and location 'Indore'
    await seller.post(
        "/rfqs",
        json=rfq_body(
            role="seller",
            category="Electronics",
            title="Fast Charge Type C Braided Cable",
            city="Indore",
            status="active",
        ),
    )

    # 1. Search 'type c indore' -> must find the cable in Indore
    res = await buyer.get("/marketplace/catalog?q=type c indore")
    assert res.status_code == 200
    items = res.json()["items"]
    assert any("Type C" in item["title"] for item in items)

    # 2. Search non-existent query -> must return 0 items cleanly, never crash
    res_none = await buyer.get("/marketplace/catalog?q=non_existent_super_rare_item_999xyz")
    assert res_none.status_code == 200
    assert res_none.json()["total"] == 0
    assert res_none.json()["items"] == []


# =============================================================================
# 5. Adversarial & Malformed Input Robustness
# =============================================================================

@pytest.mark.parametrize(
    "malformed_input",
    [
        "????$$$$%%%%!!!!",
        "1234567890",
        "   \n\t   \n   ",
        "a" * 500,
        "undefined null NaN None",
        "banana" * 50,
    ],
)
def test_qa_adversarial_input_does_not_crash_parser(malformed_input):
    """Ensure arbitrary user inputs, punctuation floods, and noise never cause unhandled crashes."""
    r = extract(malformed_input)
    assert r is not None
    # Router must also handle gracefully
    agent, tools, meta = route_query_to_agent(malformed_input)
    assert agent is not None
    assert isinstance(tools, list)
    assert isinstance(meta, dict)

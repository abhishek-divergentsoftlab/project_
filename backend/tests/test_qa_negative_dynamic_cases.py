"""Comprehensive QA Engineer Negative & Dynamic Test Suite.

Adversarial testing covering:
1. Ground truth marketplace grounding for non-existent/unlisted commodities (zero false leaks).
2. Natural conversational doubt and multi-clause query handling (zero entity pollution).
3. Compound seller/buyer role discrimination across tricky and Hinglish phrasings.
4. Tool compulsion defense: ensuring tools are stripped on analytical turns and enabled on actions.
5. Catalog multi-term search with single-character specifiers ("Type C", "5-ply").
6. Malformed/adversarial inputs (special characters, numbers only, casing, huge strings).
"""

import json

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import RFQRole, RFQStatus
from models.rfq import RFQ
from services import chat_permissions
from services.agent_router import _fallback_intent, agent_for_intent
from models.user import User
from services import market_data_tools
from services.answer_verifier import render_facts_answer
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

    viewer = await make_actor("buyer")
    viewer_user = await db.get(User, uuid.UUID(viewer.id))

    # 1. The data tools report zero, never pulling in unrelated listings
    found = await market_data_tools.run_tool(db, viewer_user, "search_listings", {"product": "banana", "category": "Agriculture"})
    assert found["data"]["distinct_listings"] == 0
    assert found["data"]["by_role"] == {"buyer": 0, "seller": 0}
    stats = await market_data_tools.run_tool(db, viewer_user, "market_price_stats", {"product": "banana", "category": "Agriculture"})
    assert stats["ok"] and stats["data"]["n_listings"] == 0 and "median" not in stats["data"]

    # 2. The deterministic answer says so plainly and invents nothing
    answer = render_facts_answer([found, stats], query)
    assert "no matching data on the platform" in answer.lower()


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

def test_qa_tool_compulsion_defense():
    """Analytical turns are offered no write tools, so the model cannot hallucinate an action.

    Action turns ('create rfq', 'update draft') are offered the matching tool. Reads are
    never gated: that is covered by the answer engine, which always offers the read tools.
    """
    # 1. Pure market analysis query -> no action tools
    analysis_query = "can you analyze the market density and pricing trends for corrugated boxes in surat?"
    agent = agent_for_intent(_fallback_intent(analysis_query))[0]
    assert agent.id in ("market_research", "price_analyst")
    allowed = chat_permissions.allowed_action_tools(analysis_query, None, None, None)
    assert allowed == set(), f"Expected no action tools on analytical query, got: {allowed}"

    # 2. Pure price comparison query -> no action tools
    price_query = "compare prices and tell me which seller is cheapest for office chairs"
    assert agent_for_intent(_fallback_intent(price_query))[0].id == "price_analyst"
    assert chat_permissions.allowed_action_tools(price_query, None, None, None) == set()

    # 3. Action query -> the write tools must be present
    action_query = "please create rfq for 500 pcs corrugated boxes at 20 inr per piece"
    allowed_a = chat_permissions.allowed_action_tools(action_query, None, None, None)
    assert "create_rfq" in allowed_a or "update_rfq_draft" in allowed_a


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
    # The routing fallback and the permission gate must also handle it gracefully
    agent, _why = agent_for_intent(_fallback_intent(malformed_input))
    assert agent is not None
    assert isinstance(chat_permissions.allowed_action_tools(malformed_input, None, None, None), set)


# =============================================================================
# 7. Seller price discovery and "buyer details" follow-up
# =============================================================================

SUPPLY_RICE_QUERY = (
    "i want to supply rice but i am confused what price i should decide and how many "
    "sellers are there can you analyze the market and tell me suitable price"
)
BUYER_FOLLOWUP_QUERY = "give me the buyer details aslo"


async def _seed_rice_market(make_actor) -> None:
    seller = await make_actor("seller")
    buyer = await make_actor("buyer")
    listings = [
        (seller, "seller", "Supplying 3,490 tonne of basmati rice", (64353, "INR", "tonne")),
        (seller, "seller", "Supplying 100 metric tons of 1121 steam basmati rice", (92000, "INR", "tons")),
        (seller, "seller", "Supplying sona masoori rice", (70, "INR", "kg")),
        (buyer, "buyer", "Need 25 metric tons of 1121 steam basmati rice", (95000, "INR", "tons")),
    ]
    for actor, role, title, price in listings:
        response = await actor.post(
            "/rfqs",
            json=rfq_body(
                role=role,
                category="Agriculture",
                title=title,
                product="basmati rice",
                quantity=(25, "tonne"),
                price=price,
                status="active",
            ),
        )
        assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_qa_rice_benchmark_normalises_units_and_splits_market_sides(db: AsyncSession, make_actor):
    """"tons" and "kg" listings must not drop out of a "tonne" benchmark, and a buyer's
    target price must be reported separately from sellers' asking prices."""
    await _seed_rice_market(make_actor)

    viewer = await make_actor("buyer")
    viewer_user = await db.get(User, uuid.UUID(viewer.id))

    sellers = await market_data_tools.market_price_stats(db, user=viewer_user, product="rice", role="seller")
    d = sellers["data"]
    assert d["n_listings"] == 3
    assert d["unit_basis"] == "tonne" and d["excluded_price_count"] == 0
    assert (d["min"], d["max"]) == (64353.0, 92000.0)
    assert d["median"] == 70000.0  # the kg listing is restated per tonne
    # all three come from one account: too thin to call a market range
    assert d["n_distinct_users"] == 1 and "sample_warning" in d

    buyers = await market_data_tools.market_price_stats(db, user=viewer_user, product="rice", role="buyer")
    assert buyers["data"]["n_listings"] == 1 and buyers["data"]["min"] == 95000.0

    counts = await market_data_tools.search_listings(db, user=viewer_user, product="rice")
    assert counts["data"]["by_role"] == {"buyer": 1, "seller": 3}


def test_qa_buyer_details_followup_is_not_a_product():
    assert extract(BUYER_FOLLOWUP_QUERY).product is None


def test_qa_llm_product_must_come_from_the_user_message():
    from services.ai_chat_service import _grounded_ai_product

    assert _grounded_ai_product("basmati rice", SUPPLY_RICE_QUERY) is None
    assert _grounded_ai_product("details aslo", BUYER_FOLLOWUP_QUERY) is None
    assert _grounded_ai_product("bananas", "i want to supply banana") == "bananas"


def test_qa_buyer_followup_keeps_product_and_seller_role():
    from services.ai_chat_service import _resolve_market_focus

    first = _resolve_market_focus(
        SUPPLY_RICE_QUERY,
        ai_extraction={"product": "basmati rice", "category": "Agriculture", "role": "seller"},
    )
    assert (first["product"], first["role"]) == ("rice", "seller")

    followup = _resolve_market_focus(
        BUYER_FOLLOWUP_QUERY,
        previous_focus={k: first[k] for k in ("product", "category", "role")},
        ai_extraction={"product": "details aslo", "role": "buyer"},
    )
    assert followup["product"] == "rice"
    assert followup["role"] == "seller"
    assert followup["listing_role"] == "buyer"


@pytest.mark.asyncio
async def test_qa_buyer_candidates_are_real_rice_buyers_only(db: AsyncSession, make_actor):
    """No unrelated "newest" listings and no fabricated 95/90/85% match scores."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from models.user import User
    from services.ai_chat_service import _cards_from_tool_results

    await _seed_rice_market(make_actor)
    other = await make_actor("seller")
    await other.post("/rfqs", json=rfq_body(role="buyer", title="Need 15,000 pcs USB Type-C cable", status="active"))
    viewer_actor = await make_actor("seller")
    viewer = (
        await db.execute(select(User).options(selectinload(User.profile)).where(User.id == viewer_actor.id))
    ).scalar_one()

    # Cards now come from the read tool the model calls, not from a keyword-gated prefetch.
    async def cards(**kw):
        return _cards_from_tool_results([await market_data_tools.search_listings(db, user=viewer, **kw)])

    candidates = await cards(product="rice", role="buyer")
    assert [c["title"] for c in candidates] == ["Need 25 metric tons of 1121 steam basmati rice"]
    assert candidates[0]["score"] is None
    json.dumps(candidates)  # persisted to the JSONB conversation state

    assert await cards(product="saffron", role="buyer") == []

"""Multi-Agent Registry for the B2B Marketplace.

Each agent is a display persona (name, icon, description, and
suggested prompts).  The chat has one prompt and one tool set; the shown agent is a label chosen
from the parser intent or the tools used, so it never gates data or actions.

The registry is a static, in-memory structure.  Adding an agent requires no
migration and no restart — just add an entry here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentDefinition:
    id: str
    name: str
    icon: str
    description: str
    short_description: str
    suggested_prompts: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent definitions
# ---------------------------------------------------------------------------

MARKET_RESEARCH_AGENT = AgentDefinition(
    id="market_research",
    name="Market Research",
    icon="🔍",
    description="Searches the marketplace for products, analyses pricing trends, competitor density, and market positioning.",
    short_description="Search & analyse the marketplace",
    suggested_prompts=[
        {"label": "📊 Rice market in India", "prompt": "What does the rice market look like? How many sellers are active and what's the price range?"},
        {"label": "🏭 Steel suppliers analysis", "prompt": "Analyse the steel pipe market — who are the top suppliers, price range, and locations?"},
        {"label": "📦 Packaging competition", "prompt": "How competitive is the corrugated box market? Show me supplier density by region."},
        {"label": "💡 Market gaps", "prompt": "What product categories have high buyer demand but few sellers?"},
        {"label": "🌍 Export opportunities", "prompt": "Which products have cross-border demand? Show international buyer-seller matches."},
    ],
)

RFQ_DRAFTING_AGENT = AgentDefinition(
    id="rfq_drafting",
    name="RFQ Drafting",
    icon="📋",
    description="Guides you step-by-step through creating a professional RFQ with clear specifications and proper trade terms.",
    short_description="Create professional RFQs",
    suggested_prompts=[
        {"label": "🍎 Buy fresh produce", "prompt": "I want to buy 500 kg of premium Basmati rice"},
        {"label": "🔌 Sell electronics", "prompt": "I'm a seller of USB Type-C cables, 1000 units available"},
        {"label": "📦 Packaging order", "prompt": "I need 5000 corrugated boxes, 5-ply, for shipping electronics"},
        {"label": "🏗️ Industrial parts", "prompt": "I want to buy stainless steel bearings, SS 316L, bore 25mm"},
    ],
)

NEGOTIATION_AGENT = AgentDefinition(
    id="negotiation",
    name="Negotiation Coach",
    icon="🤝",
    description="Expert in B2B negotiation strategy. Helps compose persuasive counter-offers, payment term proposals, and professional business communications.",
    short_description="Negotiate deals & compose messages",
    suggested_prompts=[
        {"label": "💰 Counter-offer strategy", "prompt": "A supplier quoted ₹250/kg for rice. I want to negotiate to ₹200/kg. Help me compose a counter-offer."},
        {"label": "📝 Payment terms", "prompt": "How should I propose milestone-based payment for a large order?"},
        {"label": "🤝 Build partnership", "prompt": "Draft a message proposing a long-term supply agreement with volume discounts"},
        {"label": "⚡ Respond to quote", "prompt": "I received a quotation that's 30% above market rate. Help me respond professionally."},
    ],
)

PRICE_ANALYST_AGENT = AgentDefinition(
    id="price_analyst",
    name="Price Analyst",
    icon="📊",
    description="Analyses pricing across your matches, normalises currencies, identifies the best value deals, and helps you understand your competitive position.",
    short_description="Compare prices & find best deals",
    suggested_prompts=[
        {"label": "💰 Compare top 5 prices", "prompt": "Compare the prices of my top 5 matches and tell me which is the best deal"},
        {"label": "💱 Currency comparison", "prompt": "How do the USD-quoted suppliers compare to INR ones after conversion?"},
        {"label": "📦 Total cost analysis", "prompt": "What's the total cost including estimated shipping for the top 3 matches?"},
        {"label": "📈 Am I priced right?", "prompt": "Is my asking price competitive compared to similar listings?"},
    ],
)

LOGISTICS_ADVISOR_AGENT = AgentDefinition(
    id="logistics",
    name="Logistics Advisor",
    icon="🚚",
    description="Expert guidance on shipping, Incoterms, customs documentation, freight estimation, and cross-border trade compliance.",
    short_description="Shipping, Incoterms & customs",
    suggested_prompts=[
        {"label": "🚢 FOB vs CIF vs DDP", "prompt": "Explain FOB vs CIF vs DDP — which is best for a first-time importer?"},
        {"label": "📋 Customs docs needed", "prompt": "What customs documentation do I need to import goods from China to India?"},
        {"label": "⏱️ Transit time estimate", "prompt": "How long does sea freight take from Mumbai to Dubai? What about air?"},
        {"label": "📦 Packaging advice", "prompt": "What packaging is required for shipping fragile electronics by road within India?"},
        {"label": "💰 Landed cost calculator", "prompt": "Help me estimate the landed cost for importing 1000 power banks from Shenzhen to Delhi"},
    ],
)

SUPPLIER_VERIFICATION_AGENT = AgentDefinition(
    id="verification",
    name="Supplier Verification",
    icon="✅",
    description="Evaluates counterparty reliability using KYC status, GST verification, certifications, review scores, years in business, and trade history.",
    short_description="Verify & evaluate suppliers",
    suggested_prompts=[
        {"label": "✅ Verify a supplier", "prompt": "Can you evaluate the top-ranked supplier from my matches? Check their verification status."},
        {"label": "📋 KYC checklist", "prompt": "What KYC documents should I verify before placing a large order with a new supplier?"},
        {"label": "⚠️ Red flags to watch", "prompt": "What are the red flags I should look for when evaluating a new B2B supplier?"},
        {"label": "🏆 Trusted supplier criteria", "prompt": "What makes a supplier 'trusted' on this marketplace? Explain the trust score."},
    ],
)

# The general-purpose Super Agent — orchestrates all specialized domains
GENERAL_ASSISTANT = AgentDefinition(
    id="general",
    name="Business AI Super Agent",
    icon="✨",
    description="Autonomous B2B Super Agent. Orchestrates market research, RFQ drafting, price comparison, supplier negotiations, logistics, and verification.",
    short_description="Autonomous B2B Super Agent",
    suggested_prompts=[
        {"label": "🍎 Buy apples in Indore", "prompt": "I want to buy apple in Indore"},
        {"label": "📦 Source corrugated boxes", "prompt": "I want to buy 500 corrugated boxes"},
        {"label": "🏭 List steel pipes for sale", "prompt": "I am a seller of industrial stainless steel pipes"},
        {"label": "💬 Negotiate payment terms", "prompt": "How can I negotiate better payment terms with a supplier?"},
        {"label": "✅ Supplier KYC checklist", "prompt": "What are the key supplier KYC and verification checks?"},
        {"label": "🚢 FOB vs CIF vs EXW", "prompt": "What is the difference between FOB, CIF, and EXW shipping terms?"},
    ],
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_AGENTS: dict[str, AgentDefinition] = {
    agent.id: agent
    for agent in [
        GENERAL_ASSISTANT,
        MARKET_RESEARCH_AGENT,
        RFQ_DRAFTING_AGENT,
        NEGOTIATION_AGENT,
        PRICE_ANALYST_AGENT,
        LOGISTICS_ADVISOR_AGENT,
        SUPPLIER_VERIFICATION_AGENT,
    ]
}


def get_agent(agent_id: str | None) -> AgentDefinition:
    """Return the agent definition for the given id, or the general assistant."""
    if not agent_id:
        return GENERAL_ASSISTANT
    return _AGENTS.get(agent_id, GENERAL_ASSISTANT)


def list_agents() -> list[AgentDefinition]:
    """Return all available agents, general assistant first."""
    return list(_AGENTS.values())



def agent_info(agent: AgentDefinition) -> dict[str, str]:
    """The ``routed_agent`` payload the frontend renders."""
    return {
        "id": agent.id,
        "name": agent.name,
        "icon": agent.icon,
        "short_description": agent.short_description,
    }

"""Which specialist badge to show for a chat turn.

The shown agent is cosmetic. It never decides what data the assistant may read
(every read tool is always offered) or which actions it may take (that is
``chat_permissions``). Two inputs choose it:

1. Before the answer: the ``intent`` the AI parser already produced for the
   message (``ai_query_parser.understand``, cached by ``query_extractor``), mapped
   through ``INTENT_AGENT``. No second model call is made for routing.
2. After the answer: the tools the assistant actually used (``agent_for_tools``),
   which is the better evidence.

When the parser is unavailable (disabled, down, timed out) ``_fallback_intent``
supplies a label from a few keyword rules. It is the outage fallback only.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

from services.agent_registry import (
    GENERAL_ASSISTANT,
    LOGISTICS_ADVISOR_AGENT,
    MARKET_RESEARCH_AGENT,
    NEGOTIATION_AGENT,
    PRICE_ANALYST_AGENT,
    RFQ_DRAFTING_AGENT,
    SUPPLIER_VERIFICATION_AGENT,
    AgentDefinition,
    get_agent,
)

logger = logging.getLogger(__name__)

# Parser intent (free-form label) -> shown agent. Unknown labels are "general".
INTENT_AGENT: dict[str, AgentDefinition] = {
    "search": MARKET_RESEARCH_AGENT,
    "buy_request": MARKET_RESEARCH_AGENT,
    "market_research": MARKET_RESEARCH_AGENT,
    "price_check": PRICE_ANALYST_AGENT,
    "price_analysis": PRICE_ANALYST_AGENT,
    "sell_offer": RFQ_DRAFTING_AGENT,
    "rfq_drafting": RFQ_DRAFTING_AGENT,
    "negotiation": NEGOTIATION_AGENT,
    "logistics": LOGISTICS_ADVISOR_AGENT,
    "verification": SUPPLIER_VERIFICATION_AGENT,
    "chitchat": GENERAL_ASSISTANT,
    "other": GENERAL_ASSISTANT,
    "general": GENERAL_ASSISTANT,
}

# Tool actually used -> agent. The first match in this order wins.
_TOOL_AGENT: tuple[tuple[str, AgentDefinition], ...] = (
    ("draft_counterparty_message", NEGOTIATION_AGENT),
    ("create_rfq", RFQ_DRAFTING_AGENT),
    ("update_rfq_draft", RFQ_DRAFTING_AGENT),
    ("close_rfq", RFQ_DRAFTING_AGENT),
    ("company_profile", SUPPLIER_VERIFICATION_AGENT),
    ("estimate_freight", LOGISTICS_ADVISOR_AGENT),
    ("market_price_stats", PRICE_ANALYST_AGENT),
)

# ---------------------------------------------------------------------------
# Outage fallback: used only when the AI parser returned nothing.
# ---------------------------------------------------------------------------
_I = re.IGNORECASE
_OWN_TRADE_RE = re.compile(
    r"\b(i|we)\s+(want|wish|would\s+like|need|plan|am\s+looking|are\s+looking)\s+to\s+(buy|sell|purchase|procure|source|supply|export|import)\b"
    r"|\b(i|we)\s+(am|are)\s+(buying|selling|supplying|sourcing|procuring|exporting)\b|\b(i'm|we're|im)\s+(buying|selling|supplying|sourcing|procuring|exporting)\b"
    r"|(?:bechna|bechni|bechne|kharidna|kharidni|khareedna|lena|chahiye|खरीदना|खरीदने|बेचना|बेचने|चाहिए)",
    _I,
)
_SELL_SIDE_RE = re.compile(r"\b(sell|selling|supply|supplying|export|exporting|bechna|bechne|बेचना|बेचने)\b", _I)
# Order matters: the first matching label wins.
_FALLBACK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("verification", re.compile(
        r"\b(kyc|gstin?|tin\s+number|verif(y|ied|ication)|trustworthy|legit(imate)?|scam|fraud|red\s+flags?|"
        r"background\s+check|due\s+diligence|credibility|certificat(e|es|ion|ions)|reputation|years\s+in\s+business)\b"
        r"|\bpan\b(?![\s-]*india)", _I)),
    ("logistics", re.compile(
        r"\b(logistics?|shipping|freight|cargo|transport(ation)?|incoterms?|fob|cif|exw|ddp|cfr|customs|duties|"
        r"bill\s+of\s+lading|transit\s+time|landed\s+cost|pallet|container)\b|\bpan[\s-]*india\b", _I)),
    ("negotiation", re.compile(
        r"\b(negotiat(e|ing|ion)|counter-?offer|bargain|payment\s+terms?|letter\s+of\s+credit|ask\s+for\s+discount)\b", _I)),
    ("price_check", re.compile(
        r"\b(compare\s+prices?|price\s+comparison|cheapest|lowest\s+price|highest\s+price|best\s+(deal|value)|"
        r"cost\s+(analysis|breakdown)|per\s+unit|(good|fair|bad|reasonable)\s+(deal|price|rate|offer))\b", _I)),
    ("market_research", re.compile(
        r"\b(market\s+(research|intelligence|density|trend|analysis|rate|price|size|gaps?|conditions?)|price\s+range|"
        r"how\s+many\s+(sellers?|buyers?|suppliers?)|supply\s+and\s+demand|competitors?|competitive|"
        r"export\s+opportunit(y|ies)|what\s+does\s+the\s+.*market\s+look\s+like)\b"
        r"|\b(analy[sz]e|evaluate|assess)\b.*\b(listing|deal|offer|quote|price|market)\b", _I)),
    ("search", re.compile(
        r"\b(mail\s*ids?|e-?mails?|top\s+(\d+|one|two|three|four|five|ten))\b"
        r"|\b(show|find|list|give|get|browse|search|recommend|suggest|view)\s+(me\s+|us\s+)?(the\s+|any\s+|all\s+|some\s+|top\s+)?"
        r"(rfqs?|listings?|matches|suppliers?|buyers?|sellers?|vendors?|manufacturers?|dealers?|counterparties?)\b"
        r"|\bwho\s+(sells|supplies|buys|stocks|exports|imports|is\s+(selling|supplying|buying))\b"
        r"|\bmatching\s+(suppliers?|sellers?|buyers?|candidates?)\b|\bwhich\s+(one\s+)?suits?\s+best\b"
        r"|\b(koi|kaun|kon)\s+(sa\s+)?(supplier|seller|vendor|buyer)s?\b", _I)),
)


def states_own_trade(message: str) -> bool:
    """True when the user states their own wish to buy or sell ("I want to sell iso pipes")."""
    return bool(_OWN_TRADE_RE.search(message or ""))


def _fallback_intent(message: str) -> str:
    """Intent label from keyword rules. This is the OUTAGE FALLBACK for when the AI parser gives nothing.

    The parser's intent is always preferred; these rules exist so chat still
    routes sensibly when the local model is disabled, down or timed out. Returns
    a label in the parser's vocabulary.
    """
    from services.chat_permissions import is_question

    text = (message or "").strip()
    if not text:
        return "other"
    if states_own_trade(text) and not is_question(text):
        return "sell_offer" if _SELL_SIDE_RE.search(text) else "buy_request"
    for label, pattern in _FALLBACK_RULES:
        if pattern.search(text):
            return label
    return "other"


def agent_for_intent(
    intent: Optional[str],
    *,
    draft: Optional[dict[str, Any]] = None,
    explicit_agent_id: Optional[str] = None,
) -> tuple[AgentDefinition, str]:
    """The agent to show before the answer, and why.

    An unmapped intent falls back to the specialist the user picked in the UI
    (``explicit_agent_id``), and failing that to RFQ drafting while the draft
    has no role or product yet.
    """
    key = re.sub(r"[\s\-]+", "_", str(intent or "").strip().lower())
    agent = INTENT_AGENT.get(key, GENERAL_ASSISTANT)
    if agent is not GENERAL_ASSISTANT:
        return agent, f"parser intent '{key}'"
    if explicit_agent_id and explicit_agent_id != "general":
        return get_agent(explicit_agent_id), f"requested specialist ({explicit_agent_id})"
    d = draft or {}
    if not d.get("role") or not ((d.get("product_details") or {}).get("name") or d.get("category")):
        return RFQ_DRAFTING_AGENT, "no RFQ details yet"
    return GENERAL_ASSISTANT, "general"


def agent_for_tools(tool_names: Iterable[str]) -> Optional[AgentDefinition]:
    """The agent that matches the tools the assistant actually called, or None if it used none of them."""
    used = set(tool_names)
    for name, agent in _TOOL_AGENT:
        if name in used:
            return agent
    return None


def agent_info(agent: AgentDefinition) -> dict[str, str]:
    """The ``routed_agent`` payload the frontend renders."""
    return {
        "id": agent.id,
        "name": agent.name,
        "icon": agent.icon,
        "short_description": agent.short_description,
    }

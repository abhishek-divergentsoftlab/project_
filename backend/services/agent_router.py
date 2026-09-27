"""Autonomous Super Agent Router for the B2B Marketplace.

The Super Agent acts as the central intelligence orchestrator. It autonomously
analyzes the user's conversational intent, procurement state, active negotiations,
and context to route each turn to the optimal specialized persona:
- Market Research Specialist
- RFQ Drafting Specialist
- Negotiation Coach
- Price Analyst
- Logistics Advisor
- Supplier Verification Specialist
- General Business AI Orchestrator

The user does NOT manually switch agents; the Super Agent automatically decides
the best specialist and provisions the correct tools without ever causing dead-ends.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx

from core.config import settings
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
from services import rfq_tool_service

logger = logging.getLogger(__name__)


async def _extract_semantic_intent_with_ai(
    user_query: str,
    timeout_sec: float = 4.0,
) -> Optional[dict[str, Any]]:
    """Use local Ollama model to semantically extract product, role, location, and intent."""
    clean_q = (user_query or "").strip()
    if not clean_q or len(clean_q) < 4:
        return None

    prompt = f"""You are a B2B marketplace AI parser. Extract the goods/service and trade details from this message:
User: "{clean_q}"

JSON Schema:
{{
  "product": string or null (the specific commodity or goods being bought/sold/analyzed, e.g. "banana", "office chair", "basmati rice"),
  "category": string or null (e.g. "Agriculture", "Electronics", "Packaging", "Furniture", "Textiles", "Industrial"),
  "role": "seller" (if supplying/selling/offering) or "buyer" (if buying/purchasing/needing) or null,
  "location": string or null,
  "intent": "market_research" | "price_analysis" | "rfq_drafting" | "negotiation" | "logistics" | "verification" | "general",
  "action_needed": "none" | "create_rfq" | "update_draft" | "draft_message"
}}
Return ONLY valid JSON."""

    # Prefer lightweight model for ultra-low latency; fallback to primary model
    model_name = "qwen2.5:1.5b" if settings.OLLAMA_MODEL != "qwen2.5:1.5b" else settings.OLLAMA_MODEL
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": 4096},
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.post(f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate", json=payload)
            if resp.status_code == 200:
                raw = resp.json().get("response", "{}")
                data = json.loads(raw)
                if isinstance(data, dict) and any(data.get(k) for k in ("product", "role", "intent", "location")):
                    from services import query_extractor
                    if data.get("product") and not data.get("category"):
                        data["category"] = query_extractor._infer_category(str(data["product"]))
                    return data
    except Exception as exc:
        logger.debug("AI semantic extraction pass skipped/timed out (%s); using pattern fallback", exc)
    return None


# ---------------------------------------------------------------------------
# Semantic Intent Signatures
# ---------------------------------------------------------------------------

VERIFICATION_PATTERNS = [
    r"\b(kyc|gst|pan|tin)\b",
    r"\b(verif(y|ied|ication))\b",
    r"\b(trust|trustworthy|legit|legitimate|scam|fraud|risk|red flag)\b",
    r"\b(background check|due diligence|credibility)\b",
    r"\b(certif(icate|ication|ied)|iso|ce|fda|gmp|rohs)\b",
    r"\b(reputation|rating|reviews?|years in business)\b",
]

LOGISTICS_PATTERNS = [
    r"\b(logistics?|shipping|freight|cargo|transport|transportation)\b",
    r"\b(incoterms?|fob|cif|exw|ddp|cfr|cip|fas|dat|dap)\b",
    r"\b(customs|duties|import doc|export doc|bill of lading|port)\b",
    r"\b(transit time|sea freight|air freight|road freight|dispatch time)\b",
    r"\b(packaging|pallet|container|landed cost)\b",
]

NEGOTIATION_PATTERNS = [
    r"\b(negotiat(e|ing|ion)|counter-?offer|counter offer)\b",
    r"\b(ask for discount|give discount|lower the price|drop price|reduce price|bargain|deal terms)\b",
    r"\b(payment terms?|milestone|advance payment|letter of credit|lc|net 30|net 60)\b",
    r"\b(draft message|send message|compose message|reply to (supplier|buyer|counterparty))\b",
    r"\b(write to (them|him|her|supplier)|tell (them|supplier))\b",
]

PRICE_ANALYSIS_PATTERNS = [
    r"\b(compare prices?|price comparison|cheapest|lowest price|highest price|lower price|higher price)\b",
    r"\b(lowest.*seller|highest.*seller|cheapest.*seller)\b",
    r"\b(best deal|best value|cost analysis|cost breakdown)\b",
    r"\b(per unit|unit economics|currency|usd to inr|inr to usd)\b",
    r"\b(quote comparison|pricing trends?|which (one )?suits best)\b",
]

MARKET_RESEARCH_PATTERNS = [
    r"\b(market research|market intelligence|market density|market trend)\b",
    r"\b(analyze market|market analysis|suitable price|what price|pricing strategy|market rate)\b",
    r"\b(how many (sellers?|buyers?|suppliers?)|supplier density|buyer density)\b",
    r"\b(supply and demand|market size|competitors?|market share)\b",
    r"\b(market look like|market gaps?|export opportunit(y|ies))\b",
    r"\b(price range in|industry overview|active listings?)\b",
]

RFQ_DRAFTING_PATTERNS = [
    r"\b(i want to (buy|sell)|looking to (buy|sell)|want to purchase)\b",
    r"\b(draft rfq|create rfq|publish rfq|make rfq|post rfq|submit rfq)\b",
    r"\b(rfq draft|specifications?|delivery location|target price|budget)\b",
    r"\b(quote for|need \d+|quantity \d+)\b",
    r"\b(amend rfq|update draft|change quantity|change price)\b",
]


def _matches_any(text: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def route_query_to_agent(
    user_query: str,
    current_draft: Optional[dict[str, Any]] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[str] = None,
    last_counterparty_msg: Optional[dict[str, Any]] = None,
    explicit_agent_id: Optional[str] = None,
) -> tuple[AgentDefinition, list[dict[str, Any]], dict[str, Any]]:
    """Determine the optimal specialist agent and provision necessary tools.
    
    The Super Agent ensures no tools are stripped if the user expresses an action intent,
    even during market research or price analysis.
    
    Returns:
        tuple of (AgentDefinition, tools_list, routing_metadata)
    """
    clean_query = (user_query or "").strip()
    norm_query = clean_query.lower()
    draft = current_draft or {}
    has_matches = bool(matched_candidates)
    has_connection = bool(active_connection_id or last_counterparty_msg)

    # 1. Determine action requirements (tools must NEVER be lost)
    requires_create_rfq = rfq_tool_service.check_create_rfq_intent(norm_query)
    requires_close_rfq = rfq_tool_service.check_close_rfq_intent(norm_query)
    requires_counterparty_msg = rfq_tool_service.check_counterparty_message_intent(
        norm_query,
        has_active_connection=has_connection,
        has_previous_counterparty_msg=bool(last_counterparty_msg),
    )

    # Also detect if user is updating draft fields (e.g. "change quantity to 500", "location is Mumbai")
    is_rfq_field_update = bool(
        re.search(r"\b(quantity|qty|price|budget|location|city|deadline|specs?|role|buy|sell)\b", norm_query)
        and (not has_connection or not requires_counterparty_msg)
    )

    # 2. Select Specialized Delegate Persona
    selected_agent: AgentDefinition = GENERAL_ASSISTANT
    routing_reason = "General B2B Orchestration"
    confidence = 0.85

    if _matches_any(clean_query, VERIFICATION_PATTERNS):
        selected_agent = SUPPLIER_VERIFICATION_AGENT
        routing_reason = "Verification & Trust Assessment Intent"
        confidence = 0.95

    elif _matches_any(clean_query, LOGISTICS_PATTERNS):
        selected_agent = LOGISTICS_ADVISOR_AGENT
        routing_reason = "Shipping & Logistics Intent"
        confidence = 0.95

    elif requires_counterparty_msg or _matches_any(clean_query, NEGOTIATION_PATTERNS) or (has_connection and "counter" in norm_query):
        selected_agent = NEGOTIATION_AGENT
        routing_reason = "Commercial Negotiation & Messaging Intent"
        confidence = 0.92

    elif _matches_any(clean_query, PRICE_ANALYSIS_PATTERNS) or (has_matches and any(w in norm_query for w in ["cheapest", "compare", "best deal", "suits best"])):
        selected_agent = PRICE_ANALYST_AGENT
        routing_reason = "Price & Deal Value Comparison Intent"
        confidence = 0.92

    elif _matches_any(clean_query, MARKET_RESEARCH_PATTERNS):
        selected_agent = MARKET_RESEARCH_AGENT
        routing_reason = "Marketplace Intelligence & Density Inquiry"
        confidence = 0.90

    elif requires_create_rfq or is_rfq_field_update or _matches_any(clean_query, RFQ_DRAFTING_PATTERNS):
        selected_agent = RFQ_DRAFTING_AGENT
        routing_reason = "RFQ Drafting & Procurement Specification Intent"
        confidence = 0.95

    elif has_matches and rfq_tool_service.check_match_query_intent(norm_query):
        selected_agent = PRICE_ANALYST_AGENT
        routing_reason = "Candidate Match Evaluation"
        confidence = 0.88

    else:
        # If an explicit agent was requested and is valid, respect it as a fallback hint
        if explicit_agent_id and explicit_agent_id != "general":
            selected_agent = get_agent(explicit_agent_id)
            routing_reason = f"Requested Specialist Fallback ({explicit_agent_id})"
            confidence = 0.80
        else:
            # If draft is missing role or product, default to RFQ drafting guidance
            has_role = bool(draft.get("role"))
            has_prod = bool(draft.get("product_details", {}).get("name") or draft.get("category"))
            if not has_role or not has_prod:
                selected_agent = RFQ_DRAFTING_AGENT
                routing_reason = "RFQ Inception & Requirement Gathering"
                confidence = 0.85
            else:
                selected_agent = GENERAL_ASSISTANT
                routing_reason = "General B2B Super Agent Guidance"
                confidence = 0.85

    # 3. Dynamic Tool Allocation: Combine agent tools with user action tools
    # Crucial Fix: NEVER strip tools when the user asked for an action!
    tool_map = {
        "update_rfq_draft": rfq_tool_service.UPDATE_RFQ_DRAFT_TOOL,
        "create_rfq": rfq_tool_service.CREATE_RFQ_TOOL,
        "draft_counterparty_message": rfq_tool_service.DRAFT_COUNTERPARTY_MESSAGE_TOOL,
        "send_counterparty_message": rfq_tool_service.DRAFT_COUNTERPARTY_MESSAGE_TOOL,
        "close_rfq": rfq_tool_service.CLOSE_RFQ_TOOL,
    }

    active_tool_names: set[str] = set()

    # Agent's inherent tools
    for t_name in selected_agent.tools:
        if t_name in tool_map:
            active_tool_names.add(t_name)

    is_analysis = (
        rfq_tool_service.check_analysis_query_intent(norm_query)
        or selected_agent.id in ("market_research", "logistics", "verification")
        or (selected_agent.id == "price_analyst" and not has_matches)
    )

    is_explicit_action = (
        requires_create_rfq
        or requires_close_rfq
        or requires_counterparty_msg
        or bool(re.search(r"\b(create rfq|publish rfq|make rfq|post rfq|submit rfq|update draft|change quantity to|change price to)\b", norm_query))
    )

    is_pure_analysis = bool(is_analysis and not is_explicit_action)
    if is_pure_analysis:
        active_tool_names.clear()
    else:
        if requires_create_rfq or is_rfq_field_update or selected_agent.id in ("rfq_drafting", "general"):
            active_tool_names.add("update_rfq_draft")
            active_tool_names.add("create_rfq")

        if requires_counterparty_msg or selected_agent.id in ("negotiation", "general"):
            active_tool_names.add("draft_counterparty_message")

        if requires_close_rfq:
            active_tool_names.add("close_rfq")

    final_tools: list[dict[str, Any]] = []
    seen_tool_names: set[str] = set()
    for name in active_tool_names:
        tool_obj = tool_map.get(name)
        if tool_obj:
            fn_name = tool_obj.get("function", {}).get("name")
            if fn_name and fn_name not in seen_tool_names:
                seen_tool_names.add(fn_name)
                final_tools.append(tool_obj)

    metadata = {
        "agent_id": selected_agent.id,
        "agent_name": selected_agent.name,
        "agent_icon": selected_agent.icon,
        "routing_reason": routing_reason,
        "confidence": confidence,
        "action_intents": {
            "requires_create_rfq": requires_create_rfq,
            "requires_counterparty_msg": requires_counterparty_msg,
            "requires_close_rfq": requires_close_rfq,
            "is_pure_analysis": is_pure_analysis,
        },
    }

    logger.info(
        "Super Agent routed query '%s...' to '%s' (Reason: %s, Tools: %s)",
        clean_query[:60],
        selected_agent.name,
        routing_reason,
        list(active_tool_names),
    )

    return selected_agent, final_tools, metadata


async def route_query_to_agent_async(
    user_query: str,
    current_draft: Optional[dict[str, Any]] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[str] = None,
    last_counterparty_msg: Optional[dict[str, Any]] = None,
    explicit_agent_id: Optional[str] = None,
    timeout_sec: float = 4.0,
) -> tuple[AgentDefinition, list[dict[str, Any]], dict[str, Any]]:
    """Intelligently route each turn using local AI semantic comprehension with fallback.

    1. Uses local Ollama model to semantically extract:
       - intent ('market_research', 'price_analysis', 'rfq_drafting', 'negotiation', 'logistics', 'verification', 'general')
       - product, category, role, location
       - action_needed ('none', 'create_rfq', 'update_draft', 'draft_message')
    2. Provisions tools ONLY when an action is needed, preventing tool hallucination.
    3. If AI extraction times out or is unavailable, falls back gracefully to route_query_to_agent.
    """
    clean_query = (user_query or "").strip()
    ai_data = None
    if len(clean_query) >= 4:
        ai_data = await _extract_semantic_intent_with_ai(clean_query, timeout_sec=timeout_sec)

    if ai_data:
        intent = str(ai_data.get("intent") or "").lower()
        action_needed = str(ai_data.get("action_needed") or "none").lower()

        intent_agent_map = {
            "market_research": (MARKET_RESEARCH_AGENT, "AI Semantic Market Intelligence Intent", 0.96),
            "price_analysis": (PRICE_ANALYST_AGENT, "AI Semantic Price & Deal Valuation Intent", 0.95),
            "negotiation": (NEGOTIATION_AGENT, "AI Semantic Commercial Negotiation Intent", 0.95),
            "logistics": (LOGISTICS_ADVISOR_AGENT, "AI Semantic Shipping & Logistics Intent", 0.95),
            "verification": (SUPPLIER_VERIFICATION_AGENT, "AI Semantic Trust & Verification Intent", 0.95),
            "rfq_drafting": (RFQ_DRAFTING_AGENT, "AI Semantic RFQ Drafting & Procurement Intent", 0.95),
        }

        if intent in intent_agent_map:
            selected_agent, routing_reason, confidence = intent_agent_map[intent]

            tool_map = {
                "update_rfq_draft": rfq_tool_service.UPDATE_RFQ_DRAFT_TOOL,
                "create_rfq": rfq_tool_service.CREATE_RFQ_TOOL,
                "draft_counterparty_message": rfq_tool_service.DRAFT_COUNTERPARTY_MESSAGE_TOOL,
                "close_rfq": rfq_tool_service.CLOSE_RFQ_TOOL,
            }

            final_tools: list[dict[str, Any]] = []
            if action_needed == "create_rfq":
                final_tools = [rfq_tool_service.CREATE_RFQ_TOOL, rfq_tool_service.UPDATE_RFQ_DRAFT_TOOL]
            elif action_needed == "update_draft":
                final_tools = [rfq_tool_service.UPDATE_RFQ_DRAFT_TOOL]
            elif action_needed == "draft_message":
                final_tools = [rfq_tool_service.DRAFT_COUNTERPARTY_MESSAGE_TOOL]
            elif action_needed == "none":
                final_tools = []
            else:
                for t_name in selected_agent.tools:
                    if t_name in tool_map and t_name not in [f.get("function", {}).get("name") for f in final_tools]:
                        final_tools.append(tool_map[t_name])

            metadata = {
                "agent_id": selected_agent.id,
                "agent_name": selected_agent.name,
                "agent_icon": selected_agent.icon,
                "routing_reason": routing_reason,
                "confidence": confidence,
                "semantic_extraction": ai_data,
                "action_intents": {
                    "action_needed": action_needed,
                    "is_pure_analysis": action_needed == "none",
                },
            }
            logger.info(
                "Super Agent (AI Semantic Async) routed '%s...' to '%s' (Intent: %s, Action: %s, Tools: %s)",
                clean_query[:60],
                selected_agent.name,
                intent,
                action_needed,
                [t["function"]["name"] for t in final_tools],
            )
            return selected_agent, final_tools, metadata

    # Deterministic fallback when AI times out or is bypassed
    selected_agent, final_tools, metadata = route_query_to_agent(
        user_query=user_query,
        current_draft=current_draft,
        matched_candidates=matched_candidates,
        active_connection_id=active_connection_id,
        last_counterparty_msg=last_counterparty_msg,
        explicit_agent_id=explicit_agent_id,
    )
    if ai_data:
        metadata["semantic_extraction"] = ai_data
    return selected_agent, final_tools, metadata

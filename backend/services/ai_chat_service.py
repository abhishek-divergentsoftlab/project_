"""AI Conversational Business Assistant service powered by local Ollama gpt-oss:20b.

Enforces strict B2B business-only guardrails for procurement, RFQs,
supplier negotiation, pricing, logistics, and marketplace transactions.
Integrates rfq_tool_service for structured multi-field preprocessing
and LLM tool calling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, Union
import uuid

import httpx
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.conversation import Conversation, Message
from models.enums import ConversationType, MessageRole, RFQRole, RFQStatus
from models.user import User
from services import answer_engine, rfq_tool_service
from services.answer_engine import AnswerContext
from services.agent_registry import AgentDefinition, GENERAL_ASSISTANT
from services import (
    ai_query_parser, chat_conversation, chat_draft_assist, chat_permissions, chat_reply, chat_rfq_edit, chat_spec_filing, chat_specs,
)
from services.agent_router import agent_for_intent, agent_for_tools, agent_info

logger = logging.getLogger(__name__)
timing_log = logging.getLogger("chat.timing")  # per-phase wall time of a chat turn, at DEBUG

SYSTEM_BUSINESS_PROMPT = """You are a specialized B2B Business & Procurement Assistant.

CRITICAL INSTRUCTION - STRICT BREVITY & DIRECT INFORMATION ONLY:
- Keep every answer as SHORT, CONCISE, and direct as possible.
- Provide ONLY essential, directly relevant business facts, bullet points, or concise tables.
- Absolutely ZERO conversational filler, pleasantries, preamble, or irrelevant chatter.
- Do NOT say "Sure, here is...", "I hope this helps!", or similar fluff. Start directly with the answer.

SCOPE (Business & B2B Trade Only):
- RFQs, product specifications, procurement, and supplier selection
- Pricing, bulk terms, payment milestones, negotiation tactics, and KYC compliance
- Supply chain logistics, freight, Incoterms, and packaging
- Marketplace listings analysis, price benchmarking, market condition comparisons, and commercial feasibility

TRUTH RULES (marketplace facts):
- When asking for missing details, never give example quantities or prices.
- Use the data tools to look up any marketplace fact (listings, counts, prices, matches, freight, verification).
- State ONLY numbers that came from a tool result or from the user's own message.
- NEVER use outside knowledge for prices, rates, counts or sizes: no "typical" or "market" rates, no assumed pack sizes.
- Do NOT do arithmetic that is not in tool output (e.g. no cases-per-bottle or per-kg conversions of your own).
- If a tool returns no data, say so plainly (for example "There are no seller listings for X on the platform yet, so this price cannot be compared") and suggest a next step such as posting an RFQ or widening the location.
- If a result has a sample_warning, say the sample is too small to be a market range. If duplicates_removed is above 0, say duplicates were removed.

STRICT POLICY:
- Always answer business inquiries, listing evaluations, market comparisons, and RFQ questions directly, using only tool-backed data.
- ONLY if the query is completely unrelated to business, trade, or procurement (e.g., creative writing, pop culture, video games, casual gossip):
Respond with ONLY: "I only assist with business, procurement, and B2B trade inquiries."
"""


def _build_ollama_options() -> dict[str, Any]:
    """Build Ollama options dict, respecting native model context when num_ctx is not explicitly configured."""
    opts: dict[str, Any] = {"temperature": 0.3}
    configured_ctx = getattr(settings, "AI_CHAT_NUM_CTX", 0)
    if configured_ctx and configured_ctx > 0:
        opts["num_ctx"] = configured_ctx
    return opts


_COMMERCIAL_TERMS_RE = re.compile(
    r"\b("
    r"moq|steel|pipes?|tonnes?|wheat|chawal|rate|daam|bhav|kya\s+hai|gehu|arroz|"
    r"listings?|orders?|prices?|pcs|pieces?|quantity|costs?|quotes?|"
    r"markets?|suppliers?|buyers?|sellers?|t-shirts?|cotton|textiles?|"
    r"agriculture|packaging|electronics|inr|usd|eur|gbp|fob|"
    r"cif|rfqs?|kyc|shipping|freight|incoterms?|kg|ton|tons|mt|quintals?|"
    r"cement|sugar|manufacturers?|payments?|advance|credit|disputes?|escrow|"
    r"demurrage|detention|gst|transport|procurement|procure|b2b|invoices?|"
    r"exports?|imports?|trade|viable|viability|chemicals|minerals|plastics|"
    r"metals|construction|hardware|logistics|audit|inspections?|lc|letter\s+of\s+credit|"
    r"bank\s+guarantee|ex-factory|ex\s+factory|stock|inventory|wholesale|wholesalers?|"
    r"retail|boxes?|cartons?|bottles?|tins?|bags?|rolls?|"
    r"transit|transporters?|goods|cargo|damaged?|compensation|claims?|carriers?|"
    r"consignments?|challan|bill\s+of\s+lading|lorry\s+receipt|lr"
    r")\b",
    re.IGNORECASE,
)


def is_false_positive_refusal(reply_text: str, user_query: str) -> bool:
    """Check if the model refused a query that is actually a valid B2B trade inquiry."""
    if not reply_text:
        return False
    refusal_needle = "i only assist with business"
    if refusal_needle not in reply_text.lower():
        return False
    return bool(_COMMERCIAL_TERMS_RE.search(user_query or ""))


_RFQ_CREATION_CLAIM_RE = re.compile(
    r"(✅|🎉)[^\n]{0,60}\b(rfq|listing)\b[^\n]{0,30}\b(created|published|raised|posted|live|listed)\b"
    r"|\b(created|published|raised|posted|listed)\s+successfully\b"
    r"|\bsuccessfully\s+(created|published|raised|posted|listed)\b"
    r"|\brfq\s+id\s*[:：]"
    r"|\b(?:rfq|listing)\s+(?:has\s+been\s+|is\s+now\s+|was\s+)?(?:published|posted|created|raised|listed)\s*[:：!.]"
    r"|\b(?:rfq|listing)\b[^\n]{0,40}\b(?:has\s+been|have\s+been|was|is\s+now)\s+(?:successfully\s+)?(?:created|published|posted|raised|listed|live)\b"
    r"|\b(?:i(?:'ve|\s+have)?|we(?:'ve|\s+have)?)\s+(?:now\s+)?(?:published|posted|created|listed|raised)\s+(?:your|the|this|it)\b"
    r"|\b(?:is|are)\s+now\s+live\b"
    # Hinglish: "publish ho gaya", "post kar diya", "rfq ban gaya", "live ho gaya"
    r"|\b(?:publish|post|create|list|live)\s+(?:ho\s+gaya|ho\s+gayi|hogaya|kar\s+diya|kar\s+di|kardiya)\b"
    r"|\b(?:rfq|listing)\s+(?:ban\s+gaya|ban\s+gayi|bana\s+diya|bana\s+di)\b",
    re.IGNORECASE,
)
# In edit mode "saved" / "updated" is the false claim ("changes saved" when nothing was saved).
_RFQ_SAVE_CLAIM_RE = re.compile(
    r"(✅|🎉)[^\n]{0,60}\b(saved|updated)\b"
    r"|\b(?:saved|updated)\s+successfully\b"
    r"|\bsuccessfully\s+(?:saved|updated)\b"
    r"|\b(?:changes|edits|rfq|listing|draft)\b[^\n]{0,40}\b(?:has\s+been|have\s+been|was|were|is\s+now)\s+(?:successfully\s+)?(?:saved|updated|applied)\b"
    r"|\b(?:i(?:'ve|\s+have)?)\s+(?:now\s+)?(?:saved|updated|applied)\s+(?:your|the|these|this|it)\b"
    r"|\b(?:save|update)\s+(?:ho\s+gaya|ho\s+gayi|hogaya|kar\s+diya|kar\s+di|kardiya)\b",
    re.IGNORECASE,
)


def claims_rfq_created(reply_text: str) -> bool:
    """True when a reply tells the user an RFQ was created, published, posted or listed."""
    return bool(reply_text and _RFQ_CREATION_CLAIM_RE.search(reply_text))


def claims_rfq_saved(reply_text: str) -> bool:
    """True when a reply tells the user edits were saved or an RFQ was updated."""
    return bool(reply_text and _RFQ_SAVE_CLAIM_RE.search(reply_text))


def unbacked_creation_reply(draft: dict[str, Any], readiness: dict[str, Any], editing: bool = False) -> str:
    """Replacement for a model reply that claims an RFQ was published or saved when nothing was.

    States only what is true; the conversation's own next question or recap follows it.
    """
    if editing:
        return "⚠️ **Nothing has been saved yet.** Your changes are still pending; say \"save changes\" when you are done."
    return "⚠️ **Your RFQ has not been created yet.** Nothing was published to the marketplace."


async def _recover_from_false_refusal(
    prepared_messages: list[dict[str, Any]],
    last_user_content: str,
    call_model: bool = False,
) -> Optional[str]:
    """Recover from false-positive refusal by re-prompting the model (in streaming) or providing direct business guidance."""
    logger.warning(
        "Detected false-positive guardrail refusal for business inquiry: '%s'. Recovering reply (call_model=%s).",
        last_user_content[:80],
        call_model,
    )
    fallback = (
        "I am here to assist with your B2B trade, procurement, and marketplace inquiries. "
        f"Regarding your request ('{last_user_content[:60]}'), please let me know your desired specifications, "
        "quantity, target price, or delivery destination, and I will assist you with comparing against platform listings, "
        "matching suppliers, or preparing your listing."
    )
    if not call_model:
        return fallback
    try:
        url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
        recovery_messages = [
            m for m in (prepared_messages or [])
            if isinstance(m, dict) and m.get("role") != "system"
        ]
        if not recovery_messages:
            recovery_messages = [{"role": "user", "content": last_user_content}]
        system_nudge = {
            "role": "system",
            "content": (
                "You are an AI assistant for a B2B trade marketplace. The user's query is a legitimate business inquiry. "
                "Provide a direct, helpful, and courteous business answer without refusing."
            ),
        }
        payload = {
            "model": settings.OLLAMA_MODEL,
            "messages": [system_nudge] + recovery_messages,
            "stream": False,
        }
        timeout = min(15.0, float(settings.AI_CHAT_TIMEOUT_SECONDS))
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                msg_content = ((data.get("message") or {}).get("content") or "").strip()
                if msg_content and not is_false_positive_refusal(msg_content, last_user_content):
                    return msg_content
    except Exception as exc:
        logger.warning("Recovery call to Ollama failed: %s", exc)

    return fallback


def format_rfq_summary_text(draft: dict[str, Any], readiness: dict[str, Any]) -> str:
    """Generate an intelligent, accurate acknowledgment reflecting all captured and missing fields."""
    if not readiness.get("has_role"):
        return "Are you looking to **BUY** or **SELL** goods?"

    role_str = str(draft.get("role") or "buyer").upper()

    if not readiness.get("has_product"):
        return f"Understood, registered as **{role_str}**. What specific **product or item** are you looking to buy or sell?"

    # Required fields are complete! Check all captured details
    prod_name = (draft.get("product_details") or {}).get("name") or draft.get("category") or "Product"
    lines = [f"📋 **RFQ Draft Updated** ({role_str}):"]
    lines.append(f"- **Product**: {prod_name.title() if isinstance(prod_name, str) else prod_name}")
    if draft.get("category"):
        lines.append(f"- **Category**: {draft['category']}")

    if draft.get("quantity") and draft["quantity"].get("value") is not None:
        q = draft["quantity"]
        val = q.get("value")
        val_str = f"{val:,.0f}" if isinstance(val, (int, float)) and val.is_integer() else f"{val}"
        lines.append(f"- **Quantity**: {val_str} {q.get('unit', '')}".strip())

    if draft.get("price_target") and draft["price_target"].get("amount") is not None:
        p = draft["price_target"]
        cur = p.get("currency", "INR")
        amt = p.get("amount")
        per_u = f" / {p.get('per_unit')}" if p.get("per_unit") else ""
        lines.append(f"- **Target Price**: {cur} {amt}{per_u}")

    if draft.get("location"):
        loc = draft["location"]
        loc_str = ", ".join(loc.get(k) for k in ("city", "state", "country") if loc.get(k))
        if loc_str:
            lines.append(f"- **Delivery Location**: {loc_str}")

    if draft.get("deadline"):
        dl = draft["deadline"]
        if dl.get("date"):
            lines.append(f"- **Deadline**: {dl.get('date')}")
        elif dl.get("in_days"):
            lines.append(f"- **Deadline**: within {dl.get('in_days')} days")

    # Missing optional fields
    missing_opts = []
    if not draft.get("quantity"):
        missing_opts.append("quantity")
    if not draft.get("price_target"):
        missing_opts.append("target budget/price")
    if not draft.get("location") or not (draft.get("location") or {}).get("city"):
        missing_opts.append("delivery location")
    if not draft.get("deadline"):
        missing_opts.append("delivery deadline")

    if missing_opts:
        opts_prompt = f"Would you like to specify **{', '.join(missing_opts)}**, or proceed to create this RFQ?"
    else:
        opts_prompt = "All key commercial terms are recorded! Would you like to add any technical specifications, or proceed to create this RFQ?"

    lines.append("")
    lines.append(opts_prompt)
    return "\n".join(lines)


def build_sliding_window_messages(
    messages: list[Any],
    max_history_turns: int = 8,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Partition messages into a sliding window and a compact historical summary.
    
    Ensures prompt token count stays bounded (~2,000-2,500 tokens) even in 50+ turn
    conversations, eliminating inference latency spikes and local Ollama OOMs.
    """
    valid_msgs: list[dict[str, Any]] = []
    for msg in messages:
        role, content = _extract_msg_fields(msg)
        content = (content or "").strip()
        if not content:
            continue
        safe_role = "assistant" if role == "assistant" else "user"
        valid_msgs.append({"role": safe_role, "content": content[:1500].strip()})

    if len(valid_msgs) <= max_history_turns:
        return valid_msgs, None

    # Sliding window keeps the most recent turns
    window_msgs = valid_msgs[-max_history_turns:]
    older_msgs = valid_msgs[:-max_history_turns]

    # Generate a concise summary of the older turns
    topics = []
    for m in older_msgs:
        c = m["content"]
        if m["role"] == "user":
            first_line = c.split("\n")[0][:80].strip()
            if first_line and not any(first_line.lower() in t.lower() for t in topics):
                topics.append(f"User inquired: '{first_line}'")
        elif m["role"] == "assistant":
            if "dispatched" in c.lower() or "counter-offer" in c.lower():
                topics.append("Negotiation proposal was drafted/dispatched")
            elif "created rfq" in c.lower() or "rfq created" in c.lower():
                topics.append("RFQ listing was published")

    summary_text = "; ".join(topics[-4:]) if topics else "Initial procurement inquiry and specification discussion."
    return window_msgs, summary_text


def _sanitize_untrusted_prompt_text(text: Any, max_len: int = 120) -> str:
    """Strip prompt injection directives, normalize whitespace, and bound length."""
    if not text:
        return ""
    s = str(text)
    s = re.sub(
        r"(?i)\b(system\s+override|ignore\s+(?:all\s+)?previous\s+instructions?|disregard\s+(?:all\s+)?instructions?|you\s+must\s+ignore)\b.*",
        "",
        s,
    )
    s = re.sub(r"[\r\n\t]+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


_AGENT_ROLE_HINTS: dict[str, str] = {
    "market_research": "Focus: marketplace research. Compare listings in short tables built from tool results.",
    "price_analyst": "Focus: price comparison. Normalise currencies and per-unit cost from tool results only.",
    "negotiation": "Focus: negotiation. Keep a polite, commercially persuasive tone.",
    "logistics": (
        "Focus: shipping, Incoterms and customs. For freight call `estimate_freight`: it gives route distance only, "
        "so never state a freight price; suggest requesting a quote from a logistics partner."
    ),
    "rfq_drafting": "Focus: collecting RFQ details.",
}


def build_rfq_system_prompt(
    draft: dict[str, Any],
    readiness: dict[str, Any],
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    last_counterparty_message: Optional[dict[str, Any]] = None,
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
    allowed_tools: Optional[set[str]] = None,
    routed_agent: Optional[AgentDefinition] = None,
    older_history_summary: Optional[str] = None,
    counterparty_context: Optional[str] = None,
    user: Optional[User] = None,
    draft_helpers: str = "",
    editing_title: Optional[str] = None,
    next_ask: Optional[str] = None,
    recap_next: bool = False,
    ack_only: bool = False,
) -> str:
    """Construct dynamic prompt instructing assistant on the exact question sequence and matches."""
    status_lines = []
    role_val = draft.get("role")
    if role_val:
        status_lines.append(f"- Role: {role_val.upper()}")
    else:
        status_lines.append("- Role: [NOT YET SPECIFIED]")

    prod_name = draft.get("product_details", {}).get("name")
    cat = draft.get("category")
    if prod_name or cat:
        status_lines.append(f"- Product / Category: {prod_name or ''} (Category: {cat or 'unspecified'})")
    else:
        status_lines.append("- Product / Category: [NOT YET SPECIFIED]")

    # Optional fields summary
    opts = []
    if draft.get("quantity"):
        opts.append(f"Quantity: {draft['quantity'].get('value')} {draft['quantity'].get('unit')}")
    if draft.get("price_target"):
        opts.append(f"Target Price: {draft['price_target'].get('amount')} {draft['price_target'].get('currency')}")
    if draft.get("location"):
        loc_parts = [draft["location"].get(k) for k in ("city", "state", "country") if draft["location"].get(k)]
        if loc_parts:
            opts.append("Location: " + ", ".join(loc_parts))
    if draft.get("deadline"):
        opts.append(f"Deadline: within {draft['deadline'].get('in_days')} days")

    if opts:
        status_lines.append("- Optional details captured: " + "; ".join(opts))

    if allowed_tools is not None and "update_rfq_draft" not in allowed_tools and not draft.get("role") and not draft.get("product_details", {}).get("name") and not draft.get("category"):
        status_str = "- No active RFQ being drafted (Market Analysis / Advisory Inquiry)"
    else:
        status_str = "\n".join(status_lines)
    if editing_title:
        status_str = (
            f'- EDITING the saved RFQ "{editing_title}". Apply the user\'s changes to this draft; the role cannot be '
            'changed. The changes are stored only when the user says "save changes" (or "cancel edit" to discard). '
            "Never create a new RFQ and never claim the changes are saved before then.\n"
        ) + status_str

    missing_opts = []
    if not draft.get("quantity"):
        missing_opts.append("quantity")
    if not draft.get("price_target"):
        missing_opts.append("target budget/price")
    if not draft.get("location") or not (draft.get("location") or {}).get("city"):
        missing_opts.append("delivery location")
    if not draft.get("deadline"):
        missing_opts.append("delivery deadline")

    # A short role hint only. Correct behaviour comes from the tools and the verifier, not from this text.
    drafting = allowed_tools is None or "update_rfq_draft" in allowed_tools
    agent_id = routed_agent.id if routed_agent else "general"
    if agent_id == "verification":
        kyc_val = "unverified"
        if user:
            prof = getattr(user, "profile", None)
            if prof and getattr(prof, "kyc_status", None):
                raw_k = getattr(prof.kyc_status, "value", str(prof.kyc_status))
                kyc_val = raw_k.lower()
        role_hint = f"Focus: counterparty trust and verification. The user's own KYC status is '{kyc_val}'."
    else:
        role_hint = _AGENT_ROLE_HINTS.get(agent_id, "")
    captured = "; ".join(opts) if opts else "none yet"
    if drafting and ack_only:
        flow_directive = (
            f"RFQ DRAFT: Captured so far: {captured}. Reply with ONE short sentence acknowledging what the user just told you. "
            "Do NOT ask any question, do NOT list or suggest specs, options or numbers, do NOT say the draft was saved, "
            "created or updated (it is only a private draft until the user confirms), and do not offer to create the RFQ: "
            "the next question or the summary is added automatically after your reply."
        )
    elif drafting and recap_next:
        flow_directive = (
            f"RFQ DRAFT: all the questions are done. Captured: {captured}. Acknowledge what was captured in one short "
            "sentence. Do NOT ask any question and do not offer to create the RFQ: a summary with the publish-or-draft "
            "question is added automatically after your reply."
        )
    elif drafting and next_ask:
        flow_directive = (
            f"RFQ DRAFT: Captured so far: {captured}. Acknowledge what was captured in one short sentence, then "
            f"ask only this: \"{next_ask}\" Do not ask about any other field and do not offer to create the RFQ yet."
        )
    elif drafting and not readiness["has_role"]:
        flow_directive = "RFQ DRAFT: role is missing. Ask in ONE brief sentence whether the user wants to BUY or SELL goods."
    elif drafting and not readiness["has_product"]:
        flow_directive = (
            f"RFQ DRAFT: role is {str(draft.get('role', '')).upper()}. Ask which specific PRODUCT they want to buy/sell. Keep it short."
        )
    elif drafting and missing_opts:
        flow_directive = (
            f"RFQ DRAFT: role and product are recorded. Captured: {'; '.join(opts) if opts else 'none yet'}. "
            f"Still missing: {', '.join(missing_opts)}. Acknowledge what was captured and ask if they want to add the rest, or create the RFQ."
        )
    elif drafting:
        flow_directive = (
            "RFQ DRAFT: all key details are captured. Acknowledge the draft concisely and ask about technical "
            "specifications or whether they are ready to create the RFQ."
        )
    else:
        flow_directive = (
            "Answer the user's question using only numbers returned by the data tools. "
            "Do not ask whether they want to buy or sell, and do not offer to create an RFQ, unless they ask for it."
        )
    if role_hint:
        flow_directive = f"{role_hint}\n{flow_directive}"

    matches_section = ""
    if matched_candidates:
        match_lines = [
            f"\nCURRENT LOADED MATCHED COUNTERPARTIES ({len(matched_candidates[:10])} candidates displayed in user's matching cart):"
        ]
        for idx, c in enumerate(matched_candidates[:10]):
            rank = c.get("rank") or (idx + 1)
            cp = c.get("counterparty") if isinstance(c.get("counterparty"), dict) else {}
            company = _sanitize_untrusted_prompt_text(
                cp.get("company_name")
                or cp.get("name")
                or c.get("company_name")
                or c.get("name")
                or f"Supplier/Buyer #{rank}",
                max_len=80,
            )
            email = _sanitize_untrusted_prompt_text(
                cp.get("email") or c.get("email") or "Not provided",
                max_len=60,
            )
            phone = _sanitize_untrusted_prompt_text(
                cp.get("phone") or c.get("phone") or "Not provided",
                max_len=40,
            )

            # Score extraction
            score_data = c.get("score")
            if isinstance(score_data, dict):
                total_score = score_data.get("total")
            elif isinstance(score_data, (int, float)):
                total_score = score_data
            else:
                total_score = c.get("match_score") or c.get("total_score")
            total_pct = f"{round(total_score * 100)}%" if total_score is not None else "N/A"
            if total_score is None:
                tier = "not scored; do not invent a score"
            else:
                tier = "Prime Match" if total_score >= 0.85 else ("Strong Fit" if total_score >= 0.7 else "Viable")

            # Price extraction
            price_val = c.get("price")
            if isinstance(price_val, dict):
                price_amt = price_val.get("amount")
                price_curr = price_val.get("currency", "INR")
                price_unit = f"/{price_val.get('per_unit')}" if price_val.get("per_unit") else ""
                price_str = f"{price_amt} {price_curr}{price_unit}" if price_amt is not None else "N/A"
            elif isinstance(price_val, (int, float)):
                price_str = f"₹{price_val:,.2f}" if price_val else "N/A"
            elif price_val:
                price_str = str(price_val)
            else:
                price_str = "N/A"

            # Quantity extraction
            qty_val = c.get("quantity")
            if isinstance(qty_val, dict):
                qty_str = f"{qty_val.get('value')} {qty_val.get('unit', '')}".strip() if qty_val.get("value") is not None else "N/A"
            elif isinstance(qty_val, (int, float)):
                unit = c.get("quantity_unit") or c.get("unit") or ""
                qty_str = f"{qty_val} {unit}".strip()
            elif qty_val:
                qty_str = str(qty_val)
            else:
                qty_str = "N/A"

            # Location extraction
            loc_val = c.get("location")
            if isinstance(loc_val, dict):
                loc_parts = [loc_val.get(k) for k in ("city", "state", "country") if loc_val.get(k)]
                loc_str = ", ".join(loc_parts) or "N/A"
            elif isinstance(loc_val, str) and loc_val.strip():
                loc_str = loc_val.strip()
            else:
                loc_parts = [c.get(k) or cp.get(k) for k in ("city", "state", "country") if (c.get(k) or cp.get(k))]
                loc_str = ", ".join(loc_parts) or "N/A"

            dist_val = c.get("distance_km")
            dist = f"{round(dist_val)} km" if isinstance(dist_val, (int, float)) else "N/A"
            gst_verified = cp.get("gst_verified") or c.get("verified") or ("GST Verified" in (c.get("badges") or []))
            gst = "GST Verified" if gst_verified else "Standard"

            rating = cp.get("average_rating") or c.get("rating")
            rating_str = f"{rating:.1f}★" if isinstance(rating, (int, float)) else "New Trader"
            title = _sanitize_untrusted_prompt_text(c.get("title") or company, max_len=120)

            rfq_id = str(c.get("rfq_id") or c.get("id") or "").strip()
            rfq_link = f"/marketplace?rfq={rfq_id}" if rfq_id else "/marketplace"

            match_lines.append(
                f"- Candidate #{rank}: Company '{company}' | Title: '{title}' | Match Score: {total_pct} ({tier}) | "
                f"RFQ ID: '{rfq_id}' | RFQ Link: '{rfq_link}' | "
                f"Email: {email} | Phone: {phone} | Price: {price_str} | Quantity: {qty_str} | "
                f"Location: {loc_str} (Distance: {dist}) | Rating: {rating_str} | Status: {gst}"
            )

        match_lines.extend([
            "",
            "MATCH QUERY DIRECTIVES & MANDATORY RFQ LINK RULES:",
            "- The user is inquiring about or reviewing matched counterparties and RFQ listings.",
            "- MANDATORY REQUIREMENT: Whenever you display, list, recommend, or mention ANY counterparty or RFQ, you MUST ALWAYS provide their direct markdown RFQ link: `[View RFQ & Connect](/marketplace?rfq={rfq_id})` (or `[{company} RFQ](/marketplace?rfq={rfq_id})`).",
            "- Explicitly tell the user: 'Click the link to view the complete RFQ details and send a connection request directly.'",
            "- When the user asks for emails (e.g. 'give me mail id of top three', 'show emails', 'contact details'):",
            "  * Provide a clear list of the requested candidates (by rank and company name), their exact email addresses, AND their RFQ link `[View RFQ & Connect](/marketplace?rfq={rfq_id})` so the user can send a connection request.",
            "  * If an email is 'Not provided', state that transparently while still providing their RFQ link.",
            "- When the user asks which candidate suits best (e.g. 'which one suits best from this top three', 'who is the best match'):",
            "  * Compare the top candidates on match score %, price competitiveness, location proximity, and trust credentials (GST verification, rating).",
            "  * Give a clear, decisive recommendation for the candidate that offers the best balance of quality, pricing, and suitability, explaining the exact reasons.",
            "  * INCLUDE the direct link to the recommended RFQ: e.g. `[View RFQ & Connect](/marketplace?rfq={rfq_id})` so the user can send a connection request immediately.",
            "- When the user asks to compare (e.g. 'compare prices', 'compare ratings'): provide a concise comparison breakdown, including each candidate's RFQ link.",
            "- DISTANCE AND PROXIMITY INTEGRITY:",
            "  * Always quote the EXACT distance provided in candidate metadata.",
            "  * Never alter numbers or add zeros; if no distance is given, say it is not available.",
            "- DO NOT call `update_rfq_draft` or `create_rfq` when answering questions about matched counterparties.",
        ])
        matches_section = "\n".join(match_lines)

    active_thread_section = ""
    if last_counterparty_message or active_connection_id:
        recipient_hint = (last_counterparty_message or {}).get("counterparty_name") or "the counterparty"
        last_msg_text = (last_counterparty_message or {}).get("message") or ""
        active_thread_section = (
            f"\nACTIVE COUNTERPARTY NEGOTIATION THREAD ({recipient_hint}):\n"
            f"- A previous message was dispatched to {recipient_hint}:\n"
            f"  \"{last_msg_text}\"\n"
            "- CRITICAL RULES FOR FOLLOW-UPS & AMENDMENTS:\n"
            "  * When the user gives additions or amendments (e.g. 'also mention that...', 'aslo mantion that the location is not indore its 10kg away', 'tell them we can pay cash', 'and add that...'):\n"
            "    1. You MUST call the `draft_counterparty_message` tool to draft the updated/amended message for the counterparty!\n"
            "    2. NEVER call `update_rfq_draft` with amendment text phrases (e.g. NEVER set product to 'aslo mantion location')!\n"
            "    3. In your amended message, preserve the polite quotation context and accurately integrate the user's specific addition or correction without hallucinating unrelated distant cities or terms.\n"
            "    4. Do not invent cities like Nagpur when user only specifies a distance from Indore; state '10 km outside Indore'.\n"
        )

    negotiation_directive = (
        "\nNEGOTIATION & COUNTERPARTY MESSAGING DIRECTIVES:\n"
        "- When the user instructs to send a message to a seller or negotiate (e.g. 'send negotiation message to the counter user', 'message the supplier', 'ask for discount', 'negotiate with Himalaya Orchards'):\n"
        "  * ALWAYS adopt an exceptionally polite, courteous, and commercially persuasive tone on behalf of the user.\n"
        "  * Formulate a complete, polite business communication stating appreciation for their quotation, referencing the order details (quantity, delivery location), and politely requesting the specific adjustment (the price change the user asked for).\n"
        "  * Call the `draft_counterparty_message` tool with `message`, `recipient_name`, and optional `proposed_price` / `proposed_currency`.\n"
        "  * IMPORTANT: The message is drafted for user review. Inform the user that you have drafted the negotiation proposal for their review, and invite them to click the Send button on the card to dispatch it.\n"
        "- When the user provides follow-up additions, notes, or amendments (e.g. 'also mention that...', 'tell them that...', 'aslo mantion that the location is not indore its 10kg away'):\n"
        "  * You MUST call the `draft_counterparty_message` tool with the amended/updated message.\n"
        "  * Do NOT call `update_rfq_draft` or update the product name with conversational directives.\n"
        "  * The `message` argument in `draft_counterparty_message` MUST contain the full and complete letter from start to finish. Never truncate the tool argument mid-sentence.\n"
    ) + active_thread_section

    if allowed_tools is not None and "draft_counterparty_message" not in allowed_tools:
        negotiation_directive = ""

    prompt_parts = [
        SYSTEM_BUSINESS_PROMPT,
        f"\nACTIVE PROCUREMENT STATE SCRATCHPAD:\n{status_str}",
    ]
    if draft_helpers and (allowed_tools is None or "update_rfq_draft" in allowed_tools or "create_rfq" in allowed_tools):
        prompt_parts.append(draft_helpers)
    if older_history_summary:
        prompt_parts.append(f"\nCONVERSATION HISTORICAL CONTEXT (Pruned older turns):\n- {older_history_summary}")
    if matches_section:
        prompt_parts.append(matches_section)
    if counterparty_context:
        prompt_parts.append(counterparty_context)
    if negotiation_directive:
        prompt_parts.append(negotiation_directive)

    prompt_parts.append(f"\nSPECIALIST DIRECTIVE & FLOW:\n{flow_directive}")

    rule_lines = ["\nIMPORTANT OPERATIONAL RULES:"]
    tool_rules = {
        "update_rfq_draft": "- Call `update_rfq_draft(**kwargs)` when procurement specifications or details are provided.",
        "create_rfq": "- Call `create_rfq(...)` only because the user asked to create or confirmed it, and Role and Product are known.",
        "draft_counterparty_message": "- Call `draft_counterparty_message(...)` to compose the negotiation message the user asked for.",
        "close_rfq": "- Call `close_rfq(...)` only because the user asked to close or cancel their RFQ.",
    }
    for name, line in tool_rules.items():
        if allowed_tools is None or name in allowed_tools:
            rule_lines.append(line)
    if allowed_tools is not None:
        rule_lines.append("- Actions not listed above are not available this turn; never claim to have performed them.")
    rule_lines.append("- Keep responses concise, data-rich, and directly actionable.")
    rules = "\n".join(rule_lines)
    prompt_parts.append(rules)
    return "\n".join(prompt_parts)


def _quantity_was_given(value: Any, user_text: str) -> bool:
    """True when a quantity figure appears in what the user actually wrote."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    digits = re.sub(r"[,\s]", "", user_text or "")
    candidates = {f"{number:g}", f"{int(number)}" if number.is_integer() else f"{number:g}"}
    return any(re.search(rf"(?<![\d.]){re.escape(c)}(?![\d])", digits) for c in candidates)


def _process_tool_call(
    tool_call: dict[str, Any],
    current_draft: dict[str, Any],
    locked_role: Optional[str] = None,
    user_text: Optional[str] = None,
    notes: Optional[list[str]] = None,
    last_user_text: Optional[str] = None,
    stated_product: Optional[str] = None,
) -> dict[str, Any]:
    """Execute update_rfq_draft tool call arguments into the draft dictionary.

    ``locked_role`` is the role the user stated or already had; the model may not
    flip it (it has turned "draft the rfq for supply ..." into a BUYER draft).
    With ``user_text`` (everything the USER wrote in this conversation), a quantity the user
    never wrote (the model's "1 tonne" default) is dropped, and so is every spec the user's
    words do not support ("Class I", "Brix 14" the model made up). The model never sets
    ``status``, and its own ``description`` is kept only when the user asked for one
    (``last_user_text`` is the message of this turn). The product is renamed only when that message
    states a product (``stated_product``): "continue refining it" is not the product "Continue Refinig".
    """
    fn = tool_call.get("function") or {}
    name = fn.get("name")
    if name == "update_rfq_draft":
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if isinstance(args, dict):
            args = dict(args)
            if locked_role and args.get("role") and str(args["role"]).lower() != locked_role:
                logger.info("Ignoring model role change %s -> %s", locked_role, args["role"])
                args["role"] = locked_role
            if user_text is not None and args.get("quantity_value") is not None and not _quantity_was_given(args["quantity_value"], user_text):
                logger.info("Ignoring model quantity %s not given by the user", args["quantity_value"])
                args.pop("quantity_value", None)
                args.pop("quantity_unit", None)
            args.pop("status", None)  # status comes from the confirmation flow only
            if args.get("price_per_unit") is not None:
                args["price_per_unit"] = chat_specs.normalize_per_unit(args["price_per_unit"])
            if last_user_text is not None:
                existing = chat_draft_assist.product_name(current_draft)
                def renamed_ok(new_name: Any) -> bool:
                    ok = chat_specs.rename_allowed(str(new_name), existing, last_user_text, stated_product)
                    if not ok:
                        logger.info(
                            "Rejected product rename %r -> %r: %r states no such product", existing, new_name, last_user_text[:60]
                        )
                    return ok

                if args.get("product_name") and not renamed_ok(args["product_name"]):
                    args.pop("product_name")
                pd = args.get("product_details")
                if isinstance(pd, dict) and pd.get("name") and not renamed_ok(pd["name"]):
                    args["product_details"] = {k: v for k, v in pd.items() if k != "name"}
            if user_text is not None and isinstance(args.get("product_details"), dict):
                flat = chat_specs.flatten_details(args["product_details"])
                kept, dropped = chat_specs.ground_product_details(flat, user_text)
                if dropped:
                    logger.info("dropped ungrounded spec(s) from the model's update_rfq_draft: %s", dropped)
                args["product_details"] = kept
            if args.get("description") is not None and last_user_text is not None:
                if not chat_specs.description_allowed(args["description"], last_user_text):
                    logger.info("dropped model-written description (the user did not ask for one)")
                    args.pop("description", None)
            if notes is not None:
                args["_notes"] = notes
            return rfq_tool_service.update_rfq_draft(existing_draft=current_draft, **args)
    return current_draft


def _draft_guards(
    messages: list[Any], last_user_content: str, draft: dict[str, Any], earlier_user_text: str = ""
) -> dict[str, Any]:
    """Arguments for _process_tool_call that keep the model's edits honest."""
    from services import query_extractor

    stated = stated_role(last_user_content, bool(draft.get("role")))
    user_text = " ".join(c for r, c in (_extract_msg_fields(m) for m in messages) if r == "user")
    if earlier_user_text:
        user_text = f"{earlier_user_text} {user_text}"
    from services import ai_query_parser

    parse = ai_query_parser.cached(last_user_content)
    return {
        "locked_role": stated or draft.get("role"),
        "user_text": user_text,
        "last_user_text": last_user_content,
        "stated_product": message_product(
            parse.product if parse else None, last_user_content, query_extractor.extract(last_user_content).product
        ),
    }


async def execute_create_rfq_call(
    draft: dict[str, Any],
    tool_args: dict[str, Any],
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    assist: Optional[chat_draft_assist.DraftAssist] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Execute create_rfq tool call, validating role and submitting to database."""
    readiness = rfq_tool_service.evaluate_rfq_readiness(draft)
    if not readiness["has_role"] or not readiness["has_product"]:
        return None, "Role (buyer/seller) and Product name are required before creating an RFQ."

    if assist is not None:
        # The form blocks submit on an unsafe listing; so does chat (rfq_service still moderates server-side).
        blocked = await assist.block_reason(draft)
        if blocked:
            return None, blocked

    title = tool_args.get("title")
    status = tool_args.get("status", "active")
    notes = tool_args.get("notes")

    try:
        payload = rfq_tool_service.build_rfq_create_payload(
            draft=draft,
            title=title,
            status=status,
            notes=notes,
        )
    except rfq_tool_service.RFQDraftInvalid as exc:
        problems = "; ".join(f"{f}: {m}" for f, m in exc.errors)
        return None, f"I couldn't create the RFQ yet — please fix: {problems}"

    if db is not None and user is not None:
        if not user.can_post_as(payload.role):
            return None, (
                f"Your account is registered as '{user.role.value}' and cannot post a '{payload.role.value}' RFQ. "
                f"Please update your account role or post as a {user.role.value}."
            )
        try:
            from services import rfq_service
            rfq = await rfq_service.create_rfq(db, user, payload)
            return {
                "id": str(rfq.id),
                "title": rfq.title,
                "role": rfq.role.value,
                "status": rfq.status.value,
                "category": rfq.category,
            }, None
        except rfq_service.DuplicateRFQError as exc:
            # Never claim a new listing: point at the one that already exists.
            return None, f"You already have this RFQ: {exc.existing_title} (id {exc.existing_id}). No new RFQ was created."
        except Exception as exc:
            logger.exception("Failed to create RFQ in database: %s", exc)
            return None, f"Failed to create RFQ: {str(exc)}"
    else:
        # Fallback simulated creation (for tests without db session)
        simulated_id = str(uuid.uuid4())
        return {
            "id": simulated_id,
            "title": payload.title,
            "role": payload.role.value,
            "status": payload.status.value,
            "category": payload.category,
        }, None


async def _resolve_user_rfq(
    db: AsyncSession, user: User, ref: Any, statuses: tuple[Any, ...]
) -> Optional[Any]:
    """The RFQ a user's command names: by id (any owner, so callers can refuse with a clear message), else
    the newest of the user's own RFQs in ``statuses`` whose title, category or product name contains ``ref``."""
    from models.rfq import RFQ

    search_term = str(ref or "").strip()
    if not search_term:
        return None
    try:
        found = await db.get(RFQ, uuid.UUID(search_term))
    except Exception:
        found = None
    if found is not None:
        return found
    stmt = (
        select(RFQ)
        .where(
            RFQ.user_id == user.id,
            RFQ.status.in_(list(statuses)),
            or_(
                RFQ.title.ilike(f"%{search_term}%"),
                RFQ.category.ilike(f"%{search_term}%"),
                text("rfqs.product_details->>'name' ILIKE :search_term"),
            ),
        )
        .params(search_term=f"%{search_term}%")
        .order_by(RFQ.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def execute_close_rfq_call(
    rfq_id_str: Optional[str] = None,
    conversation: Optional[Conversation] = None,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    require_unambiguous: bool = False,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Close an active RFQ in the database.

    With ``require_unambiguous`` (a typed command that names no listing), the
    fallback to "the newest active RFQ" is only taken when the user has exactly
    one; otherwise nothing is closed and the user is asked which one.
    """
    if db is None or user is None:
        return None, "Database session not available."

    from models.rfq import RFQ
    from models.enums import RFQStatus
    from schemas.rfq import RFQUpdate
    from services import rfq_service

    target_rfq = None
    if rfq_id_str:
        target_rfq = await _resolve_user_rfq(db, user, rfq_id_str, (RFQStatus.ACTIVE,))
        if not target_rfq:
            return None, f"No active RFQ matching '{rfq_id_str}' was found in your listings."

    if not target_rfq and conversation and conversation.rfq_id:
        target_rfq = await db.get(RFQ, conversation.rfq_id)

    if not target_rfq and conversation and conversation.state:
        last_rfq_info = conversation.state.get("last_created_rfq")
        if last_rfq_info and last_rfq_info.get("id"):
            try:
                target_rfq = await db.get(RFQ, uuid.UUID(str(last_rfq_info["id"])))
            except Exception:
                pass

    if not target_rfq:
        recent_stmt = (
            select(RFQ)
            .where(RFQ.user_id == user.id, RFQ.status == RFQStatus.ACTIVE)
            .order_by(RFQ.created_at.desc())
            .limit(5 if require_unambiguous else 1)
        )
        candidates = (await db.execute(recent_stmt)).scalars().all()
        if require_unambiguous and len(candidates) > 1:
            titles = "; ".join(f"'{c.title}'" for c in candidates)
            return None, f"You have several active RFQs ({titles}). Tell me which one to close, for example \"close my cotton RFQ\"."
        target_rfq = candidates[0] if candidates else None

    if not target_rfq:
        return None, "No active RFQ was found to close. You can view all your listings under My RFQs."

    if target_rfq.user_id != user.id:
        return None, "You do not have permission to close this RFQ."

    try:
        closed = await rfq_service.update_rfq(db, target_rfq, RFQUpdate(status=RFQStatus.CLOSED))
        if conversation:
            state = dict(conversation.state or {})
            if "last_created_rfq" in state:
                state["last_created_rfq"]["status"] = "closed"
            conversation.state = state
            await db.commit()

        return {
            "id": str(closed.id),
            "title": closed.title,
            "role": closed.role.value,
            "status": "closed",
            "category": closed.category,
        }, None
    except Exception as exc:
        logger.exception("Failed to close RFQ from chat: %s", exc)
        return None, f"Failed to close RFQ: {str(exc)}"


async def execute_publish_rfq_call(
    rfq_id_str: Optional[str] = None,
    conversation: Optional[Conversation] = None,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Publish one of the user's draft RFQs (the form's "Publish now").

    Which RFQ: the one named by id or keyword; else the draft this conversation created last;
    else the user's only draft; otherwise nothing is published and the user is asked which one.
    """
    if db is None or user is None:
        return None, "Database session not available."

    from models.rfq import RFQ
    from models.enums import RFQStatus
    from services import rfq_service

    target = None
    if rfq_id_str:
        target = await _resolve_user_rfq(db, user, rfq_id_str, (RFQStatus.DRAFT,))
        if target is None:
            return None, f"No draft RFQ matching '{rfq_id_str}' was found in your listings."
    else:
        last_id = (conversation.state or {}).get("last_created_rfq_id") if conversation else None
        if last_id:
            try:
                last = await db.get(RFQ, uuid.UUID(str(last_id)))
            except Exception:
                last = None
            if last is not None and last.user_id == user.id and last.status == RFQStatus.DRAFT:
                target = last
        if target is None:
            drafts = (
                await db.execute(
                    select(RFQ)
                    .where(RFQ.user_id == user.id, RFQ.status == RFQStatus.DRAFT)
                    .order_by(RFQ.created_at.desc())
                    .limit(5)
                )
            ).scalars().all()
            if not drafts:
                return None, "You have no draft RFQs to publish."
            if len(drafts) > 1:
                titles = "; ".join(f"'{d.title}'" for d in drafts)
                return None, f"You have several draft RFQs ({titles}). Tell me which one to publish, for example \"publish my rice RFQ\"."
            target = drafts[0]

    if target.user_id != user.id:
        return None, "You do not have permission to publish this RFQ."
    if target.status == RFQStatus.ACTIVE:
        return None, f"'{target.title}' is already published."
    try:
        published = await rfq_service.publish_rfq(db, target)
    except rfq_service.RFQError as exc:
        return None, f"Could not publish '{target.title}': {exc}"
    except Exception as exc:
        logger.exception("Failed to publish RFQ from chat: %s", exc)
        return None, f"Failed to publish RFQ: {str(exc)}"
    return {
        "id": str(published.id),
        "title": published.title,
        "role": published.role.value,
        "status": published.status.value,
        "category": published.category,
    }, None


async def execute_save_rfq_edit_call(
    draft: dict[str, Any],
    snapshot: dict[str, Any],
    rfq_id_str: str,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    assist: Optional[chat_draft_assist.DraftAssist] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str], bool]:
    """Save the chat's edits to the RFQ being edited: ``(rfq, error, changed)``.

    Only fields that differ from the loaded RFQ are sent, as the form's PATCH does. Nothing is
    sent (``changed`` False) when the draft equals what was loaded.
    """
    if db is None or user is None:
        return None, "Database session not available.", False

    from models.rfq import RFQ
    from services import rfq_service

    try:
        target = await db.get(RFQ, uuid.UUID(str(rfq_id_str)))
    except Exception:
        target = None
    if target is None:
        return None, "The RFQ you were editing could not be found.", False
    if target.user_id != user.id:
        return None, "You do not have permission to edit this RFQ.", False

    try:
        patch = chat_rfq_edit.build_rfq_patch(draft, snapshot)
    except rfq_tool_service.RFQDraftInvalid as exc:
        problems = "; ".join(f"{f}: {m}" for f, m in exc.errors)
        return None, f"I couldn't save the changes yet — please fix: {problems}", True
    if patch is None:
        return None, None, False
    if assist is not None:
        blocked = await assist.block_reason(draft)
        if blocked:
            return None, blocked, True
    try:
        updated = await rfq_service.update_rfq(db, target, patch)
    except rfq_service.RFQError as exc:
        return None, f"Could not save the changes to '{target.title}': {exc}", True
    except Exception as exc:
        logger.exception("Failed to update RFQ from chat: %s", exc)
        return None, f"Failed to update RFQ: {str(exc)}", True
    return {
        "id": str(updated.id),
        "title": updated.title,
        "role": updated.role.value,
        "status": updated.status.value,
        "category": updated.category,
    }, None, True


def _connection_relevance(
    conn: Any, user_id: uuid.UUID, hint: dict[str, Any], recipient: str
) -> tuple[bool, bool, bool, datetime]:
    """Sort key for choosing which existing connection "this buyer" refers to."""
    other = conn.receiver if conn.sender_id == user_id else conn.sender
    prof = getattr(other, "profile", None)
    other_name = (getattr(prof, "company_name", None) or getattr(prof, "name", None) or "").lower()
    name_match = bool(recipient and other_name and (recipient in other_name or other_name in recipient))

    rfq = getattr(conn, "rfq", None)
    side_match = product_match = False
    if rfq is not None:
        rfq_role = rfq.role.value if hasattr(rfq.role, "value") else str(rfq.role)
        # The listing is the counterparty's unless the user owns it.
        if rfq.user_id == user_id:
            counterparty_side = "seller" if rfq_role == "buyer" else "buyer"
        else:
            counterparty_side = rfq_role
        side_match = bool(hint.get("side") and counterparty_side == hint["side"])
        product = (hint.get("product") or "").lower()
        title = (rfq.title or "").lower()
        product_match = bool(product and all(w in title for w in product.split() if len(w) >= 3))

    updated = conn.updated_at or datetime.min.replace(tzinfo=timezone.utc)
    return (name_match, side_match, product_match, updated)


def _connection_hint(message: str, focus: dict[str, Any]) -> dict[str, Any]:
    side = _requested_listing_side(message) or focus.get("listing_role")
    return {"side": side, "product": focus.get("product")}


def _describe_listing(rfq: Any) -> str:
    parts = [f"'{rfq.title}'"]
    role = rfq.role.value if hasattr(rfq.role, "value") else str(rfq.role)
    parts.append(f"{role} listing")
    if rfq.quantity_value is not None:
        parts.append(f"quantity {float(rfq.quantity_value):g} {rfq.quantity_unit or ''}".strip())
    if rfq.price_amount is not None:
        parts.append(f"price {rfq.price_currency} {float(rfq.price_amount):,.2f}/{rfq.price_per_unit or 'unit'}")
    loc = ", ".join(x for x in (rfq.location_city, rfq.location_state, rfq.location_country) if x)
    if loc:
        parts.append(loc)
    return " | ".join(parts)


async def _resolve_counterparty_context(
    db: AsyncSession,
    user: User,
    message: str,
    focus: dict[str, Any],
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Find the connection a message is meant for and describe it for the prompt.

    Without this the model writes "Dear [Buyer's Name]" and quotes the user's own
    draft as if it were the buyer's RFQ. Returns (prompt section, connection id).
    """
    from models.connection import Connection
    from services import connection_service

    try:
        conns = (
            await db.execute(
                select(Connection)
                .options(*connection_service._FULL)
                .where(or_(Connection.sender_id == user.id, Connection.receiver_id == user.id))
                .order_by(Connection.updated_at.desc())
            )
        ).scalars().all()
    except Exception as exc:
        logger.warning("Could not load connections for counterparty context: %s", exc)
        return None, None
    if not conns:
        return None, None

    conn = next((c for c in conns if active_connection_id and str(c.id) == str(active_connection_id)), None)
    if conn is None:
        hint = _connection_hint(message, focus)
        ranked = max(conns, key=lambda c: _connection_relevance(c, user.id, hint, ""))
        name_match, side_match, product_match, _ = _connection_relevance(ranked, user.id, hint, "")
        # Only guess when something ties the connection to this request.
        if name_match or side_match or product_match or len(conns) == 1:
            conn = ranked
    if conn is None:
        return None, None

    other = conn.receiver if conn.sender_id == user.id else conn.sender
    other_prof = getattr(other, "profile", None)
    other_company = getattr(other_prof, "company_name", None) or getattr(other_prof, "name", None) or "the counterparty"
    my_prof = getattr(user, "profile", None)
    my_company = getattr(my_prof, "company_name", None) or getattr(my_prof, "name", None)
    status = conn.status.value if hasattr(conn.status, "value") else str(conn.status)

    lines = [
        "\nTARGET COUNTERPARTY FOR THIS MESSAGE (resolved from the user's marketplace connections):",
        f"- Company: {other_company} (connection status: {status})",
    ]
    rfq = getattr(conn, "rfq", None)
    if rfq is not None:
        owner = "the user's own" if rfq.user_id == user.id else f"{other_company}'s"
        lines.append(f"- Connected on {owner} listing: {_describe_listing(rfq)}")
    if my_company:
        lines.append(f"- The user's company (sign the message as): {my_company}")
    lines.extend([
        "RULES FOR THE MESSAGE:",
        f"- Address {other_company} by name and sign as {my_company or 'the user'}. Never leave placeholders such as [Buyer's Name] or [Your Company Name].",
        "- Quote the counterparty's requirements only from their listing above. Figures in the user's own RFQ draft are the user's offer, not the counterparty's request.",
        "- Call `draft_counterparty_message` with the complete message.",
    ])
    return "\n".join(lines), str(conn.id)


async def execute_draft_counterparty_message_call(
    tool_args: dict[str, Any],
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
    connection_hint: Optional[dict[str, Any]] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Execute draft_counterparty_message tool call, preparing a negotiation draft for user confirmation.

    ``connection_hint`` ({"side", "product"}) picks the right existing connection
    when the user says "this buyer" without naming one.
    """
    message = tool_args.get("message")
    if not message or not str(message).strip():
        return None, "Message content is required to draft for the counterparty."
    message_content = str(message).strip()

    if db is not None and user is not None:
        try:
            from services import connection_service
            from models.connection import Connection
            from models.enums import ConnectionStatus

            conn = None
            target_conn_id = tool_args.get("connection_id") or active_connection_id
            if target_conn_id:
                try:
                    c_uuid = uuid.UUID(str(target_conn_id))
                    conn = await db.scalar(
                        select(Connection)
                        .options(*connection_service._FULL)
                        .where(
                            Connection.id == c_uuid,
                            or_(Connection.sender_id == user.id, Connection.receiver_id == user.id),
                        )
                    )
                except Exception:
                    conn = None
                if not conn:
                    return None, "Connection not found or you do not have permission to access it."

            # If no connection by ID, check matched_candidates
            if not conn and matched_candidates:
                recipient = (tool_args.get("recipient_name") or "").lower()
                target_rfq_id_str = tool_args.get("target_rfq_id")
                chosen_cand = None
                if target_rfq_id_str:
                    chosen_cand = next((c for c in matched_candidates if str(c.get("rfq_id")) == target_rfq_id_str), None)
                if not chosen_cand and recipient:
                    for cand in matched_candidates:
                        cp = cand.get("counterparty") or {}
                        c_name = (cp.get("company_name") or cp.get("name") or cand.get("company_name") or cand.get("title") or "").lower()
                        if recipient in c_name or c_name in recipient:
                            chosen_cand = cand
                            break
                if not chosen_cand and matched_candidates:
                    chosen_cand = matched_candidates[0]

                if chosen_cand and chosen_cand.get("rfq_id"):
                    target_rfq_uuid = uuid.UUID(str(chosen_cand["rfq_id"]))
                    conn = await db.scalar(
                        select(Connection)
                        .options(*connection_service._FULL)
                        .where(
                            or_(
                                and_(Connection.sender_id == user.id, Connection.rfq_id == target_rfq_uuid),
                                and_(Connection.receiver_id == user.id, Connection.rfq_id == target_rfq_uuid),
                            )
                        )
                    )
                    if not conn:
                        try:
                            created_conn = await connection_service.create_connection(db, user.id, target_rfq_uuid)
                            conn = await db.scalar(
                                select(Connection)
                                .options(*connection_service._FULL)
                                .where(Connection.id == created_conn.id)
                            )
                        except Exception as exc:
                            logger.warning("Could not auto-create connection: %s", exc)

            # If still no connection, check user's existing connections
            if not conn:
                res = await db.execute(
                    select(Connection)
                    .options(*connection_service._FULL)
                    .where(or_(Connection.sender_id == user.id, Connection.receiver_id == user.id))
                    .order_by(Connection.updated_at.desc())
                )
                conns = res.scalars().all()
                if conns:
                    recipient = (tool_args.get("recipient_name") or "").strip().lower()
                    conn = max(
                        conns,
                        key=lambda c: _connection_relevance(c, user.id, connection_hint or {}, recipient),
                    )

            if not conn:
                return None, "No active seller connection found to send the message. Please view matches or connect with a seller first."

            # Resolve counterparty name
            other_user = conn.receiver if conn.sender_id == user.id else conn.sender
            prof = getattr(other_user, "profile", None)
            company_name = (
                getattr(prof, "company_name", None)
                or getattr(prof, "contact_name", None)
                or getattr(other_user, "company_name", None)
                or getattr(other_user, "contact_name", None)
                or tool_args.get("recipient_name")
                or "Seller"
            )

            # Prepared as a draft for user confirmation - NOT sent directly
            return {
                "connection_id": str(conn.id),
                "counterparty_name": company_name,
                "message": message_content,
                "status": "draft",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }, None

        except Exception as exc:
            logger.exception("Failed to draft counterparty message: %s", exc)
            return None, f"Failed to draft counterparty message: {str(exc)}"
    else:
        # Fallback simulation (for tests without db session)
        sim_conn_id = str(active_connection_id or uuid.uuid4())
        c_name = tool_args.get("recipient_name") or "Seller"
        return {
            "connection_id": sim_conn_id,
            "counterparty_name": c_name,
            "message": message_content,
            "status": "draft",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, None


execute_send_counterparty_message_call = execute_draft_counterparty_message_call


def format_counterparty_message_text(result: dict[str, Any]) -> str:
    """Format confirmation message when a counterparty message is drafted or dispatched."""
    recipient = result.get("counterparty_name") or "Seller"
    msg_text = result.get("message") or ""
    status = result.get("status", "draft")
    if status == "draft":
        return (
            f"📝 **Negotiation Message Drafted for {recipient}**\n\n"
            f"> *\"{msg_text}\"*\n\n"
            f"Review the draft above and click the **Send to {recipient}** button on the card to dispatch it to their chat."
        )
    return (
        f"✉️ **Message Sent to {recipient}**\n\n"
        f"> *\"{msg_text}\"*\n\n"
        f"The message has been delivered directly into your active chat thread with {recipient}."
    )


async def mark_counterparty_message_sent(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    sent_message_id: Optional[str] = None,
) -> bool:
    """Mark a drafted counterparty message in an AI conversation as delivered/sent."""
    stmt = (
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    msg = await db.scalar(stmt)
    if not msg:
        return False
    meta = dict(msg.msg_metadata or {})
    cp = meta.get("counterparty_message")
    if cp:
        cp["status"] = "delivered"
        if sent_message_id:
            cp["message_id"] = str(sent_message_id)
        meta["counterparty_message"] = cp
        msg.msg_metadata = meta
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(msg, "msg_metadata")
        await db.commit()
        return True
    return False


def extract_or_compose_counterparty_message(
    reply_text: str,
    user_instruction: str,
    last_counterparty_info: Optional[dict[str, Any]] = None,
    draft: Optional[dict[str, Any]] = None,
) -> str:
    """Extract dispatched message from model output or compose a polite message from context."""
    if reply_text:
        # Check for quoted message text: *"Dear ... "* or "> Dear ..." or "Dear ..."
        m_quote = re.search(r'[\*"\']{1,2}(Dear\s+[\s\S]+?)[\*"\']{1,2}', reply_text, re.IGNORECASE)
        if m_quote:
            return m_quote.group(1).strip()

        # Check for blockquoted text > ...
        m_block = re.search(r'>\s*\*?"?(Dear\s+[\s\S]+?)"?\*?$', reply_text, re.MULTILINE | re.IGNORECASE)
        if m_block:
            return m_block.group(1).strip()

        # Check for paragraph starting with Dear ...
        m_dear = re.search(r'(Dear\s+[\s\S]+?(?:deal\.|deal!|regards,?|sincerely,?|faithfully,?|cooperation\.|thank you\.|\[[\w\s/]+\]))', reply_text, re.IGNORECASE)
        if m_dear:
            return m_dear.group(1).strip()

    # If previous message exists, weave the user's amendment
    prev_msg = (last_counterparty_info or {}).get("message")
    if prev_msg:
        # Check if user mentioned location update (e.g. location is not indore its 10kg away from indore)
        loc_m = re.search(
            r'(?:location|address)?\s*(?:is\s+)?not\s+([A-Za-z]+).*?(\d+)\s*(?:km|kg|kilometer|kilometers)?\s+away\s+from\s+([A-Za-z]+)',
            user_instruction,
            re.IGNORECASE,
        )
        if loc_m:
            not_city, dist, from_city = loc_m.group(1), loc_m.group(2), loc_m.group(3)
            clarification = f"Please note: The delivery location is approximately {dist} km outside {from_city.title()} rather than within {not_city.title()} city limits."
            return f"{prev_msg}\n\nClarification: {clarification}"

        cleaned_user = user_instruction.strip()
        cleaned_user = re.sub(r'^(aslo|also|and|please)?\s*(mention|mantion|add|tell them|say)\s+(that)?\s*', '', cleaned_user, flags=re.IGNORECASE)
        if cleaned_user:
            return f"{prev_msg}\n\nAdditional Note: {cleaned_user}"
        return prev_msg

    # Fallback polite template
    prod_name = (draft or {}).get("product_details", {}).get("name") or "the requested goods"
    return f"Dear Supplier,\n\nWe are following up regarding our requirement for {prod_name}. {user_instruction}\n\nWe look forward to your response.\n\nBest regards,"


def extract_recipient_name(
    reply_text: str,
    last_counterparty_info: Optional[dict[str, Any]] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Extract recipient counterparty name from model reply, last message, or candidate list."""
    if reply_text:
        m = re.search(r'(?:Message Sent to|Sent to|Dear|negotiation with)\s+([A-Za-z0-9\s&]+?)(?:\n|,|\*|"|\.|\s-\s)', reply_text, re.IGNORECASE)
        if m:
            cand = m.group(1).strip()
            if cand.lower() not in ("the", "your", "our", "all", "a", "an", "seller", "supplier"):
                return cand

    if last_counterparty_info and last_counterparty_info.get("counterparty_name"):
        return str(last_counterparty_info["counterparty_name"])

    if matched_candidates:
        first = matched_candidates[0]
        cp = first.get("counterparty") if isinstance(first.get("counterparty"), dict) else {}
        name = cp.get("company_name") or cp.get("name") or first.get("company_name") or first.get("name")
        if name:
            return str(name)

    return "Seller"


def is_truncated_message(msg: str) -> bool:
    """Check if message string was truncated mid-sentence or ends abruptly without proper punctuation."""
    if not msg or not str(msg).strip():
        return True
    t = str(msg).strip()
    trailing_words = {"within", "for", "and", "to", "at", "with", "in", "the", "that", "of", "by", "on", "a", "an", "is", "we", "our"}
    words = t.split()
    if words:
        last_word = re.sub(r"[^\w\s]", "", words[-1].lower())
        if last_word in trailing_words:
            return True
    if t[-1] not in (".", "!", "?", '"', "'", "*", "]", "\n"):
        return True
    return False


def _draft_location(city: str, stated_country: Optional[str] = None) -> dict[str, Any]:
    """City/state/country from the gazetteer when it knows the city; never an invented country."""
    from services import locations

    loc = locations.find_location(city) if city else None
    if loc is not None and loc.is_city:
        out: dict[str, Any] = {"city": loc.city or city}
        if loc.state:
            out["state"] = loc.state
        out["country"] = loc.country
        return out
    out = {"city": city}
    if stated_country:
        out["country"] = stated_country
    return out


def _published_text(published: dict[str, Any]) -> str:
    rfq_id = published.get("id", "")
    return (
        f"🎉 **RFQ Published!** '{published.get('title', 'RFQ')}' (ID: `{rfq_id}`) is now live in the Marketplace.\n\n"
        f"[View RFQ in Marketplace](/marketplace?rfq={rfq_id})"
    )


def format_created_rfq_text(created_rfq: dict[str, Any]) -> str:
    """Format celebratory message when an RFQ is created."""
    rfq_id = created_rfq.get("id", "")
    title = created_rfq.get("title", "RFQ")
    role = (created_rfq.get("role") or "BUYER").upper()
    status = (created_rfq.get("status") or "ACTIVE").upper()
    category = created_rfq.get("category", "General Goods")

    if status == "DRAFT":
        return (
            f"📝 **RFQ saved as a draft (not published).**\n\n"
            f"- **Title**: {title}\n"
            f"- **RFQ ID**: `{rfq_id}`\n"
            f"- **Market Side**: {role}\n"
            f"- **Category**: {category}\n"
            f"- **Status**: DRAFT (not visible in the Marketplace)\n\n"
            f"You can review and publish it later from your RFQs."
        )

    rfq_link_md = f"[View RFQ in Marketplace](/marketplace?rfq={rfq_id})" if rfq_id else "[View Marketplace](/marketplace)"

    return (
        f"🎉 **RFQ Successfully Created & Published!**\n\n"
        f"- **Title**: {title}\n"
        f"- **RFQ ID**: `{rfq_id}`\n"
        f"- **Market Side**: {role}\n"
        f"- **Category**: {category}\n"
        f"- **Status**: {status} (Active & Live in Marketplace)\n"
        f"- **Listing Link**: {rfq_link_md}\n\n"
        f"Your listing has been submitted to the marketplace and is active in the matching index. "
        f"Click the link above to view your listing, or click 'Show matching' below to find counterparties!"
    )


def build_rfq_link_appendix(matched_candidates: Optional[list[dict[str, Any]]]) -> str:
    """Generate markdown links for active counterparties/RFQs so the user can easily view and connect."""
    if not matched_candidates:
        return ""

    link_items = []
    for idx, c in enumerate(matched_candidates[:5]):
        rfq_id = str(c.get("rfq_id") or c.get("id") or "").strip()
        if not rfq_id:
            continue
        cp = c.get("counterparty") if isinstance(c.get("counterparty"), dict) else {}
        company = (
            cp.get("company_name")
            or cp.get("name")
            or c.get("company_name")
            or c.get("name")
            or f"Listing #{idx + 1}"
        )
        title = c.get("title") or "RFQ Listing"
        price_val = c.get("price")
        price_str = ""
        if isinstance(price_val, dict) and price_val.get("amount"):
            price_str = f" · {price_val.get('amount')} {price_val.get('currency', 'INR')}"
        link_items.append(
            f"- **{company}** ({title}{price_str}): [View RFQ & Send Connection Request](/marketplace?rfq={rfq_id})"
        )

    if not link_items:
        return ""

    return (
        "\n\n---\n"
        "### 🔗 Direct RFQ Links & Connection Requests:\n"
        "Click any link below to inspect specifications and send a connection request directly:\n"
        + "\n".join(link_items)
    )


_BUYER_SIDE_RE = re.compile(r"\b(buyers?|purchasers?|importers?)\b", re.IGNORECASE)
_SELLER_SIDE_RE = re.compile(r"\b(sellers?|suppliers?|vendors?|manufacturers?|exporters?)\b", re.IGNORECASE)
_SELF_ROLE_RE = re.compile(r"\b(?:i\s+am|i'm|im|we\s+are|as)\s+(?:a\s+|an\s+)?(?:buyer|seller|supplier|vendor)\b", re.IGNORECASE)
_OWN_ROLE_VERB_RE = re.compile(
    r"\b(supply|supplying|sell|selling|offer|offering|buy|buying|purchase|purchasing|procure|procuring|"
    r"source|sourcing|need|require|looking\s+for)\b",
    re.IGNORECASE,
)


# ---- who the user is: only their own words change the role -----------------------------------
is_content_free = chat_specs.is_content_free
explicit_own_role = chat_specs.explicit_own_role


def stated_role(message: str, has_role: bool) -> Optional[str]:
    """The role this message sets, or None. NEVER from a command, a confirmation or a recap reply.

    With a role already on the draft only an explicit own-role statement changes it; with none, the
    rule parser's reading of a message that says something (not "yes publish it") may set it.
    """
    from services import query_extractor

    if not (message or "").strip() or is_content_free(message):
        return None
    explicit = explicit_own_role(message)
    if explicit or has_role:
        return explicit
    detected = query_extractor.detect_role(message)
    return detected.value if detected else None


_META_PRODUCT_RE = re.compile(
    r"\b(attributes?|spec(?:ification)?s?|spesications?|options?|fields?|details?|proceed|publish|post|create|submit|"
    r"confirm|more|add|yes|okay|ok)\b",
    re.IGNORECASE,
)


def message_product(ai_product: Optional[str], message: str, rule_product: Optional[str]) -> Optional[str]:
    """The product a message names, or None.

    A confirmation, a command or a greeting names none ("ok proceed" is not the product "proceed"), and
    neither does a question about the form itself ("what attributes can I add"): the rule parser's
    leftover words never count there. The model's product still has to appear in the user's words.
    """
    if is_content_free(message):
        return None
    grounded = _grounded_ai_product(ai_product, message)
    if grounded:
        return grounded
    if not rule_product or _META_PRODUCT_RE.search(rule_product):
        return None
    if chat_permissions.is_question(message) and chat_conversation.asks_for_specs(message):
        return None
    return rule_product


def _requested_listing_side(message: str) -> Optional[str]:
    """Which side of the market the user asked to see: "buyer details" -> "buyer"."""
    text = _SELF_ROLE_RE.sub(" ", message or "")
    wants_buyers = bool(_BUYER_SIDE_RE.search(text))
    wants_sellers = bool(_SELLER_SIDE_RE.search(text))
    if wants_buyers and not wants_sellers:
        return "buyer"
    if wants_sellers and not wants_buyers:
        return "seller"
    return None


_HINDI_COMMODITY_MAP = {
    "चावल": "rice", "बासमती": "basmati", "गेहूं": "wheat", "कपास": "cotton",
    "दाल": "pulses", "चीनी": "sugar", "चाय": "tea", "लोहा": "steel", "पाइप": "pipe",
    "सेब": "apple", "केला": "banana", "प्याज़": "onion", "टमाटर": "tomato",
    "मक्का": "corn", "बाजरा": "millet", "सोयाबीन": "soybean", "हल्दी": "turmeric",
}


def _clean_city_name(city: Optional[str]) -> Optional[str]:
    if not city:
        return None
    c = str(city).strip()
    c = re.sub(r"[\s,.-]+(?:में|me|mein|se|ko|ke|in|at)\s*$", "", c, flags=re.IGNORECASE).strip()
    return c or None


def _grounded_ai_product(ai_product: Optional[str], message: str) -> Optional[str]:
    """Accept the LLM's product only when the user actually said it.

    The small routing model embellishes ("rice" -> "basmati rice") and echoes
    filler ("details aslo"); neither may drive a database lookup.
    """
    from services import query_extractor

    cleaned = query_extractor.clean_product_phrase(ai_product)
    if not cleaned:
        return None
    lowered = (message or "").lower()
    for word in re.findall(r"\w+", cleaned.lower()):
        if len(word) < 3:
            continue
        stem = word[:-1] if word.endswith("s") and len(word) > 3 else word
        if re.search(rf"\b{re.escape(stem)}", lowered):
            continue
        # Check if Hindi synonym exists in user message
        matched_hindi = any(
            h_word in lowered and (h_trans in word.lower() or word.lower() in h_trans)
            for h_word, h_trans in _HINDI_COMMODITY_MAP.items()
        )
        if matched_hindi:
            continue
        return None
    return cleaned


def _resolve_market_focus(
    message: str,
    previous_focus: Optional[dict[str, Any]] = None,
    ai_extraction: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Work out the product, the user's own role and the market side this turn is about.

    Follow-ups such as "give me the buyer details also" name no product, so the
    product and role carry over from the last turn that did.
    """
    from services import query_extractor

    previous = previous_focus or {}
    ai = ai_extraction or {}
    req = query_extractor.extract(message)

    product = message_product(ai.get("product"), message, req.product)
    category = None if is_content_free(message) else (
        req.category or ai.get("category") or (query_extractor._infer_category(product) if product else None)
    )
    if not product:
        product = previous.get("product")
        category = category or previous.get("category")

    listing_side = _requested_listing_side(message)
    role = previous.get("role")
    # Once a role is known, "buyer details" is a request to see buyers, not a
    # statement that the user is one; only an explicit own-role phrase changes it.
    # The router model's role guess is never used: it assigns a role to almost
    # every message ("thanks" -> buyer), which would then be written into the draft. A command or a
    # confirmation ("yes publish it") states no role, and a known role changes only on the user's own words.
    own = stated_role(message, bool(role))
    if own:
        role = own

    if not listing_side and role in ("buyer", "seller"):
        listing_side = "seller" if role == "buyer" else "buyer"

    return {
        "product": product,
        "category": category,
        "role": role,
        "listing_role": listing_side,
    }


def _remember_market_focus(conversation: Optional[Conversation], focus: dict[str, Any]) -> None:
    """Keep the product and role under discussion for the next turn's follow-ups."""
    if conversation is None or not focus.get("product"):
        return
    mf = {k: focus.get(k) for k in ("product", "category", "role")}
    setattr(conversation, "_pending_market_focus", mf)


async def get_or_create_conversation(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: Optional[uuid.UUID] = None,
    initial_title: Optional[str] = None,
) -> Conversation:
    """Find an existing business chat conversation or create a new one."""
    if conversation_id:
        stmt = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.type == ConversationType.BUSINESS_CHAT,
        )
        res = await db.execute(stmt)
        conv = res.scalar_one_or_none()
        if conv:
            return conv

    title = "New Business Chat"
    if initial_title and initial_title.strip():
        cleaned = " ".join(initial_title.strip().split())
        title = cleaned[:77] + "..." if len(cleaned) > 80 else cleaned

    new_conv = Conversation(
        user_id=user_id,
        type=ConversationType.BUSINESS_CHAT,
        title=title,
        state={},
    )
    db.add(new_conv)
    await db.flush()
    return new_conv


async def save_message_to_conversation(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    metadata: Optional[dict[str, Any]] = None,
) -> Message:
    """Append a message with optional metadata to a conversation."""
    role_enum = MessageRole.USER if role == "user" else MessageRole.ASSISTANT
    msg = Message(
        conversation_id=conversation_id,
        role=role_enum,
        content=content,
        msg_metadata=metadata or {},
        # The question and its reply share one transaction, so the server-side
        # now() would give them identical timestamps and an arbitrary order.
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    await db.flush()
    return msg


async def update_conversation_after_turn(
    db: AsyncSession,
    conversation: Conversation,
    draft: Optional[dict[str, Any]] = None,
    readiness: Optional[dict[str, Any]] = None,
    rfq_id: Optional[uuid.UUID] = None,
    user_prompt: Optional[str] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[str] = None,
    last_counterparty_message: Optional[dict[str, Any]] = None,
    draft_suggestions: Optional[dict[str, Any]] = None,
    draft_moderation: Optional[dict[str, Any]] = None,
    state_patch: Optional[dict[str, Any]] = None,
) -> None:
    """Update conversation state, title, and timestamp. ``state_patch`` sets keys (a None value removes the key)."""
    from sqlalchemy.orm.attributes import flag_modified

    # Query fresh committed state directly from DB to avoid overwriting concurrent turns/tabs
    res = await db.execute(
        text("SELECT state FROM conversations WHERE id = :id"),
        {"id": conversation.id},
    )
    raw_db_state = res.scalar_one_or_none()
    state_update = dict(raw_db_state or {})

    # Preserve any market_focus set during this turn
    pending_mf = getattr(conversation, "_pending_market_focus", None)
    if pending_mf:
        state_update["market_focus"] = pending_mf

    if draft is not None:
        state_update["rfq_draft"] = draft
    if readiness is not None:
        state_update["readiness"] = readiness
    if matched_candidates is not None:
        state_update["matched_candidates"] = matched_candidates
    if draft_suggestions is not None:
        state_update["draft_suggestions"] = draft_suggestions
    if draft_moderation is not None:
        state_update["draft_moderation"] = draft_moderation
    if active_connection_id is not None:
        state_update["active_connection_id"] = str(active_connection_id)
    if last_counterparty_message is not None:
        state_update["last_counterparty_message"] = last_counterparty_message
        if last_counterparty_message.get("connection_id"):
            state_update["active_connection_id"] = str(last_counterparty_message["connection_id"])
    for key, value in (state_patch or {}).items():
        if value is None:
            state_update.pop(key, None)
        else:
            state_update[key] = value
    import json
    conversation.state = state_update
    if rfq_id is not None:
        conversation.rfq_id = rfq_id

    now_utc = datetime.now(timezone.utc)
    conversation.updated_at = now_utc
    if user_prompt and (not conversation.title or conversation.title == "New Business Chat"):
        cleaned = " ".join(user_prompt.strip().split())
        if cleaned:
            conversation.title = cleaned[:77] + "..." if len(cleaned) > 80 else cleaned

    await db.execute(
        text("UPDATE conversations SET state = CAST(:state AS jsonb), updated_at = :updated_at WHERE id = :id"),
        {
            "state": json.dumps(state_update),
            "updated_at": now_utc,
            "id": conversation.id,
        },
    )
    await db.commit()


async def _earlier_user_text(db: AsyncSession, conversation: Conversation) -> str:
    """Everything the USER wrote in this conversation (the grounding source for the model's draft entries).

    A client may send only the newest message, so the stored turns are read as well.
    """
    try:
        rows = (
            await db.execute(
                select(Message.content)
                .where(Message.conversation_id == conversation.id, Message.role == MessageRole.USER)
                .order_by(Message.created_at)
            )
        ).scalars().all()
    except Exception as exc:
        logger.debug("Could not read earlier user messages: %s", exc)
        return ""
    return "\n".join(str(r or "") for r in rows)


async def list_user_conversations(
    db: AsyncSession,
    user_id: uuid.UUID,
    limit: int = 50,
) -> list[Conversation]:
    """List business chat conversations for a user, newest first."""
    stmt = (
        select(Conversation)
        .where(
            Conversation.user_id == user_id,
            Conversation.type == ConversationType.BUSINESS_CHAT,
        )
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_conversation_with_messages(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> Optional[dict[str, Any]]:
    """Retrieve full conversation details and all associated messages for user."""
    stmt = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.user_id == user_id,
        Conversation.type == ConversationType.BUSINESS_CHAT,
    )
    res = await db.execute(stmt)
    conv = res.scalar_one_or_none()
    if not conv:
        return None

    msg_stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    msg_res = await db.execute(msg_stmt)
    messages = list(msg_res.scalars().all())

    formatted_messages = []
    for m in messages:
        meta = m.msg_metadata or {}
        role_val = m.role.value if hasattr(m.role, "value") else str(m.role)
        formatted_messages.append({
            "id": m.id,
            "conversation_id": m.conversation_id,
            "role": role_val,
            "content": m.content,
            "thinking": meta.get("thinking"),
            "thinking_after": meta.get("thinking_after"),
            "tool_step": meta.get("tool_step"),
            "created_rfq": meta.get("created_rfq"),
            "counterparty_message": meta.get("counterparty_message"),
            "routed_agent": meta.get("routed_agent"),
            "created_at": m.created_at,
        })

    return {
        "id": conv.id,
        "title": conv.title,
        "type": conv.type.value if hasattr(conv.type, "value") else str(conv.type),
        "state": conv.state or {},
        "rfq_id": conv.rfq_id,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "messages": formatted_messages,
    }


async def delete_user_conversation(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> bool:
    """Delete a conversation if owned by the user."""
    stmt = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.user_id == user_id,
        Conversation.type == ConversationType.BUSINESS_CHAT,
    )
    res = await db.execute(stmt)
    conv = res.scalar_one_or_none()
    if not conv:
        return False

    await db.delete(conv)
    await db.commit()
    return True


def _extract_msg_fields(msg: Any) -> tuple[str, str]:
    if isinstance(msg, dict):
        role = msg.get("role", "user") or "user"
        content = msg.get("content", "") or ""
        return str(role), str(content)
    role = getattr(msg, "role", "user") or "user"
    content = getattr(msg, "content", "") or ""
    return str(role), str(content)


class _ActionRunner:
    """Runs the router-provisioned action tools for the answer engine and remembers their side effects.

    The engine calls this mid-loop (so the model sees the outcome), while the
    handlers read the collected state afterwards to build the same replies,
    cards and tool-step events they always did.
    """

    def __init__(
        self,
        *,
        draft: dict[str, Any],
        draft_guards: dict[str, Any],
        db: Optional[AsyncSession],
        user: Optional[User],
        conversation: Optional[Conversation],
        matched_candidates: Optional[list[dict[str, Any]]],
        active_connection_id: Optional[Union[str, uuid.UUID]],
        connection_hint: Any,
        on_step: Optional[Any] = None,
        assist: Optional[chat_draft_assist.DraftAssist] = None,
    ) -> None:
        self.assist = assist
        self.draft = draft
        self.draft_guards = draft_guards
        self.db = db
        self.user = user
        self.conversation = conversation
        self.matched_candidates = matched_candidates
        self.active_connection_id = active_connection_id
        self.connection_hint = connection_hint
        self.on_step = on_step
        self.created_rfq_obj: Optional[dict[str, Any]] = None
        self.closed_rfq_obj: Optional[dict[str, Any]] = None
        self.counterparty_msg_obj: Optional[dict[str, Any]] = None
        self.draft_msg_fallback: Optional[tuple[str, str]] = None
        self.captured_tool_step: Optional[dict[str, Any]] = None
        self.err_reply = ""
        self.duplicate = False
        self.touched = False

    def _step(self, step: dict[str, Any], **extra: Any) -> None:
        self.captured_tool_step = step
        if self.on_step:
            self.on_step({"tool_step": step, **extra})

    async def __call__(self, name: str, raw_args: dict[str, Any]) -> dict[str, Any]:
        self.touched = True
        if name == "create_rfq":
            if self.created_rfq_obj:
                return {"ok": True, "note": "already created in this turn", "rfq": self.created_rfq_obj}
            obj, err = await execute_create_rfq_call(
                self.draft, raw_args, db=self.db, user=self.user, assist=self.assist
            )
            self.created_rfq_obj = obj
            if err:
                self.err_reply = f"⚠️ {err}"
                self.duplicate = "already have this RFQ" in err
            self._step(
                {
                    "name": "create_rfq",
                    "title": "Creating Marketplace RFQ",
                    "status": "completed" if obj else "failed",
                    "args": {"title": obj["title"], "id": obj["id"]} if obj else {"error": err},
                },
                created_rfq=obj,
            )
            return {"ok": True, "rfq": obj} if obj else {"ok": False, "error": err}

        if name in ("draft_counterparty_message", "send_counterparty_message"):
            obj, err = await execute_draft_counterparty_message_call(
                raw_args,
                db=self.db,
                user=self.user,
                matched_candidates=self.matched_candidates,
                active_connection_id=self.active_connection_id,
                connection_hint=self.connection_hint,
            )
            self.counterparty_msg_obj = obj
            c_name = obj.get("counterparty_name", "Counterparty") if obj else "Counterparty"
            body = raw_args.get("message")
            if err and not obj and body:
                self.draft_msg_fallback = (body, err)
                self.err_reply = f"📝 **Negotiation Message Draft:**\n\n> {body}\n\n⚠️ *Note: {err}*"
            elif err and not obj:
                self.err_reply = f"⚠️ {err}"
            self._step(
                {
                    "name": "draft_counterparty_message",
                    "title": f"Drafting Message for {c_name}" if obj else "Drafting Negotiation Message",
                    "status": "completed" if (obj or body) else "failed",
                    "args": (
                        {"recipient": c_name, "message": obj["message"]}
                        if obj
                        else ({"recipient": "Counterparty", "message": body, "note": err} if body else {"error": err})
                    ),
                },
                counterparty_message=obj,
            )
            return {"ok": bool(obj), "error": err} if err else {"ok": True, "drafted_for_user_review": True}

        if name == "close_rfq":
            obj, err = await execute_close_rfq_call(
                rfq_id_str=raw_args.get("rfq_id"), conversation=self.conversation, db=self.db, user=self.user
            )
            self.closed_rfq_obj = obj
            if obj:
                self.err_reply = f"✅ **RFQ Closed:** '{obj['title']}' (ID: `{obj['id']}`) has been closed."
            elif err:
                self.err_reply = f"⚠️ {err}"
            self._step(
                {
                    "name": "close_rfq",
                    "title": "Closing Marketplace RFQ",
                    "status": "completed" if obj else "failed",
                    "args": {"title": obj["title"], "id": obj["id"]} if obj else {"error": err},
                },
            )
            return {"ok": True, "closed": obj} if obj else {"ok": False, "error": err}

        if name == "update_rfq_draft":
            notes = self.assist.notes if self.assist is not None else None
            self.draft = _process_tool_call(
                {"function": {"name": name, "arguments": raw_args}}, self.draft, notes=notes, **self.draft_guards
            )
            if self.assist is not None:
                chat_draft_assist.apply_spec_suggestions(self.draft, self.assist.suggestions.get("specs"))
            self._step(
                {
                    "name": "update_rfq_draft",
                    "title": "Updating RFQ Preferences & Draft",
                    "status": "completed",
                    "args": raw_args,
                },
            )
            return {"ok": True, "readiness": rfq_tool_service.evaluate_rfq_readiness(self.draft)}

        return {"ok": False, "error": f"unknown action '{name}'"}


def _engine_allowed_text(
    matched_candidates: Optional[list[dict[str, Any]]],
    counterparty_context: Optional[str],
    last_counterparty_msg: Optional[dict[str, Any]] = None,
) -> str:
    """Prompt-supplied facts the answer may quote besides tool results (candidate cards, counterparty block)."""
    parts: list[str] = []
    if matched_candidates:
        try:
            parts.append(json.dumps(matched_candidates[:10], default=str, ensure_ascii=False))
        except Exception:
            pass
    if counterparty_context:
        parts.append(counterparty_context)
    if last_counterparty_msg:
        parts.append(str(last_counterparty_msg.get("message") or ""))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# One chat turn. /message and /stream both run `_run_chat_turn`; they differ
# only in whether the events are forwarded or folded into one response.
# ---------------------------------------------------------------------------

_HELLO_REPLY = "Hello! I am your B2B Marketplace Business Assistant. Are you looking to BUY or SELL goods?"
_CONNECTION_LOST = "\n\n⚠️ [Connection to local AI assistant interrupted.]"
_TIMED_OUT = "\n\n⏳ [Response timed out. Please try asking a more concise question.]"
_DISPATCH_REPLY_RE = re.compile(r"(✉️|📝|message\s+sent\s+to|dispatched|drafted|dear\s+[\w\s]+,)", re.IGNORECASE)
_SENT_MARKERS = ("message sent to", "dispatched", "✉️", "dear ")


@dataclass
class TurnResult:
    """What a finished turn produced; the /message response is built from this."""

    reply: str
    thinking: Optional[str]
    rfq_draft: dict[str, Any]
    readiness: dict[str, Any]
    created_rfq: Optional[dict[str, Any]]
    counterparty_message: Optional[dict[str, Any]]
    conversation_id: Optional[uuid.UUID]
    routed_agent: dict[str, Any]
    matched_candidates: Optional[list[dict[str, Any]]] = None
    draft_suggestions: Optional[dict[str, Any]] = None
    draft_moderation: Optional[dict[str, Any]] = None
    editing_rfq_id: Optional[str] = None

    def as_response(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "thinking": self.thinking,
            "rfq_draft": self.rfq_draft,
            "readiness": self.readiness,
            "created_rfq": self.created_rfq,
            "counterparty_message": self.counterparty_message,
            "conversation_id": self.conversation_id,
            "routed_agent": self.routed_agent,
            "matched_candidates": self.matched_candidates,
            "draft_suggestions": self.draft_suggestions,
            "draft_moderation": self.draft_moderation,
            "editing_rfq_id": self.editing_rfq_id,
        }


@dataclass
class ChatEvent:
    """One step of a turn. ``chunk`` is exactly the JSON object the SSE stream sends.

    ``kind`` is one of status, tool_step, replace_content, final or error.
    ``result`` is set on the last event (final or error) only.
    """

    kind: str
    chunk: dict[str, Any]
    result: Optional[TurnResult] = None


def _action_tool_specs(names: set[str]) -> list[dict[str, Any]]:
    """Tool schemas for the action tools the permission gate allowed, in a fixed order."""
    table = {
        "update_rfq_draft": rfq_tool_service.UPDATE_RFQ_DRAFT_TOOL,
        "create_rfq": rfq_tool_service.CREATE_RFQ_TOOL,
        "draft_counterparty_message": rfq_tool_service.DRAFT_COUNTERPARTY_MESSAGE_TOOL,
        "close_rfq": rfq_tool_service.CLOSE_RFQ_TOOL,
    }
    # publish_rfq and edit_rfq are explicit-command shortcuts handled before the model runs: no model-facing spec.
    return [table[n] for n in chat_permissions.ALL_ACTION_TOOLS if n in table and n in names]


def _cards_from_tool_results(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Listing cards (id, title, price, ...) built from what search_listings/find_matches returned this turn.

    They back the "Direct RFQ links" appendix and let a follow-up such as "message the
    first one" find the listing. Only real tool rows are used.
    """
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for res in tool_results or []:
        data = res.get("data") or {}
        rows = data.get("matches") if res.get("tool") == "find_matches" else data.get("samples")
        if res.get("tool") not in ("find_matches", "search_listings") or not rows:
            continue
        for row in rows:
            rid = str(row.get("id") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            score_pct = row.get("match_score_pct")
            cards.append({
                "rfq_id": rid,
                "title": row.get("title"),
                "role": row.get("role"),
                "price": {
                    "amount": row.get("price"),
                    "currency": row.get("currency"),
                    "per_unit": row.get("price_per_unit"),
                } if row.get("price") is not None else None,
                "quantity": {"value": row.get("quantity"), "unit": row.get("quantity_unit")} if row.get("quantity") is not None else None,
                "location": {"city": row.get("city")} if row.get("city") else None,
                "score": score_pct / 100 if isinstance(score_pct, (int, float)) else None,
                "rank": len(cards) + 1,
            })
    return cards[:10]


async def _match_payloads(
    db: Optional[AsyncSession], user: Optional[User], tool_results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Frontend ``MatchCandidate`` objects for the listings the read tools returned this turn.

    They come from the same serializer as the match API (``match_service._to_candidate``),
    loaded from the database in tool order, at most 10. A find_matches row carries the
    matcher's real score breakdown (recomputed with the same call the tool made). A
    search_listings row was never scored, so its ``score`` is null, which the UI renders
    without a percentage; no score is ever invented.
    """
    if db is None or user is None:
        return []
    from types import SimpleNamespace

    from sqlalchemy.orm import selectinload

    from models.rfq import RFQ
    from models.user import UserProfile
    from schemas.match import MatchScore
    from services import match_service

    # (rfq id, requester rfq id for find_matches rows) in tool order, de-duplicated
    ordered: list[tuple[uuid.UUID, Optional[uuid.UUID]]] = []
    seen: set[uuid.UUID] = set()
    for res in tool_results or []:
        tool = res.get("tool")
        if tool not in ("find_matches", "search_listings") or not res.get("ok", True):
            continue
        data = res.get("data") or {}
        rows = data.get("matches") if tool == "find_matches" else data.get("samples")
        requester = res.get("args", {}).get("rfq_id") if tool == "find_matches" else None
        for row in rows or []:
            try:
                rid = uuid.UUID(str(row.get("id")))
                req_id = uuid.UUID(str(requester)) if requester else None
            except (ValueError, TypeError):
                continue
            if rid not in seen:
                seen.add(rid)
                ordered.append((rid, req_id))
    ordered = ordered[:10]
    if not ordered:
        return []

    try:
        rfqs = {
            r.id: r
            for r in (
                await db.scalars(
                    select(RFQ)
                    .options(selectinload(RFQ.user).selectinload(User.profile))
                    .where(RFQ.id.in_([rid for rid, _ in ordered]))
                )
            ).all()
        }
        connections = await match_service._connections_for(db, user.id, list(rfqs))
        scores: dict[uuid.UUID, dict[uuid.UUID, Any]] = {}
        requesters: dict[uuid.UUID, Any] = {}
        for req_id in {req for _, req in ordered if req}:
            requester = await db.get(RFQ, req_id)
            if requester is None:
                continue
            requesters[req_id] = requester
            prefs = (
                await db.execute(select(UserProfile.matching_preferences).where(UserProfile.user_id == user.id))
            ).scalar_one_or_none() or None
            pairs, _path = await match_service._collect(db, requester, user.id, prefs)
            scores[req_id] = {cand.id: score for cand, score in pairs}

        blank = SimpleNamespace(latitude=None, longitude=None, location_country=None)
        out: list[dict[str, Any]] = []
        for rid, req_id in ordered:
            cand = rfqs.get(rid)
            if cand is None:
                continue
            score = (scores.get(req_id) or {}).get(rid) if req_id else None
            card = match_service._to_candidate(
                requesters.get(req_id, blank) if req_id else blank,
                cand,
                score or MatchScore(total=0),
                len(out) + 1,
                connections.get(rid),
            ).model_dump(mode="json")
            if score is None:
                card["score"] = None  # unscored listing: never show a made-up percentage
            out.append(card)
        return out
    except Exception as exc:
        logger.warning("Could not build match cards: %s", exc)
        return []


def _last_user_and_previous_assistant(messages: list[Any]) -> tuple[str, str]:
    """The newest user message and the assistant text that came right before it."""
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if _extract_msg_fields(messages[i])[0] == "user":
            last_user_idx = i
            break
    if last_user_idx < 0:
        return "", ""
    last_user = _extract_msg_fields(messages[last_user_idx])[1]
    for j in range(last_user_idx - 1, -1, -1):
        role, content = _extract_msg_fields(messages[j])
        if role == "assistant":
            return last_user, content
        if role == "user":
            break
    return last_user, ""


def _reply_extras(
    assist: chat_draft_assist.DraftAssist, reply: str, draft: dict[str, Any], created: Optional[dict[str, Any]]
) -> str:
    """Sentences added to a reply: unrecognised HSN codes and a content-policy warning for the current draft."""
    if created:
        return ""
    lines = [n for n in assist.notes if n not in reply]
    warning = assist.current_warning(draft) if assist.moderation_checked_now else None
    if warning and warning not in reply and "content policy" not in reply:
        lines.append(f"⚠️ {warning}")
    return "\n".join(lines)


async def _plan_rfq_lifecycle(
    db: Optional[AsyncSession],
    user: Optional[User],
    conversation: Optional[Conversation],
    text: str,
    previous_assistant: str,
    current_rfq: Optional[dict[str, Any]],
    editing_id: Optional[str],
    editing_title: str,
) -> Optional[dict[str, Any]]:
    """Does this message publish a saved draft RFQ, load an RFQ for editing, or save/cancel an edit?

    Returns ``{"kind": publish | edit_load | save | cancel | reply, ...}`` or None when the message is
    an ordinary turn (including "create the RFQ" for the chat's own draft).
    """
    from models.rfq import RFQ

    if db is None or user is None or not (text or "").strip():
        return None
    perms = chat_permissions
    if editing_id:
        if perms.check_cancel_edit_intent(text):
            return {"kind": "cancel"}
        if perms.check_save_edit_intent(text, previous_assistant):
            return {"kind": "save", "publish": perms.says_publish(text) and not perms.wants_draft_status(text)}
        if perms.check_edit_rfq_intent(text) and perms.edit_rfq_target(text):
            return {"kind": "reply", "reply": (
                f"You're editing '{editing_title}'. Say \"save changes\" or \"cancel edit\" first, then I can edit another RFQ."
            )}
        return None

    state = (conversation.state or {}) if conversation else {}
    drafting = chat_permissions._drafting_in_progress(current_rfq or {})
    editable = (RFQStatus.DRAFT, RFQStatus.ACTIVE)

    if perms.check_edit_rfq_intent(text):
        target = perms.edit_rfq_target(text)
        rfq = None
        if target:
            rfq = await _resolve_user_rfq(db, user, target, editable)
            if rfq is None:
                rfq = await _resolve_user_rfq(db, user, target, (RFQStatus.CLOSED, RFQStatus.EXPIRED))
            if rfq is None:
                if drafting:
                    return None  # "update the rfq ..." about the chat's own draft
                return {"kind": "reply", "reply": f"No RFQ matching '{target}' was found in your listings."}
        elif drafting:
            return None
        else:
            last_id = state.get("last_created_rfq_id")
            if last_id:
                try:
                    last = await db.get(RFQ, uuid.UUID(str(last_id)))
                except Exception:
                    last = None
                if last is not None and last.user_id == user.id and last.status in editable:
                    rfq = last
            if rfq is None:
                rows = (
                    await db.execute(
                        select(RFQ)
                        .where(RFQ.user_id == user.id, RFQ.status.in_(list(editable)))
                        .order_by(RFQ.created_at.desc())
                        .limit(5)
                    )
                ).scalars().all()
                if not rows:
                    return {"kind": "reply", "reply": "You have no RFQs that can be edited yet."}
                if len(rows) > 1:
                    titles = "; ".join(f"'{r.title}'" for r in rows)
                    return {"kind": "reply", "reply": (
                        f"You have several RFQs ({titles}). Tell me which one to edit, for example \"edit my rice RFQ\"."
                    )}
                rfq = rows[0]
        if rfq.user_id != user.id:
            return {"kind": "reply", "reply": "You do not have permission to edit this RFQ."}
        if rfq.status not in editable:
            return {"kind": "reply", "reply": (
                f"'{rfq.title}' is {rfq.status.value} and can no longer be edited. "
                "You can create a new RFQ from it instead."
            )}
        return {"kind": "edit_load", "rfq": rfq}

    if perms.check_publish_rfq_intent(text, previous_assistant):
        target = perms.publish_rfq_target(text)
        if target:
            if drafting and await _resolve_user_rfq(db, user, target, (RFQStatus.DRAFT,)) is None:
                return None  # the chat's own draft is being named: create it
            return {"kind": "publish", "target": target}
        if drafting:
            return None
        has_draft = (
            await db.execute(
                select(func.count()).select_from(RFQ).where(RFQ.user_id == user.id, RFQ.status == RFQStatus.DRAFT)
            )
        ).scalar_one()
        return {"kind": "publish", "target": None} if has_draft else None
    return None


_INLINE_MY = r"(?:this|the|my|our|that|meri|mera|mere|apni|apna)"
_INLINE_OBJ = r"(?:rfqs?|listings?|postings?|requirements?)"
_INLINE_UUID_RE = re.compile(
    r"(?:\b(?:of|in|for|on)\s+)?(?:\b(?:my|the|this)\s+)?(?:\b(?:rfq|listing)\s+)?(?:\bid\s+)?"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_INLINE_COMMAND_RE = re.compile(
    r"\b(?:(?:and|then|aur|phir)\s+|&\s*)?(?:please\s+)?(?:save|apply|confirm|commit|publish|post|go\s+live)\b"
    r"(?:\s+(?:it|this|that|the|my|these|all|changes|edits|updates|rfq|listing))*"
    r"(?:\s+kar(?:o|do|dena|dijiye)?(?:\s+(?:do|dena|dijiye))?)?",
    re.IGNORECASE,
)
_INLINE_PRICE_WORD_RE = re.compile(r"\b(price|rate|cost|budget|amount)\b", re.IGNORECASE)
_INLINE_QTY_WORD_RE = re.compile(r"\b(quantity|qty|volume|units?)\b", re.IGNORECASE)
# Only these fields are ever taken from an edit command's change text: never product, category or role.
_INLINE_FIELDS = ("quantity", "price_target", "location", "deadline")


def _strip_edit_command(message: str) -> str:
    """The change text left in an edit/save command once the saved-RFQ reference and "save" words are removed."""
    text = message or ""
    target = chat_permissions.edit_rfq_target(text)
    if target and _INLINE_UUID_RE.search(target):
        text = _INLINE_UUID_RE.sub(" ", text)
    else:
        fillers = r"(?:(?:draft|saved|existing|current|posted|published|active)\s+)*"
        named = r"\s+".join(re.escape(w) for w in target.split()) + r"\s+" if target else ""
        text = re.sub(
            rf"(?:\b(?:of|in|for|on)\s+)?\b{_INLINE_MY}\s+{fillers}{named}{_INLINE_OBJ}\b",
            " ", text, count=1, flags=re.IGNORECASE,
        )
    text = _INLINE_COMMAND_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" ,.;:-")


def apply_inline_changes(draft: dict[str, Any], message: str) -> list[str]:
    """Apply what an edit/save command states ("change the quantity of my rice RFQ to 600 kg") to ``draft``.

    The rule pre-processor runs on the change text alone, and only the quantity, price, location and
    deadline it finds are taken. Returns the draft fields that were set.
    """
    remainder = _strip_edit_command(message)
    if not remainder:
        return []
    found = rfq_tool_service.preprocess_message_to_rfq(remainder, {})
    if (
        found.get("quantity")
        and not found.get("price_target")
        and _INLINE_PRICE_WORD_RE.search(remainder)
        and not _INLINE_QTY_WORD_RE.search(remainder)
    ):
        found.pop("quantity")  # "set price to 50": the figure is the price, not a quantity
    applied: list[str] = []
    for key in _INLINE_FIELDS:
        if found.get(key):
            draft[key] = found[key]
            applied.append(key)
    return applied


async def _save_edit_flow(
    draft: dict[str, Any],
    snapshot: dict[str, Any],
    edit_id: str,
    publish: bool,
    conversation: Optional[Conversation],
    db: Optional[AsyncSession],
    user: Optional[User],
    assist: chat_draft_assist.DraftAssist,
) -> tuple[str, Optional[dict[str, Any]], Optional[dict[str, Any]], bool, bool]:
    """Save the edited RFQ (and publish it when asked): ``(reply, tool_step, created_rfq, done, ok)``.

    ``done`` means edit mode ends (the save went through, or the save+publish was attempted);
    ``ok`` is False for any refusal or failure shown in the reply.
    """
    fields = sorted(chat_rfq_edit.changed_fields(draft, snapshot))
    saved, err, changed = await execute_save_rfq_edit_call(draft, snapshot, edit_id, db=db, user=user, assist=assist)
    title = str(snapshot.get("title") or "the RFQ")
    step: Optional[dict[str, Any]] = {
        "name": "update_rfq",
        "title": "Saving RFQ Changes",
        "status": "completed" if saved else "failed",
        "args": {"title": saved["title"], "id": saved["id"], "fields": fields} if saved else {"error": err},
    }
    if err:
        return f"⚠️ {err}", step, None, False, False
    if not changed and not publish:
        return (
            f"There is nothing to save: '{title}' is unchanged. Tell me what to change, "
            "or say \"cancel edit\" to leave edit mode."
        ), None, None, False, False
    result, reply, ok = None, "", True
    if saved:
        result = saved
        title = saved["title"]
        reply = f"✅ **RFQ Updated:** '{title}' (ID: `{saved['id']}`) saved. Changed: {', '.join(f.replace('_', ' ') for f in fields)}."
    if publish and (saved is None or saved["status"] == "draft"):
        published, perr = await execute_publish_rfq_call(
            rfq_id_str=edit_id, conversation=conversation, db=db, user=user
        )
        if published:
            result = published
            reply = (reply + "\n\n" if reply else "") + _published_text(published)
            step = {"name": "publish_rfq", "title": "Publishing Marketplace RFQ", "status": "completed",
                    "args": {"title": published["title"], "id": published["id"]}}
        else:
            ok = False
            reply = (reply + "\n\n" if reply else "") + f"⚠️ {perr}"
    return reply, step, result, True, ok


_CONVO_RESET: dict[str, Any] = {
    "recap_hash": None, "recap_awaiting": None, "declined_fields": None, "asked_fields": None,
    "last_asked": None, "auto_role": None, "auto_role_noted": None, "recap_publish": None,
    "recap_snapshot": None, "spec_value_ask": None,
}
MAX_HSN_OFFERS = 2  # the suggested HSN is offered this many times, then left alone
_ANSWER_FIELDS = {"quantity": "quantity", "price": "price", "location": "location", "dispatch": "deadline"}
_ASKS_RE = re.compile(r"^\s*(?:what|which|how|why|when|where|who|kya|kaun|konsa|konsi|konse|kaise|kyun|kab)\b", re.IGNORECASE)


def _asks_something(message: str) -> bool:
    """A question or an information request ("which grades are available?"), not an answer to the bot's question."""
    return chat_permissions.is_question(message) or bool(_ASKS_RE.match(message or ""))
_OPTIONAL_ASKS = ("quantity", "min_order", "price", "location", "dispatch", "hsn")


def _apply_answer(draft: dict[str, Any], field: str, message: str) -> bool:
    """File a reply to ONE question (the one just asked) under that field when the general parser missed it."""
    from services import query_extractor

    key = _ANSWER_FIELDS.get(field)
    if not key or not (message or "").strip():
        return False
    ans = query_extractor.parse_answer(key, message)
    if ans is None:
        return False
    if field == "quantity" and ans.quantity_value is not None and not draft.get("quantity"):
        draft["quantity"] = {"value": float(ans.quantity_value), "unit": ans.quantity_unit or "pcs"}
    elif field == "price" and ans.price_amount is not None and not draft.get("price_target"):
        price: dict[str, Any] = {"amount": float(ans.price_amount), "currency": ans.price_currency or "INR"}
        per_unit = chat_specs.normalize_per_unit(ans.price_per_unit)
        if per_unit:
            price["per_unit"] = per_unit
        draft["price_target"] = price
    elif field == "location" and ans.city and not (draft.get("location") or {}).get("city"):
        draft["location"] = _draft_location(ans.city, ans.country)
    elif field == "dispatch" and ans.deadline_days is not None and not draft.get("deadline"):
        draft["deadline"] = {"in_days": int(ans.deadline_days)}
    else:
        return False
    return True


def _edit_summary(draft: dict[str, Any]) -> str:
    """One line of the current values of an RFQ loaded for editing."""
    parts: list[str] = []
    qty = draft.get("quantity") or {}
    if qty:
        parts.append(f"quantity {qty.get('value')} {qty.get('unit')}")
    price = draft.get("price_target") or {}
    if price:
        parts.append(f"target price {price.get('amount')} {price.get('currency')}" + (f"/{price['per_unit']}" if price.get("per_unit") else ""))
    loc = draft.get("location") or {}
    if loc.get("city"):
        parts.append(f"location {loc['city']}")
    if (draft.get("deadline") or {}).get("date"):
        parts.append(f"deadline {draft['deadline']['date']}")
    return ", ".join(parts) or "no quantity, price, location or deadline yet"


async def _run_chat_turn(
    messages: list[Any],
    current_rfq: Optional[dict[str, Any]] = None,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    conversation_id: Optional[uuid.UUID] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
    agent: Optional[AgentDefinition] = None,
    rfq_id: Optional[uuid.UUID] = None,
) -> AsyncIterator[ChatEvent]:
    """Run one chat turn and yield its events. The only place a turn's logic and persistence live.

    Order: persist the user message, read the AI parse of the message (intent,
    product, location), ask ``chat_permissions`` which action tools may be used,
    then either take an explicit-command shortcut (create / close) or let the
    answer engine run its read tools plus the allowed action tools. Afterwards the
    reply is guarded (false refusal, unbacked "RFQ created", counterparty letter),
    persisted once and emitted as the final event.
    """
    turn_started = time.perf_counter()

    def mark(phase: str) -> None:
        timing_log.debug("turn %.2fs  %s", time.perf_counter() - turn_started, phase)

    last_user_content, previous_assistant = _last_user_and_previous_assistant(messages)

    conversation = None
    if db is not None and user is not None:
        try:
            conversation = await get_or_create_conversation(
                db, user.id, conversation_id=conversation_id, initial_title=last_user_content
            )
            if last_user_content.strip():
                await save_message_to_conversation(db, conversation.id, role="user", content=last_user_content)
                await db.commit()
        except Exception as exc:
            logger.exception("Failed to initialize conversation: %s", exc)

    state = (conversation.state or {}) if conversation else {}
    earlier_user_text = await _earlier_user_text(db, conversation) if (db is not None and conversation is not None) else ""
    assist = chat_draft_assist.DraftAssist(state)
    if conversation is not None and isinstance(state.get("rfq_draft"), dict):
        # The server's draft is authoritative; the client copy is only for stateless calls.
        current_rfq = dict(state["rfq_draft"])
    turn_start_draft = json.loads(json.dumps(current_rfq or {}))  # what the draft was before this turn (for the honest acknowledgement)

    editing_id = state.get("editing_rfq_id") if conversation else None
    editing_snapshot = state.get("editing_snapshot") if conversation else None
    if not (editing_id and isinstance(editing_snapshot, dict)):
        editing_id, editing_snapshot = None, None
    edit_ctx: dict[str, Any] = {"id": editing_id, "snapshot": editing_snapshot}
    state_patch: dict[str, Any] = {}

    # What the conversation remembers between turns (see chat_conversation): the recap the user was shown,
    # the fields they declined, how often each was asked, and the register (English / Hinglish).
    gating = conversation is not None
    recap_hash = state.get("recap_hash") if gating else None
    recap_snapshot = state.get("recap_snapshot") if gating else None  # the exact draft the recap showed
    recap_awaiting = state.get("recap_awaiting") if gating else None
    declined: list[str] = [str(x) for x in (state.get("declined_fields") or [])] if gating else []
    asked: dict[str, int] = dict(state.get("asked_fields") or {}) if gating else {}
    last_asked = state.get("last_asked") if gating else None
    lang = chat_conversation.pick_lang(last_user_content, state.get("lang") if gating else None)
    if gating and lang == "hinglish" and state.get("lang") != "hinglish":
        state_patch["lang"] = "hinglish"
    recap_reply: Optional[str] = None
    decline: Optional[str] = None
    if gating and recap_awaiting and chat_conversation.draft_hash(current_rfq) == recap_hash:
        recap_reply = chat_conversation.classify_recap_reply(last_user_content)
    if gating and not editing_id and last_asked and not recap_reply and not recap_awaiting:
        decline = chat_conversation.detect_decline(last_user_content)
        if decline == "all":
            declined = list(dict.fromkeys([
                *declined, *_OPTIONAL_ASKS,
                "specs",
            ]))
            state_patch["declined_fields"] = declined
        elif decline == "one" and str(last_asked) not in ("role", "product") and str(last_asked) not in declined:
            declined.append(str(last_asked))
            state_patch["declined_fields"] = declined

    client_cands = list(matched_candidates or [])
    last_counterparty_msg = state.get("last_counterparty_message") if conversation else None
    if conversation and not active_connection_id and state.get("active_connection_id"):
        active_connection_id = state.get("active_connection_id")
    has_prev_counterparty = bool(last_counterparty_msg) or any(
        _extract_msg_fields(m)[0] == "assistant" and any(k in _extract_msg_fields(m)[1].lower() for k in _SENT_MARKERS)
        for m in messages
    )
    active_conv_id = str(conversation.id) if conversation else None

    # One model call reads the message (intent, role, product, location). It is cached,
    # so the sync extract()/detect_role() calls below reuse it.
    from services import query_extractor

    await query_extractor.extract_async(last_user_content)
    parse = ai_query_parser.cached(last_user_content)
    parsed_intent = parse.intent if parse else None
    mark("parsed the message")

    lifecycle = await _plan_rfq_lifecycle(
        db, user, conversation, last_user_content, previous_assistant, current_rfq,
        editing_id, str((editing_snapshot or {}).get("title") or ""),
    )
    forced_create: Optional[str] = None  # "yes" | "draft": a bare answer to the recap question
    if recap_reply and not lifecycle:
        if editing_id and recap_awaiting == "edit":
            lifecycle = {"kind": "save", "publish": False} if recap_reply in ("yes", "draft") else {"kind": "recap_no"}
        elif not editing_id and recap_awaiting == "create":
            if recap_reply == "no":
                lifecycle = {"kind": "recap_no"}
            else:
                forced_create = recap_reply
    edit_loaded_rfq = None
    if lifecycle and lifecycle["kind"] == "edit_load":
        edit_loaded_rfq = lifecycle["rfq"]
        loaded = chat_rfq_edit.rfq_to_draft(edit_loaded_rfq)
        if chat_permissions._drafting_in_progress(current_rfq or {}):
            assist.notes.append("Your unsent chat draft was replaced by the RFQ you are editing.")
        current_rfq = json.loads(json.dumps(loaded))
        editing_id, editing_snapshot = str(edit_loaded_rfq.id), loaded
        edit_ctx.update(id=editing_id, snapshot=editing_snapshot)
        state_patch.update(editing_rfq_id=editing_id, editing_snapshot=editing_snapshot)
        assist.reset()
        edit_note = f"✏️ Editing '{edit_loaded_rfq.title}'. Say \"save changes\" when you're done, or \"cancel edit\" to discard."
        assist.notes.append(edit_note)

    allowed = chat_permissions.allowed_action_tools(
        last_user_content,
        current_rfq,
        {
            "editing_rfq_id": editing_id,
            "previous_assistant": previous_assistant,
            "active_connection_id": active_connection_id,
            "last_counterparty_message": last_counterparty_msg,
            "has_previous_counterparty_msg": has_prev_counterparty,
        },
        parsed_intent,
    )
    if lifecycle and lifecycle["kind"] == "publish":
        allowed = {"publish_rfq"}
    elif lifecycle and lifecycle["kind"] == "edit_load":
        allowed = {"edit_rfq"}
        if chat_permissions.edit_command_has_change(last_user_content):
            allowed.add("update_rfq_draft")
    elif editing_id:
        allowed.discard("create_rfq")  # while editing, "create" means save
    if forced_create:
        allowed = {"create_rfq", "update_rfq_draft"}
    is_counterparty_msg = "draft_counterparty_message" in allowed
    # A reply to the spec question ("quality is minimum brix, packaging carton...") can read like a lookup to the
    # parser, but it answers the question just asked.
    answers_specs = bool(gating and last_asked in ("specs", "hsn") and not chat_permissions.is_question(last_user_content))
    # A message that answers the question the bot just asked (a field, the spec question, the HSN offer, the recap) is a
    # drafting turn whatever the parser made of it: "certification must of organic and fssai" reads as a trust lookup.
    answers_question = bool(
        gating and (last_asked or recap_awaiting == "create") and not editing_id and not lifecycle and not forced_create
        and not recap_reply and not _asks_something(last_user_content)
    )
    if (
        gating and last_asked and not editing_id and not lifecycle and not allowed
        and (answers_specs or not chat_permissions.is_information_request(last_user_content))
    ):
        allowed.add("update_rfq_draft")  # a reply to the question the assistant just asked

    # A confirmation or command ("yes", "ok proceed", "yes publish it") changes nothing in the draft: no pre-processing, no
    # overlays. After a recap the draft it creates is the snapshot the user was shown.
    confirmation_turn = bool(forced_create) or ("create_rfq" in allowed and is_content_free(last_user_content))
    if lifecycle or forced_create or decline:
        # A lifecycle command ("publish my rice rfq", "save changes") carries no draft details to extract.
        draft = json.loads(json.dumps(current_rfq or {}))
        if forced_create and isinstance(recap_snapshot, dict) and chat_conversation.draft_hash(recap_snapshot) == recap_hash:
            draft = json.loads(json.dumps(recap_snapshot))
        if lifecycle and lifecycle["kind"] in ("edit_load", "save") and chat_permissions.edit_command_has_change(last_user_content):
            applied_inline = apply_inline_changes(draft, last_user_content)
            if applied_inline:
                logger.info("Applied inline edit fields %s", applied_inline)
    else:
        draft = rfq_tool_service.preprocess_message_to_rfq(
            last_user_content,
            current_rfq,
            has_active_connection=bool(active_connection_id),
            has_previous_counterparty_msg=has_prev_counterparty,
        )
        # A role changes only on the user's own words, never from a command or a bare confirmation.
        prev_role = (current_rfq or {}).get("role")
        if draft.get("role") != prev_role and draft.get("role") != stated_role(last_user_content, bool(prev_role)):
            logger.info("Ignoring role %s -> %s read from %r", prev_role, draft.get("role"), last_user_content[:60])
            if prev_role:
                draft["role"] = prev_role
            else:
                draft.pop("role", None)

    if not client_cands and is_counterparty_msg and isinstance(state.get("matched_candidates"), list):
        matched_candidates = state["matched_candidates"]  # "message the first one" needs last turn's listings
    else:
        matched_candidates = client_cands or None

    previous_focus = state.get("market_focus") if conversation else None
    sem_ext = {"product": parse.product, "category": parse.category, "location": parse.location} if parse else {}
    focus = _resolve_market_focus(last_user_content, previous_focus, sem_ext)
    if (
        gating and last_asked and not editing_id and chat_draft_assist.product_name(current_rfq)
        and not chat_permissions.is_question(last_user_content)
        and not chat_permissions.is_information_request(last_user_content)
    ):
        # An answer to the question just asked ("fuji, organic and fssai") is about the draft's product,
        # not a new product made of the answer's words.
        focus["product"] = chat_draft_assist.product_name(current_rfq)

    counterparty_context = None
    if is_counterparty_msg and db is not None and user is not None:
        counterparty_context, resolved_conn_id = await _resolve_counterparty_context(
            db, user, last_user_content, focus, active_connection_id
        )
        if resolved_conn_id and not active_connection_id:
            active_connection_id = resolved_conn_id

    # The shown specialist is cosmetic: parser intent now, the tools actually used later.
    routed_agent, _why = agent_for_intent(
        parsed_intent or chat_permissions.effective_intent(last_user_content),
        draft=draft,
        explicit_agent_id=agent.id if agent else None,
    )
    agent_state = {"info": agent_info(routed_agent)}
    _remember_market_focus(conversation, focus)

    req_linguistic = query_extractor.extract(last_user_content)
    effective_prod = message_product(sem_ext.get("product"), last_user_content, req_linguistic.product)
    effective_cat = None if is_content_free(last_user_content) else (
        req_linguistic.category or sem_ext.get("category") or (
            query_extractor._infer_category(effective_prod) if effective_prod else None
        )
    )
    if "update_rfq_draft" in allowed and not confirmation_turn:
        own_role = stated_role(last_user_content, bool(draft.get("role")))
        if own_role and draft.get("role") != own_role:
            draft["role"] = own_role
        elif focus["role"] and not draft.get("role"):
            draft["role"] = focus["role"]
        if effective_prod and not (draft.get("product_details") or {}).get("name"):
            if not isinstance(draft.get("product_details"), dict):
                draft["product_details"] = {}
            draft["product_details"]["name"] = effective_prod
        if effective_cat and not draft.get("category"):
            draft["category"] = effective_cat
        ai_loc = sem_ext.get("location")
        if ai_loc and not (draft.get("location") or {}).get("city"):
            raw_city = ai_loc.split(",")[0].strip()
            draft["location"] = _draft_location(_clean_city_name(raw_city) or raw_city, ai_loc)
        elif req_linguistic.city and not (draft.get("location") or {}).get("city"):
            draft["location"] = _draft_location(
                _clean_city_name(req_linguistic.city) or req_linguistic.city, req_linguistic.country
            )
    # The form's helpers: a typed or accepted HSN, must-match phrases, then fresh suggestions and moderation.
    pending = assist.pending_hsn
    qualifier = (
        chat_draft_assist.hsn_qualifier_answer(last_user_content, pending)
        if (
            pending and not confirmation_turn and not editing_id and chat_draft_assist.product_name(draft)
            and assist.suggestions.get("hsn_for") == chat_draft_assist.product_name(draft)
            and (assist.suggestions.get("hsn_offered") or "hsn" in previous_assistant.lower())
            and not chat_permissions.is_question(last_user_content)
        ) else None
    )
    if qualifier:
        # "fresh" answers the fresh-or-dried distinction in the offered HSN: it accepts that HSN and is a spec
        # (condition: Fresh), not a description.
        chat_draft_assist.apply_hsn(draft, str(pending["code"]), pending.get("category"), assist.notes)
        details = dict(draft.get("product_details") or {})
        details.setdefault("condition", qualifier)
        draft["product_details"] = details
        assist.applied_hsn = True
    elif (
        chat_draft_assist.product_name(draft) and not chat_permissions.is_information_request(last_user_content)
        and not confirmation_turn
    ):
        typed_hsn = chat_draft_assist.explicit_hsn_code(last_user_content)
        if typed_hsn:
            hsn_status, _hsn_item = chat_draft_assist.apply_explicit_hsn(draft, typed_hsn, assist.notes)
            if hsn_status == "ok":
                assist.applied_hsn = True
            elif hsn_status in ("unknown", "invalid"):
                assist.notes.append(chat_draft_assist.unrecognised_hsn_note(typed_hsn))
        elif assist.suggestions.get("hsn_for") == chat_draft_assist.product_name(draft) and chat_draft_assist.accepts_hsn(
            last_user_content, previous_assistant, assist.pending_hsn
        ):
            pending = assist.pending_hsn or {}
            chat_draft_assist.apply_hsn(draft, str(pending["code"]), pending.get("category"), assist.notes)
            assist.applied_hsn = True
    if assist.applied_hsn:
        allowed.add("update_rfq_draft")
    if not confirmation_turn:
        chat_draft_assist.must_match_from_text(draft, last_user_content)

    def lock_role() -> None:
        if edit_ctx["snapshot"] and chat_rfq_edit.enforce_locked_role(draft, edit_ctx["snapshot"]):
            note = f"The role cannot be changed while editing an RFQ; it stays {str(edit_ctx['snapshot']['role']).upper()}."
            if note not in assist.notes:
                assist.notes.append(note)

    lock_role()

    # An account that can post on one side only never gets asked which side; a stated side it cannot post is refused.
    account_roles = {r for r in ("buyer", "seller") if user.can_post_as(r)} if user is not None else {"buyer", "seller"}
    sole_role = next(iter(account_roles)) if len(account_roles) == 1 else None
    role_refused = False

    def enforce_account_role() -> None:
        nonlocal role_refused
        if edit_ctx["snapshot"] or "update_rfq_draft" not in allowed or sole_role is None:
            return
        stated = str(draft.get("role") or "").lower()
        if stated and stated != sole_role:
            draft.pop("role", None)
            role_refused = True
            if lang == "hinglish":
                note = (
                    f"Aapka account sirf {sole_role} ke roop mein post kar sakta hai, isliye main {stated} RFQ nahi bana sakta. "
                    f"Kya main ise {sole_role} ke roop mein bana dun?"
                )
            else:
                note = (
                    f"Your account can only post as a {sole_role}, so I can't create a {stated} RFQ from it. "
                    f"Would you like me to draft it as a {sole_role} instead?"
                )
            if note not in assist.notes:
                assist.notes.append(note)
        elif (
            not stated and chat_draft_assist.product_name(draft) and not role_refused
            # "publish it" with nothing drafted is not a product: leave the role question to the create path
            and not ("create_rfq" in allowed and not chat_draft_assist.product_name(current_rfq or {}))
        ):
            draft["role"] = sole_role
            state_patch["auto_role"] = sole_role

    enforce_account_role()
    if (
        gating and last_asked in _ANSWER_FIELDS and "update_rfq_draft" in allowed and not edit_ctx["snapshot"]
        and not confirmation_turn and not chat_permissions.is_question(last_user_content)
    ):
        _apply_answer(draft, str(last_asked), last_user_content)

    # The user's answer to the spec question is filed here, before the model runs, so the model sees the updated draft and
    # nothing depends on it turning the answer into tool arguments.
    filing: Optional[chat_spec_filing.Filing] = None
    if (
        gating and not edit_ctx["snapshot"] and "update_rfq_draft" in allowed and not confirmation_turn and not lifecycle
        and not forced_create and not qualifier and not decline and not recap_reply and not is_counterparty_msg
        and (last_asked in ("specs", "hsn") or recap_awaiting == "create")
        and chat_draft_assist.product_name(draft) and not _asks_something(last_user_content)
    ):
        filing = chat_spec_filing.file_answer(
            draft, last_user_content, assist.suggestions.get("specs") or [],
            pending_value=state.get("spec_value_ask") if last_asked == "specs" else None,  # a bare number answers that question only
        )
        if filing.touched:
            logger.info("Filed spec answer: stored=%s must=%s ask_value=%s", filing.stored, filing.must, filing.ask_value)
        state_patch["spec_value_ask"] = None
    mark("before the draft helpers")
    # The helpers (HSN suggestion, spec suggestions, moderation) run while the model works; the reply waits for both.
    refresh_task: Optional["asyncio.Task[None]"] = asyncio.create_task(assist.refresh(json.loads(json.dumps(draft))))

    async def settle_refresh() -> None:
        nonlocal refresh_task
        task, refresh_task = refresh_task, None
        if task is not None:
            await task
            mark("draft helpers settled")

    readiness = rfq_tool_service.evaluate_rfq_readiness(draft)

    # One question at a time, most important first; None when this turn is not a drafting turn.
    drafting_turn = bool(
        gating and not edit_ctx["id"] and not lifecycle and not forced_create and not role_refused
        and "update_rfq_draft" in allowed and "create_rfq" not in allowed and not is_counterparty_msg
        and chat_permissions._drafting_in_progress(draft)
        and not chat_permissions.is_question(last_user_content)
        and (answers_specs or not chat_permissions.is_information_request(last_user_content))
    )

    def value_ask() -> Optional[dict[str, Any]]:
        """A numeric spec the user named without a usable value ("minimum Brix lael"): the next question asks for it."""
        ask = filing.ask_value if filing else None
        if ask and "specs" not in declined and asked.get(f"spec_value:{chat_draft_assist._norm_key(ask['label'])}", 0) < 1:
            return ask
        return None

    def pick_next() -> str:
        if value_ask():
            return "spec_value"
        return chat_conversation.next_question(
            draft, assist.suggestions, role=draft.get("role") or sole_role, declined=declined, asked=asked
        )

    window_messages, older_summary = build_sliding_window_messages(messages, max_history_turns=8)
    tool_cards: list[dict[str, Any]] = []
    cards_state: dict[str, list[dict[str, Any]]] = {"match_cards": []}
    verification_meta: Optional[dict[str, Any]] = None  # set once the answer engine has run

    # ---- event helpers -------------------------------------------------
    def emit(chunk: dict[str, Any]) -> dict[str, Any]:
        if active_conv_id:
            chunk["conversation_id"] = active_conv_id
        chunk.setdefault("routed_agent", agent_state["info"])
        shown = cards_state["match_cards"] or client_cands
        if shown and chunk.get("done") and "matched_candidates" not in chunk:
            chunk["matched_candidates"] = shown
        if chunk.get("done"):
            chunk["draft_suggestions"], chunk["draft_moderation"] = assist.public()
            chunk["editing_rfq_id"] = edit_ctx["id"]
        return chunk

    def wire(
        *,
        content: str = "",
        thinking: str = "",
        tool_step: Optional[dict[str, Any]] = None,
        created: Optional[dict[str, Any]] = None,
        cp_msg: Optional[dict[str, Any]] = None,
        done: bool = False,
        replace: bool = False,
        draft_now: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        d = draft if draft_now is None else draft_now
        chunk: dict[str, Any] = {"content": content}
        if replace:
            chunk["replace_content"] = True
        chunk.update({
            "thinking": thinking,
            "thinking_after": "",
            "tool_step": tool_step,
            "rfq_draft": d,
            "readiness": rfq_tool_service.evaluate_rfq_readiness(d) if draft_now is not None else readiness,
            "created_rfq": created,
            "counterparty_message": cp_msg,
            "done": done,
        })
        return emit(chunk)

    async def finish(
        *,
        kind: str = "final",
        reply: str,
        wire_content: Optional[str] = None,
        thinking: Optional[str] = None,
        tool_step: Optional[dict[str, Any]] = None,
        created: Optional[dict[str, Any]] = None,
        cp_msg: Optional[dict[str, Any]] = None,
        cards: Optional[list[dict[str, Any]]] = None,
    ) -> ChatEvent:
        """Persist the turn once and build its last event."""
        await settle_refresh()
        if kind != "error":
            extra = _reply_extras(assist, reply, draft, created)
            if extra:
                reply = f"{reply.rstrip()}\n\n{extra}" if reply.strip() else extra
                wire_content = f"{(wire_content or '').rstrip()}\n\n{extra}".lstrip() if wire_content is not None else None
        if not draft:
            assist.reset()  # the draft was submitted or never existed
        persisted = reply.strip() if kind == "error" else reply
        if db is not None and conversation is not None:
            try:
                await save_message_to_conversation(
                    db,
                    conversation.id,
                    role="assistant",
                    content=persisted,
                    metadata={
                        "thinking": thinking or None,
                        "thinking_after": None,
                        "tool_step": tool_step,
                        "created_rfq": created,
                        "counterparty_message": cp_msg,
                        "routed_agent": agent_state["info"],
                        **({"verification": verification_meta} if verification_meta else {}),
                    },
                )
                await update_conversation_after_turn(
                    db,
                    conversation,
                    draft=draft,
                    readiness=readiness,
                    rfq_id=uuid.UUID(created["id"]) if created and created.get("id") else None,
                    user_prompt=last_user_content,
                    matched_candidates=cards or matched_candidates,
                    active_connection_id=active_connection_id,
                    last_counterparty_message=cp_msg or last_counterparty_msg,
                    draft_suggestions=assist.suggestions,
                    draft_moderation=assist.moderation or {},
                    state_patch=state_patch,
                )
            except Exception as exc:
                logger.exception("Failed to persist turn: %s", exc)
        mark("reply persisted")
        chunk = wire(
            content=reply if wire_content is None else wire_content,
            tool_step=tool_step, created=created, cp_msg=cp_msg, done=True,
        )
        result = TurnResult(
            reply=persisted, thinking=thinking or None, rfq_draft=draft, readiness=readiness,
            created_rfq=created, counterparty_message=cp_msg,
            conversation_id=conversation.id if conversation else None, routed_agent=agent_state["info"],
            matched_candidates=(cards_state["match_cards"] or client_cands) or None,
            draft_suggestions=chunk.get("draft_suggestions"), draft_moderation=chunk.get("draft_moderation"),
            editing_rfq_id=edit_ctx["id"],
        )
        return ChatEvent(kind, chunk, result)

    def create_step(created: Optional[dict[str, Any]], err: Optional[str]) -> dict[str, Any]:
        return {
            "name": "create_rfq",
            "title": "Creating Marketplace RFQ",
            "status": "completed" if created else "failed",
            "args": {"title": created["title"], "id": created["id"]} if created else {"error": err},
        }

    # ---- prompt --------------------------------------------------------
    dynamic_system_prompt = build_rfq_system_prompt(
        draft,
        readiness,
        matched_candidates=matched_candidates,
        last_counterparty_message=last_counterparty_msg,
        active_connection_id=active_connection_id,
        allowed_tools=allowed,
        routed_agent=routed_agent,
        older_history_summary=older_summary,
        counterparty_context=counterparty_context,
        user=user,
        draft_helpers=assist.prompt_section(draft, flow_asks=drafting_turn),
        editing_title=str((editing_snapshot or {}).get("title") or "") if editing_id else None,
        ack_only=drafting_turn,
    )
    prepared_messages: list[dict[str, Any]] = [{"role": "system", "content": dynamic_system_prompt}, *window_messages]

    if len(prepared_messages) == 1:
        yield await finish(reply=_HELLO_REPLY)
        return

    if (
        lifecycle or "create_rfq" in allowed or "close_rfq" in allowed
        or chat_conversation.asks_for_specs(last_user_content) or chat_specs.wants_to_refine(last_user_content)
    ):
        await settle_refresh()  # the shortcuts below read the suggestions; only the answer-engine path overlaps the helpers

    # ---- explicit-command shortcuts (create / close) --------------------
    # ---- lifecycle shortcuts: publish a saved draft, edit a saved RFQ ----------
    kind = lifecycle["kind"] if lifecycle else None
    if kind == "reply":
        yield await finish(reply=f"⚠️ {lifecycle['reply']}")
        return

    if kind == "publish":
        agent_state["info"] = agent_info(agent_for_tools(["publish_rfq"]) or routed_agent)
        published, err = await execute_publish_rfq_call(
            rfq_id_str=lifecycle.get("target"), conversation=conversation, db=db, user=user
        )
        step = {
            "name": "publish_rfq",
            "title": "Publishing Marketplace RFQ",
            "status": "completed" if published else "failed",
            "args": {"title": published["title"], "id": published["id"]} if published else {"error": err},
        }
        reply = f"⚠️ {err or 'Could not publish the RFQ.'}" if not published else _published_text(published)
        yield await finish(reply=reply, tool_step=step, created=published)
        return

    if (
        gating and not lifecycle and not edit_ctx["id"] and "create_rfq" not in allowed and not is_counterparty_msg
        and chat_draft_assist.product_name(draft) and chat_conversation.asks_for_specs(last_user_content)
    ):
        # "what specs can I add": answered from the stored suggestions (with their options), not by a model essay.
        if assist.specs_pending(draft):
            await assist.settle_specs(draft)
        listing = chat_conversation.specs_listing(draft, assist.suggestions, lang)
        if listing:
            asked["specs"] = max(1, asked.get("specs", 0))
            state_patch.update(asked_fields=dict(asked), last_asked="specs")
            yield await finish(reply=listing)
            return

    if (
        gating and not lifecycle and not edit_ctx["id"] and "create_rfq" not in allowed and not is_counterparty_msg
        and chat_draft_assist.product_name(draft) and chat_specs.wants_to_refine(last_user_content)
    ):
        # "continue refining it" / "add more details": the answer is the spec question (never the recap, never a model choice).
        if assist.specs_pending(draft):
            await assist.settle_specs(draft)
        if chat_conversation.unfilled_specs(draft, assist.suggestions):
            asked["specs"] = 1
            state_patch.update(asked_fields=dict(asked), last_asked="specs", recap_hash=None, recap_awaiting=None)
            yield await finish(reply=chat_conversation.question_text(
                "specs", {**draft, "role": draft.get("role") or sole_role}, assist.suggestions, lang
            ))
            return

    if kind == "recap_no":
        state_patch.update(recap_hash=None, recap_awaiting=None, recap_snapshot=None)
        if edit_ctx["id"]:
            reply = (
                "Theek hai, kuch save nahi kiya. Aap kya badalna chahte hain?" if lang == "hinglish"
                else "No problem, nothing was saved. What would you like to change?"
            )
        else:
            reply = (
                "Theek hai, maine kuch create nahi kiya. Aap kya badalna chahte hain?" if lang == "hinglish"
                else "No problem, I haven't created anything. What would you like to change?"
            )
        yield await finish(reply=reply)
        return

    if kind == "cancel":
        title = str((edit_ctx["snapshot"] or {}).get("title") or "the RFQ")
        draft.clear()
        readiness = rfq_tool_service.evaluate_rfq_readiness(draft)
        state_patch.update(editing_rfq_id=None, editing_snapshot=None)
        edit_ctx.update(id=None, snapshot=None)
        yield await finish(reply=f"Edit cancelled. No changes were saved to '{title}'.")
        return

    if kind == "save":
        agent_state["info"] = agent_info(agent_for_tools(["edit_rfq"]) or routed_agent)
        snap = edit_ctx["snapshot"] or {}
        changed_now = list(chat_rfq_edit.changed_fields(draft, snap))
        new_hash = chat_conversation.draft_hash(draft)
        # Saving needs a confirmed recap of the changes, unless the same message states the change and says save.
        confirmed = (
            not gating
            or chat_permissions.edit_command_has_change(last_user_content)
            or (recap_awaiting == "edit" and recap_hash == new_hash)
        )
        problem = False
        if changed_now and not confirmed:  # an invalid or blocked change is reported now, not after a recap
            try:
                chat_rfq_edit.build_rfq_patch(draft, snap)
            except rfq_tool_service.RFQDraftInvalid:
                problem = True
            problem = problem or bool(await assist.block_reason(draft))
        if changed_now and not confirmed and not problem:
            wants_publish = bool(lifecycle.get("publish"))
            state_patch.update(recap_hash=new_hash, recap_awaiting="edit", recap_publish=wants_publish or None)
            yield await finish(reply=chat_conversation.build_edit_recap(draft, snap, changed_now, lang, publish=wants_publish))
            return
        publish_after = bool(lifecycle.get("publish")) or bool(
            gating and recap_awaiting == "edit" and recap_hash == new_hash and state.get("recap_publish")
        )
        reply, step, result, done, _ok = await _save_edit_flow(
            draft, edit_ctx["snapshot"] or {}, str(edit_ctx["id"]), publish_after,
            conversation, db, user, assist,
        )
        if done:
            draft.clear()
            readiness = rfq_tool_service.evaluate_rfq_readiness(draft)
            state_patch.update(editing_rfq_id=None, editing_snapshot=None, **_CONVO_RESET)
            edit_ctx.update(id=None, snapshot=None)
        yield await finish(reply=reply, tool_step=step, created=result)
        return

    if kind == "edit_load":
        agent_state["info"] = agent_info(agent_for_tools(["edit_rfq"]) or routed_agent)
        step = {
            "name": "edit_rfq",
            "title": "Loading RFQ for Editing",
            "status": "completed",
            "args": {"title": edit_loaded_rfq.title, "id": str(edit_loaded_rfq.id)},
        }
        if "update_rfq_draft" in allowed:
            yield ChatEvent("tool_step", wire(tool_step=step))
        else:
            reply = (
                f"✏️ **Editing '{edit_loaded_rfq.title}'** ({edit_loaded_rfq.status.value}). "
                f"Current: {_edit_summary(draft)}.\n\n"
                "Tell me what to change, then say \"save changes\" (or \"cancel edit\" to discard)."
            )
            if edit_note in assist.notes:
                assist.notes.remove(edit_note)
            yield await finish(reply=reply, tool_step=step)
            return

    if "create_rfq" in allowed:
        agent_state["info"] = agent_info(agent_for_tools(["create_rfq"]) or routed_agent)
        explicit_draft = recap_reply == "draft" or chat_permissions.wants_draft_status(last_user_content)
        # The user must have seen what will be posted: the recap of this exact draft, or an explicit "save as draft".
        # A bare "yes" confirms only the draft the recap showed (the snapshot); if the draft is not that, recap again.
        draft_is_recapped = recap_awaiting == "create" and recap_hash == chat_conversation.draft_hash(draft)
        confirmed = not gating or explicit_draft or draft_is_recapped
        if forced_create and not draft_is_recapped:
            logger.info("Draft differs from the recap it was confirmed against; showing a fresh recap")
        if (
            readiness["has_role"] and readiness["has_product"] and not confirmed
            and not await assist.block_reason(draft)  # unsafe content is refused by the create call, not recapped
        ):
            auto = state.get("auto_role") or state_patch.get("auto_role")
            note = auto if auto and not state.get("auto_role_noted") else None
            state_patch.update(
                recap_hash=chat_conversation.draft_hash(draft), recap_awaiting="create", last_asked=None,
                recap_snapshot=json.loads(json.dumps(draft)),
                **({"auto_role_noted": True} if note else {}),
            )
            yield await finish(reply=chat_conversation.build_recap(draft, lang, role_note=note))
            return
        if readiness["has_role"] and readiness["has_product"]:
            yield ChatEvent("status", wire(
                thinking="User requested to finalize and publish RFQ. Validating required fields and submitting listing to marketplace database..."
            ))
            create_args = {"status": "draft"} if explicit_draft else {}
            created, err = await execute_create_rfq_call(draft, create_args, db=db, user=user, assist=assist)
            step = create_step(created, err)
            yield ChatEvent("tool_step", wire(tool_step=step, created=created))
            reply = format_created_rfq_text(created) if created else f"⚠️ {err or 'Failed to create RFQ.'}"
            if created:
                state_patch["last_created_rfq_id"] = created["id"]
                state_patch.update(last_created_rfq_title=created.get("title"), last_created_rfq_status=created.get("status"))
                state_patch.update(_CONVO_RESET)
                draft.clear()  # the RFQ exists now; a second "create" must start from a fresh draft
                readiness = rfq_tool_service.evaluate_rfq_readiness(draft)
            yield await finish(
                reply=reply, tool_step=step, created=created,
                thinking="Validated required fields and submitted RFQ to marketplace database.",
            )
        elif (
            gating and state.get("last_created_rfq_id") and state.get("last_created_rfq_status") != "draft"
            and not chat_draft_assist.product_name(draft)
        ):
            # "yes publish it" after the RFQ was created in this conversation: it exists; never ask for a role and product.
            yield await finish(reply=chat_reply.already_published_reply(
                str(state.get("last_created_rfq_title") or ""), str(state["last_created_rfq_id"]), lang
            ))
        else:
            missing = []
            if not readiness["has_role"]:
                missing.append("Role (Buyer or Seller)")
            if not readiness["has_product"]:
                missing.append("Product name")
            yield await finish(
                reply=f"To create your RFQ, we still need {' and '.join(missing)}. Are you looking to BUY or SELL goods, and what product?"
            )
        return

    if "close_rfq" in allowed:
        agent_state["info"] = agent_info(agent_for_tools(["close_rfq"]) or routed_agent)
        closed, err = await execute_close_rfq_call(
            rfq_id_str=chat_permissions.close_rfq_target(last_user_content),
            conversation=conversation, db=db, user=user, require_unambiguous=True,
        )
        step = {
            "name": "close_rfq",
            "title": "Closing Marketplace RFQ",
            "status": "completed" if closed else "failed",
            "args": {"title": closed["title"], "id": closed["id"]} if closed else {"error": err},
        }
        if closed:
            reply = (
                f"✅ **RFQ Closed:** '{closed['title']}' (ID: `{closed['id']}`) has been successfully closed. "
                "It is no longer active in the marketplace and will not appear in match searches."
            )
        else:
            reply = f"⚠️ {err or 'Could not close the RFQ.'}"
        yield await finish(reply=reply, tool_step=step, created=closed)
        return

    def conversation_tail(reply_text: str) -> str:
        """The deterministic end of a drafting reply: the one next question, or the recap once everything is asked."""
        if not drafting_turn or role_refused or not draft:
            return ""
        field = pick_next()
        if field == "recap":
            if assist.current_warning(draft):
                return ""
            h = chat_conversation.draft_hash(draft)
            if recap_awaiting == "create" and recap_hash == h:
                return "" if "?" in reply_text else chat_conversation.recap_question(lang)
            auto = state.get("auto_role") or state_patch.get("auto_role")
            note = auto if auto and not state.get("auto_role_noted") else None
            state_patch.update(
                recap_hash=h, recap_awaiting="create", last_asked=None, asked_fields=dict(asked), spec_value_ask=None,
                recap_snapshot=json.loads(json.dumps(draft)),
                **({"auto_role_noted": True} if note else {}),
            )
            return chat_conversation.build_recap(draft, lang, role_note=note)
        def hsn_offer() -> str:
            """The suggested HSN, offered in one line after a spec question; twice at most, never once applied or declined."""
            hsn = assist.pending_hsn
            if (
                not hsn or chat_draft_assist.user_hsn(draft) or "hsn" in declined
                or assist.suggestions.get("hsn_for") != chat_draft_assist.product_name(draft)
                or int(assist.suggestions.get("hsn_offers") or 0) >= MAX_HSN_OFFERS or str(hsn["code"]) in reply_text
            ):
                return ""
            assist.suggestions["hsn_offers"] = int(assist.suggestions.get("hsn_offers") or 0) + 1
            assist.suggestions["hsn_offered"] = True
            return chat_reply.hsn_line(hsn, lang)

        if field == "spec_value":
            ask = value_ask() or {}
            asked[f"spec_value:{chat_draft_assist._norm_key(ask['label'])}"] = 1
            state_patch.update(asked_fields=dict(asked), last_asked="specs", spec_value_ask=ask)
            return chat_reply.join_blocks(chat_reply.spec_value_question(ask, lang), hsn_offer())
        asked[field] = asked.get(field, 0) + 1
        state_patch.update(asked_fields=dict(asked), last_asked=field)
        question = chat_conversation.question_text(
            field, {**draft, "role": draft.get("role") or sole_role}, assist.suggestions, lang
        )
        if question in reply_text:
            return hsn_offer() if field == "specs" else ""
        pending_code = str((assist.pending_hsn or {}).get("code") or "")
        if field == "hsn" and pending_code and pending_code in reply_text:
            return ""  # the model already made the HSN offer
        if field == "hsn":
            assist.suggestions["hsn_offers"] = max(1, int(assist.suggestions.get("hsn_offers") or 0))
        if field == "specs":
            return chat_reply.join_blocks(question, hsn_offer())  # the spec question follows, with the HSN offer after it
        return "" if "?" in reply_text else question

    def fallback_closing(d: dict[str, Any]) -> Optional[str]:
        """The drafting fallback's last line follows the conversation: the next question, or "" when the recap is appended."""
        if not drafting_turn or role_refused:
            return None
        field = "spec_value" if value_ask() else chat_conversation.next_question(
            d, assist.suggestions, role=d.get("role") or sole_role, declined=declined, asked=asked
        )
        if field in ("recap", "spec_value", "specs"):
            return ""  # the recap, the value question or the spec question is appended after the reply, once
        return chat_conversation.question_text(field, {**d, "role": d.get("role") or sole_role}, assist.suggestions, lang)

    # ---- answer: read tools + allowed action tools ----------------------
    captured_tool_step: Optional[dict[str, Any]] = None
    try:
        queue: asyncio.Queue = asyncio.Queue()
        pre_turn_draft = json.loads(json.dumps(draft))  # what the verifier may take draft values from
        runner = _ActionRunner(
            draft=draft,
            draft_guards=_draft_guards(messages, last_user_content, draft, earlier_user_text),
            db=db,
            user=user,
            conversation=conversation,
            matched_candidates=matched_candidates,
            active_connection_id=active_connection_id,
            connection_hint=_connection_hint(last_user_content, focus),
            on_step=queue.put_nowait,
            assist=assist,
        )

        def status(text: str) -> ChatEvent:
            return ChatEvent("status", wire(thinking=text, draft_now=runner.draft))

        yield status("Looking up verified marketplace data...\n")
        task = asyncio.create_task(
            answer_engine.answer(
                db,
                user,
                history_messages=window_messages,
                system_prompt=dynamic_system_prompt,
                user_message=last_user_content,
                extra_tools=_action_tool_specs(allowed),
                tool_executor=runner,
                context=AnswerContext(
                    rfq_id=str(rfq_id) if rfq_id else None,
                    draft=lambda: runner.draft,
                    # read at verification time: the suggestions may arrive while the model is still working
                    extra_allowed_text=lambda: "\n".join(
                        t for t in (
                            _engine_allowed_text(matched_candidates, counterparty_context, last_counterparty_msg),
                            assist.extra_allowed_text(runner.draft),
                        ) if t
                    ),
                    on_progress=lambda event: queue.put_nowait({"__progress": event}),
                    drafting="update_rfq_draft" in allowed and (
                        answers_question
                        or chat_permissions.effective_intent(last_user_content, parsed_intent) in chat_permissions.DRAFTING_INTENTS
                    ),
                    verify_draft=pre_turn_draft,
                    drafting_closing=fallback_closing,
                    before_verify=settle_refresh,
                    tool_round_only=drafting_turn,
                ),
            )
        )
        try:
            while not task.done() or not queue.empty():
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                if "__progress" in item:
                    event = item["__progress"]
                    if event.get("event") == "verifying":
                        yield status("Checking every number against the data...\n")
                    elif event.get("name") in answer_engine.market_data_tools.READ_TOOL_NAMES:
                        yield status(f"Looking up: {str(event['name']).replace('_', ' ')}\n")
                    continue
                yield ChatEvent("tool_step", wire(
                    tool_step=item.get("tool_step"), created=item.get("created_rfq"),
                    cp_msg=item.get("counterparty_message"), draft_now=runner.draft,
                ))
        finally:
            if not task.done():
                task.cancel()
        engine_result = task.result()
        mark("answer engine done")

        draft = runner.draft
        await settle_refresh()
        lock_role()
        chat_draft_assist.must_match_from_text(draft, last_user_content)
        chat_draft_assist.apply_spec_suggestions(draft, assist.suggestions.get("specs"))
        if filing is not None:
            # The model must not repeat a filed spec under another key, or restate a value the next question asks for.
            dropped_specs = chat_spec_filing.prune_model_specs(draft, filing, set((pre_turn_draft.get("product_details") or {})))
            if dropped_specs:
                logger.info("Dropped model-added specs that repeat the filed answer: %s", dropped_specs)
        enforce_account_role()  # the model may have recorded a role this account cannot post
        if not runner.created_rfq_obj:
            await assist.refresh(draft)  # a product or text the model just recorded
        readiness = rfq_tool_service.evaluate_rfq_readiness(draft)
        if drafting_turn and not role_refused and draft and pick_next() in ("hsn", "recap") and assist.specs_pending(draft):
            await assist.settle_specs(draft)  # the spec question comes before the recap
        if drafting_turn and not role_refused and draft and assist.hsn_pending(draft) and pick_next() in ("specs", "spec_value", "hsn", "recap"):
            await assist.settle_hsn(draft)  # the reply offers the HSN: give a running fetch its time
        mark("post-engine helpers / spec settle done")
        created_rfq_obj = runner.created_rfq_obj
        counterparty_msg_obj = runner.counterparty_msg_obj
        captured_tool_step = runner.captured_tool_step
        total_content = engine_result.text
        thinking = engine_result.thinking or None
        tool_cards = _cards_from_tool_results(engine_result.tool_results)
        verification_meta = dict(engine_result.diagnostics or {})
        cards_state["match_cards"] = await _match_payloads(db, user, engine_result.tool_results)

        # The badge follows the tools the assistant really used.
        used = [r.get("tool") for r in engine_result.tool_results] + [a.get("tool") for a in engine_result.action_results]
        used_agent = agent_for_tools(t for t in used if t)
        if used_agent is not None:
            agent_state["info"] = agent_info(used_agent)

        if is_counterparty_msg and engine_result.used_fallback and not engine_result.tool_results:
            total_content = ""  # a letter is not a data answer; the composer below builds it

        if engine_result.llm_error and not engine_result.tool_results and not runner.touched:
            yield await finish(kind="error", reply=_CONNECTION_LOST)
            return

        if runner.err_reply and (not total_content or runner.duplicate):
            total_content = runner.err_reply
        if runner.draft_msg_fallback and not total_content:
            draft_msg_text, draft_err = runner.draft_msg_fallback
            total_content = f"📝 **Negotiation Message Draft:**\n\n> {draft_msg_text}\n\n⚠️ *Note: {draft_err}*"

        # Guardrail recovery: the model refused a legitimate business request.
        if is_false_positive_refusal(total_content, last_user_content):
            total_content = await _recover_from_false_refusal(prepared_messages, last_user_content, call_model=True) or ""

        # Never let the model announce an RFQ that was not actually created (published, posted, saved).
        claim_replaced = False
        if not created_rfq_obj and (
            claims_rfq_created(total_content) or (edit_ctx["id"] and claims_rfq_saved(total_content))
        ):
            logger.warning("Model claimed RFQ creation or a save without the action; correcting reply.")
            total_content = unbacked_creation_reply(draft, readiness, editing=bool(edit_ctx["id"]))
            claim_replaced = True

        # The model may skip the message tool; compose the letter deterministically.
        if not counterparty_msg_obj and (is_counterparty_msg or _DISPATCH_REPLY_RE.search(total_content)):
            msg_to_send = extract_or_compose_counterparty_message(
                reply_text=total_content, user_instruction=last_user_content,
                last_counterparty_info=last_counterparty_msg, draft=draft,
            )
            recipient_name = extract_recipient_name(
                reply_text=total_content, last_counterparty_info=last_counterparty_msg,
                matched_candidates=matched_candidates or tool_cards,
            )
            counterparty_msg_obj, _err = await execute_draft_counterparty_message_call(
                tool_args={"message": msg_to_send, "recipient_name": recipient_name},
                db=db,
                user=user,
                matched_candidates=matched_candidates or tool_cards,
                active_connection_id=active_connection_id,
                connection_hint=_connection_hint(last_user_content, focus),
            )
            if counterparty_msg_obj:
                c_name = counterparty_msg_obj.get("counterparty_name", recipient_name)
                captured_tool_step = {
                    "name": "draft_counterparty_message",
                    "title": f"Drafting Message for {c_name}",
                    "status": "completed",
                    "args": {"recipient": c_name, "message": counterparty_msg_obj["message"]},
                }
                agent_state["info"] = agent_info(agent_for_tools(["draft_counterparty_message"]) or routed_agent)
                yield ChatEvent("tool_step", wire(tool_step=captured_tool_step, cp_msg=counterparty_msg_obj))

        # Preprocessing may have captured fields the model did not record through the tool.
        if (
            "update_rfq_draft" in allowed and not counterparty_msg_obj and not captured_tool_step
            and draft != (current_rfq or {})
        ):
            extracted_args = {
                k: draft[k]
                for k in ("role", "category", "product_details", "quantity", "price_target", "location", "deadline")
                if draft.get(k) and draft.get(k) != (current_rfq or {}).get(k)
            }
            if extracted_args:
                captured_tool_step = {
                    "name": "update_rfq_draft",
                    "title": "Updating RFQ Preferences & Draft",
                    "status": "completed",
                    "args": extracted_args,
                }
                agent_state["info"] = agent_info(agent_for_tools(["update_rfq_draft"]) or routed_agent)
                yield ChatEvent("tool_step", wire(tool_step=captured_tool_step))

        # The tool argument can be cut off mid-sentence; the reply may hold the whole letter.
        if counterparty_msg_obj and is_truncated_message(counterparty_msg_obj.get("message", "")):
            complete_letter = extract_or_compose_counterparty_message(
                reply_text=total_content, user_instruction=last_user_content,
                last_counterparty_info=last_counterparty_msg, draft=draft,
            )
            if complete_letter and not is_truncated_message(complete_letter):
                counterparty_msg_obj["message"] = complete_letter
                if counterparty_msg_obj.get("message_id") and db:
                    try:
                        from models.connection import ConnectionMessage

                        db_msg = await db.get(ConnectionMessage, uuid.UUID(str(counterparty_msg_obj["message_id"])))
                        if db_msg:
                            db_msg.content = complete_letter
                            await db.commit()
                    except Exception as exc:
                        logger.warning("Could not update truncated message in DB: %s", exc)

        if not total_content.strip() and thinking and not counterparty_msg_obj and not created_rfq_obj and not drafting_turn:
            total_content, thinking = thinking, None
        if total_content.strip() and not created_rfq_obj and not counterparty_msg_obj:
            total_content = _contradiction_free(total_content, assist, draft)
            if drafting_turn and not role_refused and draft:
                total_content = _drop_invented_choices(total_content, assist)
        if drafting_turn and not role_refused and draft and not created_rfq_obj and not counterparty_msg_obj:
            # What the user reads about the draft comes from the draft, not from the model: its "RFQ draft saved/updated"
            # opening and its own spec list are replaced by the real diff and the flow's one question.
            total_content, _claimed = chat_reply.strip_draft_claims(total_content)
            total_content = chat_reply.strip_spec_echo(
                total_content, assist.suggestions.get("specs"), drop_lists=pick_next() in ("specs", "spec_value")
            )
            if assist.pending_hsn and not chat_draft_assist.user_hsn(draft):
                total_content = chat_reply.drop_unapplied_hsn_claims(total_content)
            total_content = chat_reply.join_blocks(
                chat_reply.build_ack(turn_start_draft, draft, lang), chat_reply.limit_model_text(total_content)
            )
        assist.note_reply(total_content)

        if total_content.strip() and not created_rfq_obj:
            yield ChatEvent("replace_content", wire(content=total_content, replace=True))

        if counterparty_msg_obj:
            shown = "" if total_content.strip() else format_counterparty_message_text(counterparty_msg_obj)
            yield await finish(
                reply=total_content if total_content.strip() else shown, wire_content=shown, thinking=thinking,
                tool_step=captured_tool_step, cp_msg=counterparty_msg_obj,
            )
        elif created_rfq_obj:
            state_patch["last_created_rfq_id"] = created_rfq_obj["id"]
            state_patch.update(
                last_created_rfq_title=created_rfq_obj.get("title"), last_created_rfq_status=created_rfq_obj.get("status")
            )
            state_patch.update(_CONVO_RESET)
            draft.clear()
            readiness = rfq_tool_service.evaluate_rfq_readiness(draft)
            yield await finish(
                reply=format_created_rfq_text(created_rfq_obj), thinking=thinking,
                tool_step=captured_tool_step, created=created_rfq_obj,
            )
        elif total_content.strip():
            appendix = ""
            wants_links = any(c for c in tool_cards) and (
                any(r.get("tool") == "find_matches" for r in engine_result.tool_results)
                or chat_permissions.effective_intent(last_user_content, parsed_intent) == "search"
            )
            if wants_links and "/marketplace?rfq=" not in total_content and not total_content.startswith("⚠️"):
                appendix = build_rfq_link_appendix(tool_cards)
            tail = conversation_tail(total_content)
            if not tail and claim_replaced and not edit_ctx["id"]:
                tail = format_rfq_summary_text(draft, readiness)  # the true state, when no next question or recap applies
            if tail:
                assist.note_reply(tail)
                appendix = f"{appendix}\n\n{tail}" if appendix else f"\n\n{tail}"
            yield await finish(
                reply=total_content + appendix, wire_content=appendix, thinking=thinking,
                tool_step=captured_tool_step, cards=cards_state["match_cards"] or tool_cards or None,
            )
        else:
            tail = conversation_tail("")
            if tail:
                assist.note_reply(tail)
                ack = "Theek hai, note kar liya." if lang == "hinglish" else "Noted."
                summary = f"{ack}\n\n{tail}"
            else:
                summary = format_rfq_summary_text(draft, readiness)
            yield await finish(reply=summary, thinking=thinking, tool_step=captured_tool_step)
    except httpx.TimeoutException:
        yield await finish(kind="error", reply=_TIMED_OUT, tool_step=captured_tool_step)
    except Exception as exc:
        logger.error("Chat turn failed: %s", exc)
        yield await finish(kind="error", reply=_CONNECTION_LOST, tool_step=captured_tool_step)


_NO_HSN_CLAIM_RE = re.compile(
    r"\bno\s+hsn(?:\s+code)?\b|\bhsn(?:\s+code)?\s+(?:was\s+|is\s+)?not\s+found\b|\b(?:could\s*n'?t|can\s*not|can'?t|did\s*n'?t)\s+(?:find|locate)\s+(?:an?\s+)?hsn\b",
    re.IGNORECASE,
)
_CHOICE_QUESTION_RE = re.compile(
    r"\b(publish|post|refin\w*|proceed|continue|create|submit|finali[sz]e|add\s+more|ready\s+to|"
    r"would\s+you\s+like|do\s+you\s+want|shall\s+i|want\s+me\s+to)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _contradiction_free(reply: str, assist: chat_draft_assist.DraftAssist, draft: dict[str, Any]) -> str:
    """Drop "no HSN code found" while a suggestion exists or an HSN is applied: a read tool coming back empty does not undo it."""
    if not (assist.pending_hsn or chat_draft_assist.user_hsn(draft)) or not _NO_HSN_CLAIM_RE.search(reply):
        return reply
    logger.info("Dropped a 'no HSN found' claim that contradicts the suggested/applied HSN")
    lines = []
    for line in reply.split("\n"):
        kept = [sent for sent in _SENTENCE_SPLIT_RE.split(line) if not _NO_HSN_CLAIM_RE.search(sent)]
        if kept:
            lines.append(" ".join(kept))
    return "\n".join(lines).strip()


def _drop_invented_choices(reply: str, assist: chat_draft_assist.DraftAssist) -> str:
    """Remove model-made closing choices ("Ready to publish or continue refining?") from a drafting reply.

    Questions that carry the pending HSN offer stay; the flow's own next question or recap is appended after.
    """
    code = str((assist.pending_hsn or {}).get("code") or "")
    lines = []
    for line in reply.split("\n"):
        kept = []
        for sent in _SENTENCE_SPLIT_RE.split(line):
            if sent.rstrip().endswith("?") and _CHOICE_QUESTION_RE.search(sent) and not (code and code in sent):
                logger.info("Dropped a model-invented question from a drafting reply: %r", sent[:80])
                continue
            kept.append(sent)
        if kept:
            lines.append(" ".join(kept))
    return "\n".join(lines).strip()


async def generate_business_chat_reply(
    messages: list[Any],
    current_rfq: Optional[dict[str, Any]] = None,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    conversation_id: Optional[uuid.UUID] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
    agent: Optional[AgentDefinition] = None,
    rfq_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    """Run one chat turn and return the finished response (the /message endpoint)."""
    result: Optional[TurnResult] = None
    async for event in _run_chat_turn(
        messages, current_rfq, db, user, conversation_id, matched_candidates, active_connection_id, agent, rfq_id
    ):
        if event.result is not None:
            result = event.result
    if result is None:  # the turn always ends with a final or error event
        raise RuntimeError("chat turn ended without a result")
    return result.as_response()


async def stream_business_chat_reply(
    messages: list[Any],
    current_rfq: Optional[dict[str, Any]] = None,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    conversation_id: Optional[uuid.UUID] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
    agent: Optional[AgentDefinition] = None,
    rfq_id: Optional[uuid.UUID] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one chat turn and yield its SSE chunks (the /stream endpoint)."""
    async for event in _run_chat_turn(
        messages, current_rfq, db, user, conversation_id, matched_candidates, active_connection_id, agent, rfq_id
    ):
        yield event.chunk

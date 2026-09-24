"""AI Conversational Business Assistant service powered by local Ollama gpt-oss:20b.

Enforces strict B2B business-only guardrails for procurement, RFQs,
supplier negotiation, pricing, logistics, and marketplace transactions.
Integrates rfq_tool_service for structured multi-field preprocessing
and LLM tool calling.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Optional, Union
import uuid

import httpx
from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.conversation import Conversation, Message
from models.enums import ConversationType, MessageRole
from models.user import User
from services import rfq_tool_service
from services.agent_registry import AgentDefinition

logger = logging.getLogger(__name__)

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

STRICT POLICY:
If the query is not about business, trade, or procurement:
Respond with ONLY: "I only assist with business, procurement, and B2B trade inquiries."
"""


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


def build_rfq_system_prompt(
    draft: dict[str, Any],
    readiness: dict[str, Any],
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    last_counterparty_message: Optional[dict[str, Any]] = None,
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
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

    status_str = "\n".join(status_lines)

    missing_opts = []
    if not draft.get("quantity"):
        missing_opts.append("quantity")
    if not draft.get("price_target"):
        missing_opts.append("target budget/price")
    if not draft.get("location") or not (draft.get("location") or {}).get("city"):
        missing_opts.append("delivery location")
    if not draft.get("deadline"):
        missing_opts.append("delivery deadline")

    # Sequence directive
    if not readiness["has_role"]:
        flow_directive = (
            "1. STEP 1 (REQUIRED): Role is missing. Ask the user in ONE brief sentence whether they want to BUY or SELL goods."
        )
    elif not readiness["has_product"]:
        flow_directive = (
            f"2. STEP 2 (REQUIRED): Role is {draft.get('role', '').upper()}. Now ask what specific PRODUCT they want to buy/sell. Keep it short."
        )
    elif missing_opts:
        flow_directive = (
            f"3. STEP 3 (REQUIRED COMPLETE): Role and Product are recorded. "
            f"Captured optional details: {'; '.join(opts) if opts else 'None yet'}. "
            f"Still missing optional fields: {', '.join(missing_opts)}. "
            f"Acknowledge the captured fields and ask if the user wants to specify {', '.join(missing_opts)}, or proceed to create the RFQ."
        )
    else:
        flow_directive = (
            "3. STEP 3 (ALL KEY DETAILS CAPTURED): All required and key commercial terms "
            "(Role, Product, Quantity, Price Target, Location, Deadline) are captured! "
            "Acknowledge the complete RFQ draft concisely, and ask if they have any technical specifications or if they are ready to proceed to create the RFQ."
        )

    matches_section = ""
    if matched_candidates:
        match_lines = [
            f"\nCURRENT LOADED MATCHED COUNTERPARTIES ({len(matched_candidates)} candidates displayed in user's matching cart):"
        ]
        for idx, c in enumerate(matched_candidates):
            rank = c.get("rank") or (idx + 1)
            cp = c.get("counterparty") if isinstance(c.get("counterparty"), dict) else {}
            company = (
                cp.get("company_name")
                or cp.get("name")
                or c.get("company_name")
                or c.get("name")
                or f"Supplier/Buyer #{rank}"
            )
            email = cp.get("email") or c.get("email") or "Not provided"
            phone = cp.get("phone") or c.get("phone") or "Not provided"

            # Score extraction
            score_data = c.get("score")
            if isinstance(score_data, dict):
                total_score = score_data.get("total")
            elif isinstance(score_data, (int, float)):
                total_score = score_data
            else:
                total_score = c.get("match_score") or c.get("total_score")
            total_pct = f"{round(total_score * 100)}%" if total_score is not None else "N/A"
            tier = "Prime Match" if (total_score or 0) >= 0.85 else ("Strong Fit" if (total_score or 0) >= 0.7 else "Viable")

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
            title = c.get("title") or company

            match_lines.append(
                f"- Candidate #{rank}: Company '{company}' | Title: '{title}' | Match Score: {total_pct} ({tier}) | "
                f"Email: {email} | Phone: {phone} | Price: {price_str} | Quantity: {qty_str} | "
                f"Location: {loc_str} (Distance: {dist}) | Rating: {rating_str} | Status: {gst}"
            )

        match_lines.extend([
            "",
            "MATCH QUERY DIRECTIVES:",
            "- The user has loaded and is looking at these matched counterparties in their inline matching cart.",
            "- When the user asks for emails (e.g. 'give me mail id of top three', 'show emails', 'contact details'):",
            "  * Provide a clear list of the requested candidates (by rank and company name) along with their exact email addresses.",
            "  * If an email is 'Not provided', state that transparently.",
            "- When the user asks which candidate suits best (e.g. 'which one suits best from this top three', 'who is the best match'):",
            "  * Compare the top candidates on match score %, price competitiveness, location proximity, and trust credentials (GST verification, rating).",
            "  * Give a clear, decisive recommendation for the candidate that offers the best balance of quality, pricing, and suitability, explaining the exact reasons.",
            "- When the user asks to compare (e.g. 'compare prices', 'compare ratings'): provide a concise comparison breakdown.",
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
            "    1. You MUST call the `send_counterparty_message` tool to dispatch the updated/amended message to the counterparty!\n"
            "    2. NEVER call `update_rfq_draft` with amendment text phrases (e.g. NEVER set product to 'aslo mantion location')!\n"
            "    3. In your amended message, preserve the polite quotation context and accurately integrate the user's specific addition or correction without hallucinating unrelated distant cities or terms.\n"
            "    4. Do not invent cities like Nagpur when user only specifies a distance from Indore; state '10 km outside Indore'.\n"
        )

    negotiation_directive = (
        "\nNEGOTIATION & COUNTERPARTY MESSAGING DIRECTIVES:\n"
        "- When the user instructs to send a message to a seller or negotiate (e.g. 'send nagitiation message to that user', 'message the supplier', 'ask for discount', 'negotiate with Himalaya Orchards'):\n"
        "  * ALWAYS adopt an exceptionally polite, courteous, and commercially persuasive tone on behalf of the user.\n"
        "  * Formulate a complete, polite business communication stating appreciation for their quotation, referencing the order details (quantity, delivery location), and politely requesting the specific adjustment (e.g. price reduction to 180 INR per kg).\n"
        "  * Call the `send_counterparty_message` tool with `message`, `recipient_name`, and optional `proposed_price` / `proposed_currency`.\n"
        "  * In your reply to the user, confirm that the polite negotiation message was dispatched and inform them they can watch the live conversation in the seller chat pane on the right.\n"
        "- When the user provides follow-up additions, notes, or amendments (e.g. 'also mention that...', 'tell them that...', 'aslo mantion that the location is not indore its 10kg away'):\n"
        "  * You MUST call the `send_counterparty_message` tool with the amended/updated message.\n"
        "  * Do NOT call `update_rfq_draft` or update the product name with conversational directives.\n"
        "  * The `message` argument in `send_counterparty_message` MUST contain the full and complete letter from start to finish. Never truncate the tool argument mid-sentence.\n"
    ) + active_thread_section

    return f"""{SYSTEM_BUSINESS_PROMPT}

CURRENT RFQ DRAFT STATE:
{status_str}

CONVERSATION FLOW DIRECTIVE:
{flow_directive}
{matches_section}
{negotiation_directive}

IMPORTANT RULES:
- The user may provide one field at a time OR multiple fields in a single sentence (e.g. 'I want to buy apples in Indore').
- Required fields MUST be prioritized first.
- Always call `update_rfq_draft(**kwargs)` when business details are mentioned.
- When the user confirms or requests to create/post the RFQ (e.g. 'create rfq', 'yes create it', 'proceed', 'post it') and both Role and Product are known, call `create_rfq(...)` to publish it into the marketplace!
- When the user instructs to send a message to the seller or negotiate, call `send_counterparty_message(...)`.
- Keep all questions and responses extremely short and informative.
"""


def _process_tool_call(tool_call: dict[str, Any], current_draft: dict[str, Any]) -> dict[str, Any]:
    """Execute update_rfq_draft tool call arguments into the draft dictionary."""
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
            return rfq_tool_service.update_rfq_draft(existing_draft=current_draft, **args)
    return current_draft


async def execute_create_rfq_call(
    draft: dict[str, Any],
    tool_args: dict[str, Any],
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Execute create_rfq tool call, validating role and submitting to database."""
    readiness = rfq_tool_service.evaluate_rfq_readiness(draft)
    if not readiness["has_role"] or not readiness["has_product"]:
        return None, "Role (buyer/seller) and Product name are required before creating an RFQ."

    title = tool_args.get("title")
    status = tool_args.get("status", "active")
    notes = tool_args.get("notes")

    payload = rfq_tool_service.build_rfq_create_payload(
        draft=draft,
        title=title,
        status=status,
        notes=notes,
    )

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


async def execute_close_rfq_call(
    rfq_id_str: Optional[str] = None,
    conversation: Optional[Conversation] = None,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Close an active RFQ in the database."""
    if db is None or user is None:
        return None, "Database session not available."

    from models.rfq import RFQ
    from models.enums import RFQStatus
    from schemas.rfq import RFQUpdate
    from services import rfq_service

    target_rfq = None
    if rfq_id_str:
        try:
            target_rfq = await db.get(RFQ, uuid.UUID(str(rfq_id_str)))
        except Exception:
            pass

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
            .limit(1)
        )
        res = await db.execute(recent_stmt)
        target_rfq = res.scalar_one_or_none()

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


async def execute_send_counterparty_message_call(
    tool_args: dict[str, Any],
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Execute send_counterparty_message tool call, dispatching message to seller/counterparty."""
    message = tool_args.get("message")
    if not message or not str(message).strip():
        return None, "Message content is required to send to the counterparty."
    message_content = str(message).strip()

    if db is not None and user is not None:
        try:
            from services import connection_service
            from models.connection import Connection
            from models.enums import ConnectionStatus
            from schemas.connection import ConnectionMessageCreate

            conn = None
            target_conn_id = tool_args.get("connection_id") or active_connection_id
            if target_conn_id:
                try:
                    c_uuid = uuid.UUID(str(target_conn_id))
                    conn = await db.scalar(
                        select(Connection)
                        .options(*connection_service._FULL)
                        .where(Connection.id == c_uuid)
                    )
                except Exception:
                    conn = None

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
                    conn = conns[0]

            if not conn:
                return None, "No active seller connection found to send the message. Please view matches or connect with a seller first."

            # Ensure connection is ACCEPTED so communication can proceed
            if conn.status != ConnectionStatus.ACCEPTED:
                conn.status = ConnectionStatus.ACCEPTED
                await db.commit()
                await db.refresh(conn)

            # Send message via connection_service
            msg_payload = ConnectionMessageCreate(content=message_content)
            sent_msg = await connection_service.send_message(db, conn.id, user.id, msg_payload)

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

            return {
                "connection_id": str(conn.id),
                "counterparty_name": company_name,
                "message": message_content,
                "message_id": str(sent_msg.id),
                "status": "delivered",
                "created_at": sent_msg.created_at.isoformat(),
            }, None

        except Exception as exc:
            logger.exception("Failed to send counterparty message: %s", exc)
            return None, f"Failed to send counterparty message: {str(exc)}"
    else:
        # Fallback simulation (for tests without db session)
        sim_conn_id = str(active_connection_id or uuid.uuid4())
        sim_msg_id = str(uuid.uuid4())
        c_name = tool_args.get("recipient_name") or "Seller"
        return {
            "connection_id": sim_conn_id,
            "counterparty_name": c_name,
            "message": message_content,
            "message_id": sim_msg_id,
            "status": "delivered",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, None


def format_counterparty_message_text(result: dict[str, Any]) -> str:
    """Format confirmation message when a counterparty message is dispatched."""
    recipient = result.get("counterparty_name") or "Seller"
    msg_text = result.get("message") or ""
    return (
        f"✉️ **Message Sent to {recipient}**\n\n"
        f"> *\"{msg_text}\"*\n\n"
        f"The message has been delivered directly into your active chat thread with {recipient}. "
        f"You can monitor the live conversation and counterparty replies in the seller chat pane on the right."
    )


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


def format_created_rfq_text(created_rfq: dict[str, Any]) -> str:
    """Format celebratory message when an RFQ is created."""
    rfq_id = created_rfq.get("id", "")
    title = created_rfq.get("title", "RFQ")
    role = (created_rfq.get("role") or "BUYER").upper()
    status = (created_rfq.get("status") or "ACTIVE").upper()
    category = created_rfq.get("category", "General Goods")

    return (
        f"🎉 **RFQ Successfully Created & Published!**\n\n"
        f"- **Title**: {title}\n"
        f"- **RFQ ID**: `{rfq_id}`\n"
        f"- **Market Side**: {role}\n"
        f"- **Category**: {category}\n"
        f"- **Status**: {status} (Active & Live in Marketplace)\n\n"
        f"Your listing has been submitted to the marketplace and is active in the matching index. "
        f"Our matching engine is actively scanning for compatible counterparties!"
    )



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
) -> None:
    """Update conversation state, title, and timestamp."""
    state_update = dict(conversation.state or {})
    if draft is not None:
        state_update["rfq_draft"] = draft
    if readiness is not None:
        state_update["readiness"] = readiness
    if matched_candidates is not None:
        state_update["matched_candidates"] = matched_candidates
    if active_connection_id is not None:
        state_update["active_connection_id"] = str(active_connection_id)
    if last_counterparty_message is not None:
        state_update["last_counterparty_message"] = last_counterparty_message
        if last_counterparty_message.get("connection_id"):
            state_update["active_connection_id"] = str(last_counterparty_message["connection_id"])
    conversation.state = state_update

    if rfq_id is not None:
        conversation.rfq_id = rfq_id

    if user_prompt and (not conversation.title or conversation.title == "New Business Chat"):
        cleaned = " ".join(user_prompt.strip().split())
        if cleaned:
            conversation.title = cleaned[:77] + "..." if len(cleaned) > 80 else cleaned

    conversation.updated_at = datetime.now(timezone.utc)
    await db.commit()


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


async def generate_business_chat_reply(
    messages: list[Any],
    current_rfq: Optional[dict[str, Any]] = None,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    conversation_id: Optional[uuid.UUID] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
    agent: Optional[AgentDefinition] = None,
) -> dict[str, Any]:
    """Send conversation messages to local Ollama gpt-oss:20b with RFQ and counterparty negotiation tool calling."""
    # 1. Deterministic Preprocessing on the latest user message
    last_user_content = ""
    for msg in reversed(messages):
        r, c = _extract_msg_fields(msg)
        if r == "user":
            last_user_content = c
            break

    conversation = None
    if db is not None and user is not None:
        try:
            conversation = await get_or_create_conversation(
                db, user.id, conversation_id=conversation_id, initial_title=last_user_content
            )
            if last_user_content.strip():
                await save_message_to_conversation(
                    db, conversation.id, role="user", content=last_user_content
                )
        except Exception as exc:
            logger.exception("Failed to initialize conversation in generate_business_chat_reply: %s", exc)

    # Restore matched_candidates from conversation state if not passed in turn
    if not matched_candidates and conversation and conversation.state:
        matched_candidates = conversation.state.get("matched_candidates")

    last_counterparty_msg = None
    if conversation and conversation.state:
        if not active_connection_id and conversation.state.get("active_connection_id"):
            active_connection_id = conversation.state.get("active_connection_id")
        last_counterparty_msg = conversation.state.get("last_counterparty_message")

    has_prev_counterparty = bool(last_counterparty_msg)
    if not has_prev_counterparty:
        for m in reversed(messages):
            r, c = _extract_msg_fields(m)
            if r == "assistant" and any(k in c.lower() for k in ["message sent to", "dispatched", "✉️", "dear "]):
                has_prev_counterparty = True
                break

    is_counterparty_msg = rfq_tool_service.check_counterparty_message_intent(
        last_user_content,
        has_active_connection=bool(active_connection_id),
        has_previous_counterparty_msg=has_prev_counterparty,
    )

    draft = rfq_tool_service.preprocess_message_to_rfq(
        last_user_content,
        current_rfq,
        has_active_connection=bool(active_connection_id),
        has_previous_counterparty_msg=has_prev_counterparty,
    )
    readiness = rfq_tool_service.evaluate_rfq_readiness(draft)

    is_match_query = (not is_counterparty_msg) and (
        rfq_tool_service.check_match_query_intent(last_user_content) or (
            bool(matched_candidates) and any(
                w in last_user_content.lower()
                for w in [
                    "mail", "email", "top", "three", "two", "five", "best", "suit", "suits",
                    "which", "who", "compare", "candidate", "supplier", "buyer", "seller",
                    "cheapest", "rating", "contact", "phone",
                ]
            )
        )
    )

    async def _finalize_response(
        reply_text: str,
        thinking_text: Optional[str] = None,
        rfq_draft_data: Optional[dict[str, Any]] = None,
        readiness_data: Optional[dict[str, Any]] = None,
        created_rfq_data: Optional[dict[str, Any]] = None,
        counterparty_message_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if db is not None and conversation is not None:
            try:
                meta = {
                    "thinking": thinking_text,
                    "created_rfq": created_rfq_data,
                    "counterparty_message": counterparty_message_data,
                }
                await save_message_to_conversation(
                    db, conversation.id, role="assistant", content=reply_text, metadata=meta
                )
                rfq_uuid = (
                    uuid.UUID(created_rfq_data["id"])
                    if created_rfq_data and created_rfq_data.get("id")
                    else None
                )
                await update_conversation_after_turn(
                    db,
                    conversation,
                    draft=rfq_draft_data or draft,
                    readiness=readiness_data or readiness,
                    rfq_id=rfq_uuid,
                    user_prompt=last_user_content,
                    matched_candidates=matched_candidates,
                    active_connection_id=active_connection_id,
                    last_counterparty_message=counterparty_message_data or last_counterparty_msg,
                )
            except Exception as exc:
                logger.exception("Failed to persist assistant reply in _finalize_response: %s", exc)

        return {
            "reply": reply_text,
            "thinking": thinking_text,
            "rfq_draft": rfq_draft_data or draft,
            "readiness": readiness_data or readiness,
            "created_rfq": created_rfq_data,
            "counterparty_message": counterparty_message_data,
            "conversation_id": conversation.id if conversation else None,
        }

    # 2. Build system prompt
    # When a specialised agent is selected, prepend its prompt before the
    # standard RFQ-aware dynamic prompt.
    dynamic_system_prompt = build_rfq_system_prompt(
        draft,
        readiness,
        matched_candidates=matched_candidates,
        last_counterparty_message=last_counterparty_msg,
        active_connection_id=active_connection_id,
    )
    if agent and agent.id != "general" and agent.system_prompt:
        dynamic_system_prompt = agent.system_prompt + "\n\n" + dynamic_system_prompt
    prepared_messages: list[dict[str, Any]] = [
        {"role": "system", "content": dynamic_system_prompt}
    ]

    for msg in messages:
        role, content = _extract_msg_fields(msg)
        content = content.strip()
        if not content:
            continue
        safe_role = "assistant" if role == "assistant" else "user"
        prepared_messages.append({"role": safe_role, "content": content})

    if len(prepared_messages) == 1:
        return await _finalize_response(
            reply_text="Hello! I am your B2B Marketplace Business Assistant. Are you looking to BUY or SELL goods?",
            thinking_text=None,
            rfq_draft_data=draft,
            readiness_data=readiness,
            created_rfq_data=None,
            counterparty_message_data=None,
        )

    # Immediate handling when user explicitly asks to create/proceed with RFQ
    if not is_match_query and not is_counterparty_msg and rfq_tool_service.check_create_rfq_intent(last_user_content):
        if readiness["has_role"] and readiness["has_product"]:
            created_rfq_obj, err = await execute_create_rfq_call(draft, {}, db=db, user=user)
            if created_rfq_obj:
                reply = format_created_rfq_text(created_rfq_obj)
            else:
                reply = f"⚠️ {err or 'Failed to create RFQ.'}"
            return await _finalize_response(
                reply_text=reply,
                thinking_text="Validated required fields and submitted RFQ to marketplace database.",
                rfq_draft_data=draft,
                readiness_data=readiness,
                created_rfq_data=created_rfq_obj,
                counterparty_message_data=None,
            )
        else:
            missing = []
            if not readiness["has_role"]:
                missing.append("Role (Buyer or Seller)")
            if not readiness["has_product"]:
                missing.append("Product name")
            return await _finalize_response(
                reply_text=f"To create your RFQ, we still need {' and '.join(missing)}. Are you looking to BUY or SELL goods, and what product?",
                thinking_text=None,
                rfq_draft_data=draft,
                readiness_data=readiness,
                created_rfq_data=None,
                counterparty_message_data=None,
            )

    # Immediate handling when user explicitly asks to close an RFQ
    if not is_match_query and not is_counterparty_msg and rfq_tool_service.check_close_rfq_intent(last_user_content):
        closed_rfq_obj, err = await execute_close_rfq_call(
            rfq_id_str=None, conversation=conversation, db=db, user=user
        )
        if closed_rfq_obj:
            reply = f"✅ **RFQ Closed:** '{closed_rfq_obj['title']}' (ID: `{closed_rfq_obj['id']}`) has been closed. It is no longer active in the marketplace and will not appear in match searches."
        else:
            reply = f"⚠️ {err or 'Could not close the RFQ.'}"
        return await _finalize_response(
            reply_text=reply,
            thinking_text="Closed RFQ in database as requested.",
            rfq_draft_data=draft,
            readiness_data=readiness,
            created_rfq_data=closed_rfq_obj,
            counterparty_message_data=None,
        )

    tools = [] if is_match_query else [
        rfq_tool_service.UPDATE_RFQ_DRAFT_TOOL,
        rfq_tool_service.CREATE_RFQ_TOOL,
        rfq_tool_service.SEND_COUNTERPARTY_MESSAGE_TOOL,
        rfq_tool_service.CLOSE_RFQ_TOOL,
    ]

    payload: dict[str, Any] = {
        "model": settings.AI_CHAT_MODEL,
        "messages": prepared_messages,
        "stream": False,
        "options": {
            "temperature": 0.3,
            "num_ctx": settings.AI_CHAT_NUM_CTX,
        },
    }
    if tools:
        payload["tools"] = tools

    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"

    try:
        timeout = float(settings.AI_CHAT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)

        if response.status_code != 200:
            logger.error("Ollama chat returned status %d: %s", response.status_code, response.text)
            return await _finalize_response(
                reply_text="⚠️ The business AI assistant service is temporarily unavailable.",
                thinking_text=None,
                rfq_draft_data=draft,
                readiness_data=readiness,
                created_rfq_data=None,
                counterparty_message_data=None,
            )

        data = response.json()
        message_data = data.get("message") or {}
        reply = (message_data.get("content") or "").strip()
        thinking = message_data.get("thinking") or None

        # Execute any tool calls
        created_rfq_obj = None
        counterparty_msg_obj = None
        tool_calls = message_data.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            tc_name = fn.get("name")
            raw_args = fn.get("arguments") or {}
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = {}

            if tc_name == "create_rfq":
                created_rfq_obj, err = await execute_create_rfq_call(draft, raw_args, db=db, user=user)
                if err and not reply:
                    reply = f"⚠️ {err}"
            elif tc_name == "send_counterparty_message":
                counterparty_msg_obj, err = await execute_send_counterparty_message_call(
                    raw_args,
                    db=db,
                    user=user,
                    matched_candidates=matched_candidates,
                    active_connection_id=active_connection_id,
                )
                if err and not reply:
                    reply = f"⚠️ {err}"
            elif tc_name == "close_rfq":
                rfq_arg = raw_args.get("rfq_id")
                created_rfq_obj, err = await execute_close_rfq_call(
                    rfq_id_str=rfq_arg, conversation=conversation, db=db, user=user
                )
                if created_rfq_obj and not reply:
                    reply = f"✅ **RFQ Closed:** '{created_rfq_obj['title']}' (ID: `{created_rfq_obj['id']}`) has been closed."
                elif err and not reply:
                    reply = f"⚠️ {err}"
            elif tc_name == "update_rfq_draft":
                draft = _process_tool_call(tc, draft)

        readiness = rfq_tool_service.evaluate_rfq_readiness(draft)

        # Check deterministic user intent to create if tool was not called explicitly
        if not is_match_query and not is_counterparty_msg and not created_rfq_obj and rfq_tool_service.check_create_rfq_intent(last_user_content):
            if readiness["has_role"] and readiness["has_product"]:
                created_rfq_obj, err = await execute_create_rfq_call(draft, {}, db=db, user=user)
                if err and not reply:
                    reply = f"⚠️ {err}"

        # Deterministic fallback for counterparty message if tool was not called explicitly
        is_dispatch_reply = bool(re.search(r"(✉️|message\s+sent\s+to|dispatched|dear\s+[\w\s]+,)", reply, re.IGNORECASE))
        if not counterparty_msg_obj and (is_counterparty_msg or is_dispatch_reply):
            msg_to_send = extract_or_compose_counterparty_message(
                reply_text=reply,
                user_instruction=last_user_content,
                last_counterparty_info=last_counterparty_msg,
                draft=draft,
            )
            recipient_name = extract_recipient_name(
                reply_text=reply,
                last_counterparty_info=last_counterparty_msg,
                matched_candidates=matched_candidates,
            )
            counterparty_msg_obj, err = await execute_send_counterparty_message_call(
                tool_args={"message": msg_to_send, "recipient_name": recipient_name},
                db=db,
                user=user,
                matched_candidates=matched_candidates,
                active_connection_id=active_connection_id,
            )
            if counterparty_msg_obj and not reply:
                reply = format_counterparty_message_text(counterparty_msg_obj)

        # Check if counterparty_msg_obj message was truncated and reply has the full message
        if counterparty_msg_obj and is_truncated_message(counterparty_msg_obj.get("message", "")):
            complete_letter = extract_or_compose_counterparty_message(
                reply_text=reply,
                user_instruction=last_user_content,
                last_counterparty_info=last_counterparty_msg,
                draft=draft,
            )
            if complete_letter and not is_truncated_message(complete_letter):
                counterparty_msg_obj["message"] = complete_letter
                if counterparty_msg_obj.get("message_id") and db:
                    try:
                        from models.connection import ConnectionMessage
                        msg_uuid = uuid.UUID(str(counterparty_msg_obj["message_id"]))
                        db_msg = await db.get(ConnectionMessage, msg_uuid)
                        if db_msg:
                            db_msg.content = complete_letter
                            await db.commit()
                    except Exception as exc:
                        logger.warning("Could not update truncated message in DB: %s", exc)

        if counterparty_msg_obj:
            if not reply:
                reply = format_counterparty_message_text(counterparty_msg_obj)
        elif created_rfq_obj:
            reply = format_created_rfq_text(created_rfq_obj)
        elif not reply and thinking:
            reply = thinking
            thinking = None
        elif not reply:
            reply = format_rfq_summary_text(draft, readiness)

        return await _finalize_response(
            reply_text=reply,
            thinking_text=thinking,
            rfq_draft_data=draft,
            readiness_data=readiness,
            created_rfq_data=created_rfq_obj,
            counterparty_message_data=counterparty_msg_obj,
        )

    except Exception as exc:
        logger.error("Ollama chat error: %s", exc)
        return await _finalize_response(
            reply_text="⚠️ Connection to local AI assistant interrupted.",
            thinking_text=None,
            rfq_draft_data=draft,
            readiness_data=readiness,
            created_rfq_data=None,
        )


async def stream_business_chat_reply(
    messages: list[Any],
    current_rfq: Optional[dict[str, Any]] = None,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    conversation_id: Optional[uuid.UUID] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
    agent: Optional[AgentDefinition] = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Stream conversation replies chunk-by-chunk using SSE with RFQ and counterparty negotiation tool calling."""
    # 1. Deterministic Preprocessing on the latest user message
    last_user_content = ""
    for msg in reversed(messages):
        role, content = _extract_msg_fields(msg)
        if role == "user":
            last_user_content = content
            break

    conversation = None
    if db is not None and user is not None:
        try:
            conversation = await get_or_create_conversation(
                db, user.id, conversation_id=conversation_id, initial_title=last_user_content
            )
            if last_user_content.strip():
                await save_message_to_conversation(
                    db, conversation.id, role="user", content=last_user_content
                )
        except Exception as exc:
            logger.exception("Failed to initialize conversation for stream: %s", exc)

    # Restore matched_candidates from conversation state if not passed in turn
    if not matched_candidates and conversation and conversation.state:
        matched_candidates = conversation.state.get("matched_candidates")

    last_counterparty_msg = None
    if conversation and conversation.state:
        if not active_connection_id and conversation.state.get("active_connection_id"):
            active_connection_id = conversation.state.get("active_connection_id")
        last_counterparty_msg = conversation.state.get("last_counterparty_message")

    has_prev_counterparty = bool(last_counterparty_msg)
    if not has_prev_counterparty:
        for m in reversed(messages):
            r, c = _extract_msg_fields(m)
            if r == "assistant" and any(k in c.lower() for k in ["message sent to", "dispatched", "✉️", "dear "]):
                has_prev_counterparty = True
                break

    active_conv_id = str(conversation.id) if conversation else None

    is_counterparty_msg = rfq_tool_service.check_counterparty_message_intent(
        last_user_content,
        has_active_connection=bool(active_connection_id),
        has_previous_counterparty_msg=has_prev_counterparty,
    )

    draft = rfq_tool_service.preprocess_message_to_rfq(
        last_user_content,
        current_rfq,
        has_active_connection=bool(active_connection_id),
        has_previous_counterparty_msg=has_prev_counterparty,
    )
    readiness = rfq_tool_service.evaluate_rfq_readiness(draft)

    is_match_query = (not is_counterparty_msg) and (
        rfq_tool_service.check_match_query_intent(last_user_content) or (
            bool(matched_candidates) and any(
                w in last_user_content.lower()
                for w in [
                    "mail", "email", "top", "three", "two", "five", "best", "suit", "suits",
                    "which", "who", "compare", "candidate", "supplier", "buyer", "seller",
                    "cheapest", "rating", "contact", "phone",
                ]
            )
        )
    )

    def emit(chunk: dict[str, Any]) -> dict[str, Any]:
        if active_conv_id:
            chunk["conversation_id"] = active_conv_id
        return chunk

    async def _persist_stream_terminal(
        reply_content: str,
        thinking_val: Optional[str] = None,
        thinking_after_val: Optional[str] = None,
        tool_step_val: Optional[dict[str, Any]] = None,
        created_rfq_val: Optional[dict[str, Any]] = None,
        counterparty_message_val: Optional[dict[str, Any]] = None,
    ):
        if db is not None and conversation is not None:
            try:
                metadata = {
                    "thinking": thinking_val or None,
                    "thinking_after": thinking_after_val or None,
                    "tool_step": tool_step_val,
                    "created_rfq": created_rfq_val,
                    "counterparty_message": counterparty_message_val,
                }
                await save_message_to_conversation(
                    db,
                    conversation.id,
                    role="assistant",
                    content=reply_content,
                    metadata=metadata,
                )
                rfq_uuid = (
                    uuid.UUID(created_rfq_val["id"])
                    if created_rfq_val and created_rfq_val.get("id")
                    else None
                )
                await update_conversation_after_turn(
                    db,
                    conversation,
                    draft=draft,
                    readiness=readiness,
                    rfq_id=rfq_uuid,
                    user_prompt=last_user_content,
                    matched_candidates=matched_candidates,
                    active_connection_id=active_connection_id,
                    last_counterparty_message=counterparty_message_val or last_counterparty_msg,
                )
            except Exception as exc:
                logger.exception("Failed to persist stream turn: %s", exc)

    # 2. Build system prompt
    dynamic_system_prompt = build_rfq_system_prompt(
        draft,
        readiness,
        matched_candidates=matched_candidates,
        last_counterparty_message=last_counterparty_msg,
        active_connection_id=active_connection_id,
    )
    if agent and agent.id != "general" and agent.system_prompt:
        dynamic_system_prompt = agent.system_prompt + "\n\n" + dynamic_system_prompt
    prepared_messages: list[dict[str, Any]] = [
        {"role": "system", "content": dynamic_system_prompt}
    ]

    for msg in messages:
        role, content = _extract_msg_fields(msg)
        content = content.strip()
        if not content:
            continue
        safe_role = "assistant" if role == "assistant" else "user"
        prepared_messages.append({"role": safe_role, "content": content})

    if len(prepared_messages) == 1:
        hello_reply = "Hello! I am your B2B Marketplace Business Assistant. Are you looking to BUY or SELL goods?"
        await _persist_stream_terminal(reply_content=hello_reply)
        yield emit({
            "content": hello_reply,
            "thinking": "",
            "thinking_after": "",
            "tool_step": None,
            "rfq_draft": draft,
            "readiness": readiness,
            "created_rfq": None,
            "counterparty_message": None,
            "done": True,
        })
        return

    # Immediate handling when user explicitly asks to create/proceed with RFQ
    if not is_match_query and not is_counterparty_msg and rfq_tool_service.check_create_rfq_intent(last_user_content):
        if readiness["has_role"] and readiness["has_product"]:
            # Initial thinking step
            yield emit({
                "content": "",
                "thinking": "User requested to finalize and publish RFQ. Validating required fields and submitting listing to marketplace database...",
                "thinking_after": "",
                "tool_step": None,
                "rfq_draft": draft,
                "readiness": readiness,
                "created_rfq": None,
                "counterparty_message": None,
                "done": False,
            })
            created_rfq_obj, err = await execute_create_rfq_call(draft, {}, db=db, user=user)
            tool_step = {
                "name": "create_rfq",
                "title": "Creating Marketplace RFQ",
                "status": "completed" if created_rfq_obj else "failed",
                "args": (
                    {"title": created_rfq_obj["title"], "id": created_rfq_obj["id"]}
                    if created_rfq_obj
                    else {"error": err}
                ),
            }
            yield emit({
                "content": "",
                "thinking": "",
                "thinking_after": "",
                "tool_step": tool_step,
                "rfq_draft": draft,
                "readiness": readiness,
                "created_rfq": created_rfq_obj,
                "counterparty_message": None,
                "done": False,
            })
            if created_rfq_obj:
                reply_text = format_created_rfq_text(created_rfq_obj)
            else:
                reply_text = f"⚠️ {err or 'Failed to create RFQ.'}"

            await _persist_stream_terminal(
                reply_content=reply_text,
                tool_step_val=tool_step,
                created_rfq_val=created_rfq_obj,
                counterparty_message_val=None,
            )
            yield emit({
                "content": reply_text,
                "thinking": "",
                "thinking_after": "",
                "tool_step": tool_step,
                "rfq_draft": draft,
                "readiness": readiness,
                "created_rfq": created_rfq_obj,
                "counterparty_message": None,
                "done": True,
            })
            return
        else:
            missing = []
            if not readiness["has_role"]:
                missing.append("Role (Buyer or Seller)")
            if not readiness["has_product"]:
                missing.append("Product name")
            missing_str = " and ".join(missing)
            missing_reply = f"To create your RFQ, we still need {missing_str}. Are you looking to BUY or SELL goods, and what product?"
            await _persist_stream_terminal(reply_content=missing_reply)
            yield emit({
                "content": missing_reply,
                "thinking": "",
                "thinking_after": "",
                "tool_step": None,
                "rfq_draft": draft,
                "readiness": readiness,
                "created_rfq": None,
                "counterparty_message": None,
                "done": True,
            })
            return

    # Immediate handling when user explicitly asks to close an RFQ
    if not is_match_query and not is_counterparty_msg and rfq_tool_service.check_close_rfq_intent(last_user_content):
        closed_rfq_obj, err = await execute_close_rfq_call(
            rfq_id_str=None, conversation=conversation, db=db, user=user
        )
        tool_step = {
            "name": "close_rfq",
            "title": "Closing Marketplace RFQ",
            "status": "completed" if closed_rfq_obj else "failed",
            "args": (
                {"title": closed_rfq_obj["title"], "id": closed_rfq_obj["id"]}
                if closed_rfq_obj
                else {"error": err}
            ),
        }
        if closed_rfq_obj:
            reply_text = f"✅ **RFQ Closed:** '{closed_rfq_obj['title']}' (ID: `{closed_rfq_obj['id']}`) has been successfully closed. It is no longer active in the marketplace and will not appear in match searches."
        else:
            reply_text = f"⚠️ {err or 'Could not close the RFQ.'}"

        await _persist_stream_terminal(
            reply_content=reply_text,
            tool_step_val=tool_step,
            created_rfq_val=closed_rfq_obj,
            counterparty_message_val=None,
        )
        yield emit({
            "content": reply_text,
            "thinking": "",
            "thinking_after": "",
            "tool_step": tool_step,
            "rfq_draft": draft,
            "readiness": readiness,
            "created_rfq": closed_rfq_obj,
            "counterparty_message": None,
            "done": True,
        })
        return

    tools = [] if is_match_query else [
        rfq_tool_service.UPDATE_RFQ_DRAFT_TOOL,
        rfq_tool_service.CREATE_RFQ_TOOL,
        rfq_tool_service.SEND_COUNTERPARTY_MESSAGE_TOOL,
        rfq_tool_service.CLOSE_RFQ_TOOL,
    ]

    payload: dict[str, Any] = {
        "model": settings.AI_CHAT_MODEL,
        "messages": prepared_messages,
        "stream": True,
        "options": {
            "temperature": 0.3,
            "num_ctx": settings.AI_CHAT_NUM_CTX,
        },
    }
    if tools:
        payload["tools"] = tools

    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"

    try:
        timeout = httpx.Timeout(
            connect=15.0,
            read=float(settings.AI_CHAT_TIMEOUT_SECONDS),
            write=15.0,
            pool=15.0,
        )
        total_content = ""
        total_thinking = ""
        total_thinking_after = ""
        has_tool_called = False
        captured_tool_step = None
        created_rfq_obj = None
        counterparty_msg_obj = None

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    unavail_reply = "⚠️ The business AI assistant service is temporarily unavailable."
                    await _persist_stream_terminal(reply_content=unavail_reply)
                    yield emit({
                        "content": unavail_reply,
                        "thinking": "",
                        "thinking_after": "",
                        "tool_step": None,
                        "rfq_draft": draft,
                        "readiness": readiness,
                        "created_rfq": None,
                        "counterparty_message": None,
                        "done": True,
                    })
                    return

                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    message_chunk = chunk.get("message") or {}
                    content_delta = message_chunk.get("content") or ""
                    thinking_delta = message_chunk.get("thinking") or ""
                    is_done = bool(chunk.get("done", False))

                    if content_delta:
                        total_content += content_delta
                    if thinking_delta:
                        if has_tool_called:
                            total_thinking_after += thinking_delta
                        else:
                            total_thinking += thinking_delta

                    # Tool calls from chunk
                    tool_calls = message_chunk.get("tool_calls") or []
                    for tc in tool_calls:
                        fn = tc.get("function") or {}
                        tc_name = fn.get("name")
                        raw_args = fn.get("arguments") or {}
                        if isinstance(raw_args, str):
                            try:
                                raw_args = json.loads(raw_args)
                            except Exception:
                                pass
                        has_tool_called = True

                        if tc_name == "create_rfq":
                            created_rfq_obj, err = await execute_create_rfq_call(draft, raw_args, db=db, user=user)
                            captured_tool_step = {
                                "name": "create_rfq",
                                "title": "Creating Marketplace RFQ",
                                "status": "completed" if created_rfq_obj else "failed",
                                "args": (
                                    {"title": created_rfq_obj["title"], "id": created_rfq_obj["id"]}
                                    if created_rfq_obj
                                    else {"error": err}
                                ),
                            }
                            yield emit({
                                "content": "",
                                "thinking": "",
                                "thinking_after": "",
                                "tool_step": captured_tool_step,
                                "rfq_draft": draft,
                                "readiness": readiness,
                                "created_rfq": created_rfq_obj,
                                "counterparty_message": None,
                                "done": False,
                            })
                        elif tc_name == "send_counterparty_message":
                            counterparty_msg_obj, err = await execute_send_counterparty_message_call(
                                raw_args,
                                db=db,
                                user=user,
                                matched_candidates=matched_candidates,
                                active_connection_id=active_connection_id,
                            )
                            c_name = counterparty_msg_obj.get("counterparty_name", "Counterparty") if counterparty_msg_obj else "Counterparty"
                            captured_tool_step = {
                                "name": "send_counterparty_message",
                                "title": f"Dispatching Message to {c_name}" if counterparty_msg_obj else "Messaging Counterparty",
                                "status": "completed" if counterparty_msg_obj else "failed",
                                "args": (
                                    {"recipient": c_name, "message": counterparty_msg_obj["message"]}
                                    if counterparty_msg_obj
                                    else {"error": err}
                                ),
                            }
                            yield emit({
                                "content": "",
                                "thinking": "",
                                "thinking_after": "",
                                "tool_step": captured_tool_step,
                                "rfq_draft": draft,
                                "readiness": readiness,
                                "created_rfq": None,
                                "counterparty_message": counterparty_msg_obj,
                                "done": False,
                            })
                        elif tc_name == "close_rfq":
                            rfq_arg = raw_args.get("rfq_id")
                            created_rfq_obj, err = await execute_close_rfq_call(
                                rfq_id_str=rfq_arg, conversation=conversation, db=db, user=user
                            )
                            captured_tool_step = {
                                "name": "close_rfq",
                                "title": "Closing Marketplace RFQ",
                                "status": "completed" if created_rfq_obj else "failed",
                                "args": (
                                    {"title": created_rfq_obj["title"], "id": created_rfq_obj["id"]}
                                    if created_rfq_obj
                                    else {"error": err}
                                ),
                            }
                            yield emit({
                                "content": "",
                                "thinking": "",
                                "thinking_after": "",
                                "tool_step": captured_tool_step,
                                "rfq_draft": draft,
                                "readiness": readiness,
                                "created_rfq": created_rfq_obj,
                                "counterparty_message": None,
                                "done": False,
                            })
                        else:
                            draft = _process_tool_call(tc, draft)
                            captured_tool_step = {
                                "name": tc_name or "update_rfq_draft",
                                "title": "Updating RFQ Preferences & Draft",
                                "status": "completed",
                                "args": raw_args,
                            }
                            readiness = rfq_tool_service.evaluate_rfq_readiness(draft)
                            yield emit({
                                "content": "",
                                "thinking": "",
                                "thinking_after": "",
                                "tool_step": captured_tool_step,
                                "rfq_draft": draft,
                                "readiness": readiness,
                                "created_rfq": None,
                                "counterparty_message": None,
                                "done": False,
                            })

                    readiness = rfq_tool_service.evaluate_rfq_readiness(draft)

                    if content_delta or thinking_delta:
                        yield emit({
                            "content": content_delta,
                            "thinking": thinking_delta if not has_tool_called else "",
                            "thinking_after": thinking_delta if has_tool_called else "",
                            "tool_step": None,
                            "rfq_draft": draft,
                            "readiness": readiness,
                            "created_rfq": None,
                            "counterparty_message": counterparty_msg_obj,
                            "done": False,
                        })

                    if is_done:
                        break

        # Check user intent to create if tool was not called explicitly by model
        if not is_match_query and not is_counterparty_msg and not created_rfq_obj and rfq_tool_service.check_create_rfq_intent(last_user_content):
            if readiness["has_role"] and readiness["has_product"]:
                created_rfq_obj, err = await execute_create_rfq_call(draft, {}, db=db, user=user)
                captured_tool_step = {
                    "name": "create_rfq",
                    "title": "Creating Marketplace RFQ",
                    "status": "completed" if created_rfq_obj else "failed",
                    "args": (
                        {"title": created_rfq_obj["title"], "id": created_rfq_obj["id"]}
                        if created_rfq_obj
                        else {"error": err}
                    ),
                }
                yield emit({
                    "content": "",
                    "thinking": "",
                    "thinking_after": "",
                    "tool_step": captured_tool_step,
                    "rfq_draft": draft,
                    "readiness": readiness,
                    "created_rfq": created_rfq_obj,
                    "counterparty_message": None,
                    "done": False,
                })

        # Check deterministic counterparty messaging fallback if tool was not called explicitly
        is_dispatch_reply = bool(re.search(r"(✉️|message\s+sent\s+to|dispatched|dear\s+[\w\s]+,)", total_content, re.IGNORECASE))
        if not counterparty_msg_obj and (is_counterparty_msg or is_dispatch_reply):
            msg_to_send = extract_or_compose_counterparty_message(
                reply_text=total_content,
                user_instruction=last_user_content,
                last_counterparty_info=last_counterparty_msg,
                draft=draft,
            )
            recipient_name = extract_recipient_name(
                reply_text=total_content,
                last_counterparty_info=last_counterparty_msg,
                matched_candidates=matched_candidates,
            )
            counterparty_msg_obj, err = await execute_send_counterparty_message_call(
                tool_args={"message": msg_to_send, "recipient_name": recipient_name},
                db=db,
                user=user,
                matched_candidates=matched_candidates,
                active_connection_id=active_connection_id,
            )
            if counterparty_msg_obj:
                c_name = counterparty_msg_obj.get("counterparty_name", recipient_name)
                captured_tool_step = {
                    "name": "send_counterparty_message",
                    "title": f"Dispatching Message to {c_name}",
                    "status": "completed",
                    "args": {"recipient": c_name, "message": counterparty_msg_obj["message"]},
                }
                yield emit({
                    "content": "",
                    "thinking": "",
                    "thinking_after": "",
                    "tool_step": captured_tool_step,
                    "rfq_draft": draft,
                    "readiness": readiness,
                    "created_rfq": None,
                    "counterparty_message": counterparty_msg_obj,
                    "done": False,
                })

        # If model did not emit tool_calls in stream, but preprocessing updated fields
        if not is_match_query and not is_counterparty_msg and not counterparty_msg_obj and not captured_tool_step and draft != (current_rfq or {}):
            extracted_args = {}
            for k in ("role", "category", "product_details", "quantity", "price_target", "location", "deadline"):
                if draft.get(k) and draft.get(k) != (current_rfq or {}).get(k):
                    extracted_args[k] = draft[k]
            if extracted_args:
                captured_tool_step = {
                    "name": "update_rfq_draft",
                    "title": "Updating RFQ Preferences & Draft",
                    "status": "completed",
                    "args": extracted_args,
                }
                yield emit({
                    "content": "",
                    "thinking": "",
                    "thinking_after": "",
                    "tool_step": captured_tool_step,
                    "rfq_draft": draft,
                    "readiness": readiness,
                    "created_rfq": None,
                    "counterparty_message": None,
                    "done": False,
                })

        # Check if counterparty_msg_obj has a truncated message and total_content has the full message
        if counterparty_msg_obj and is_truncated_message(counterparty_msg_obj.get("message", "")):
            complete_letter = extract_or_compose_counterparty_message(
                reply_text=total_content,
                user_instruction=last_user_content,
                last_counterparty_info=last_counterparty_msg,
                draft=draft,
            )
            if complete_letter and not is_truncated_message(complete_letter):
                counterparty_msg_obj["message"] = complete_letter
                if counterparty_msg_obj.get("message_id") and db:
                    try:
                        from models.connection import ConnectionMessage
                        msg_uuid = uuid.UUID(str(counterparty_msg_obj["message_id"]))
                        db_msg = await db.get(ConnectionMessage, msg_uuid)
                        if db_msg:
                            db_msg.content = complete_letter
                            await db.commit()
                    except Exception as exc:
                        logger.warning("Could not update truncated message in DB: %s", exc)

        # Final completion chunk
        if counterparty_msg_obj:
            if total_content.strip():
                final_content = ""
                persist_content = total_content
            else:
                final_content = format_counterparty_message_text(counterparty_msg_obj)
                persist_content = final_content

            await _persist_stream_terminal(
                reply_content=persist_content,
                thinking_val=total_thinking,
                thinking_after_val=total_thinking_after,
                tool_step_val=captured_tool_step,
                created_rfq_val=None,
                counterparty_message_val=counterparty_msg_obj,
            )
            yield emit({
                "content": final_content,
                "thinking": "",
                "thinking_after": "",
                "tool_step": captured_tool_step,
                "rfq_draft": draft,
                "readiness": readiness,
                "created_rfq": None,
                "counterparty_message": counterparty_msg_obj,
                "done": True,
            })
        elif created_rfq_obj:
            if total_content.strip():
                final_content = ""
                persist_content = total_content
            else:
                final_content = format_created_rfq_text(created_rfq_obj)
                persist_content = final_content

            await _persist_stream_terminal(
                reply_content=persist_content,
                thinking_val=total_thinking,
                thinking_after_val=total_thinking_after,
                tool_step_val=captured_tool_step,
                created_rfq_val=created_rfq_obj,
                counterparty_message_val=None,
            )
            yield emit({
                "content": final_content,
                "thinking": "",
                "thinking_after": "",
                "tool_step": captured_tool_step,
                "rfq_draft": draft,
                "readiness": readiness,
                "created_rfq": created_rfq_obj,
                "counterparty_message": None,
                "done": True,
            })
        elif total_content.strip():
            await _persist_stream_terminal(
                reply_content=total_content,
                thinking_val=total_thinking,
                thinking_after_val=total_thinking_after,
                tool_step_val=captured_tool_step,
                created_rfq_val=None,
                counterparty_message_val=None,
            )
            yield emit({
                "content": "",
                "thinking": "",
                "thinking_after": "",
                "tool_step": captured_tool_step,
                "rfq_draft": draft,
                "readiness": readiness,
                "created_rfq": None,
                "counterparty_message": None,
                "done": True,
            })
        else:
            # Provide intelligent, structured acknowledgment based on current draft
            fallback_text = format_rfq_summary_text(draft, readiness)
            await _persist_stream_terminal(
                reply_content=fallback_text,
                thinking_val=total_thinking,
                thinking_after_val=total_thinking_after,
                tool_step_val=captured_tool_step,
                created_rfq_val=None,
                counterparty_message_val=None,
            )
            yield emit({
                "content": fallback_text,
                "thinking": "",
                "thinking_after": "",
                "tool_step": captured_tool_step,
                "rfq_draft": draft,
                "readiness": readiness,
                "created_rfq": None,
                "counterparty_message": None,
                "done": True,
            })

    except httpx.TimeoutException:
        timeout_msg = "\n\n⏳ [Response timed out. Please try asking a more concise question.]"
        await _persist_stream_terminal(reply_content=timeout_msg, tool_step_val=captured_tool_step)
        yield emit({
            "content": timeout_msg,
            "thinking": "",
            "thinking_after": "",
            "tool_step": captured_tool_step,
            "rfq_draft": draft,
            "readiness": readiness,
            "created_rfq": None,
            "counterparty_message": None,
            "done": True,
        })
    except Exception as exc:
        logger.error("Ollama stream failed: %s", exc)
        error_msg = "\n\n⚠️ [Connection to local AI assistant interrupted.]"
        await _persist_stream_terminal(reply_content=error_msg, tool_step_val=captured_tool_step)
        yield emit({
            "content": error_msg,
            "thinking": "",
            "thinking_after": "",
            "tool_step": captured_tool_step,
            "rfq_draft": draft,
            "readiness": readiness,
            "created_rfq": None,
            "counterparty_message": None,
            "done": True,
        })

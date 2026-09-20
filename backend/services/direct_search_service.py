"""Conversational search that never creates an RFQ.

The second user journey. A message is parsed into requirements, merged with
what earlier turns established, and turned into a *transient* RFQ that is
matched against the market and then thrown away. Nothing is published, and the
user is never a counterparty in anyone else's results.

Each turn either asks for the one most useful missing field or, once the product
is known, returns a ranked table and asks for the next field alongside it -- so
there is always something on screen to react to.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.conversation import Conversation, Message, MessageEvent
from models.enums import ConversationType, MessageRole, RFQStatus
from models.rfq import RFQ
from models.user import User
from schemas.match import MatchCandidate
from core.config import settings
from services import conversation_llm, currency as currency_service, llm_extractor, match_service
from services.moderation_service import AIContentModerator
from services.query_extractor import (
    Requirements,
    _CATEGORY_EXAMPLES,
    extract,
    is_broad_product,
    merge,
    merge_answer,
    parse_answer,
)
from services.rfq_indexing import build_search_tags, build_search_text


class ModerationSessionBlockedError(Exception):
    """Raised when a user attempts to continue a session terminated for safety violations."""


# Asked in this order: the earlier a field is, the more it changes the ranking.
QUESTION_ORDER: tuple[str, ...] = ("quantity", "price", "location", "deadline")

# Fallback phrasing, used when the model is off, slow or unreachable. These
# still read back the product rather than reciting a fixed string.
QUESTION_TEMPLATES: dict[str, str] = {
    "quantity": "How many {product} do you need, and in what unit?",
    "price": "What is your target price per unit for {product}?",
    "location": "Which city should the {product} be delivered to?",
    "deadline": "By when do you need the {product}?",
}

# Kept for callers that only need the bare prompt (and for the API's field list).
QUESTIONS: dict[str, str] = {
    field: template.replace("{product}", "them").replace("the them", "them")
    for field, template in QUESTION_TEMPLATES.items()
}


# Used when the product is not known yet, so the question does not read
# "How many them do you need".
_BARE_QUESTIONS: dict[str, str] = {
    "quantity": "How many do you need, and in what unit?",
    "price": "What is your target price per unit?",
    "location": "Which city should this be delivered to?",
    "deadline": "By when do you need it?",
}


def fallback_question(field: str, requirements: "Requirements") -> str:
    if not requirements.product:
        return _BARE_QUESTIONS.get(field, "Can you tell me more?")
    template = QUESTION_TEMPLATES.get(field, "Can you tell me more?")
    return template.format(product=requirements.product)


async def compose_question(field: str, requirements: "Requirements") -> str:
    """Let the model phrase it; fall back the moment it is slow or odd."""
    generated = await conversation_llm.next_question(requirements, field)
    return generated or fallback_question(field, requirements)

# Anything here means "stop asking about this one".
_SKIP_WORDS = frozenset(
    """skip any anything whatever doesn't dont don't matter nevermind never mind
    no none nothing na n/a not sure unsure later show results just show
    show me results go ahead continue proceed""".split()
)

# "skip" retires the field being asked about. These retire every remaining
# question and go straight to the search.
_SHOW_NOW = (
    "show results", "show me results", "just show", "show now", "search now",
    "search it", "go ahead", "that's all", "thats all", "done", "no more",
)

MAX_RESULTS = 10


class DirectSearchResult:
    """What one turn produced. Assembled into the API response by the router."""

    def __init__(
        self,
        conversation: Conversation,
        requirements: Requirements,
        reply: str,
        results: list[MatchCandidate],
        total: int,
        pending: Optional[str],
        search_id: Optional[uuid.UUID],
        notes: list[str],
        blocked: bool = False,
        block_reason: Optional[str] = None,
        block_category: Optional[str] = None,
    ) -> None:
        self.conversation = conversation
        self.requirements = requirements
        self.reply = reply
        self.results = results
        self.total = total
        self.pending = pending
        self.search_id = search_id
        self.notes = notes
        self.blocked = blocked
        self.block_reason = block_reason
        self.block_category = block_category


def _is_show_now(message: str) -> bool:
    cleaned = message.strip().lower().rstrip(".!")
    return any(phrase in cleaned for phrase in _SHOW_NOW)


def _is_skip(message: str) -> bool:
    cleaned = message.strip().lower().rstrip(".!")
    if not cleaned:
        return False
    words = set(cleaned.split())
    # Short and made entirely of skip words, so "no printing needed" is not a skip.
    return len(words) <= 4 and words.issubset(_SKIP_WORDS)


def _missing_fields(requirements: Requirements) -> list[str]:
    return [
        field
        for field in QUESTION_ORDER
        if not requirements.known(field) and field not in requirements.skipped
    ]


def build_probe_rfq(requirements: Requirements, user_id: uuid.UUID) -> RFQ:
    """A throwaway RFQ used only to drive the matcher. Never added to a session."""
    details: dict[str, Any] = dict(requirements.attributes)
    if requirements.product:
        details["name"] = requirements.product

    descriptor = requirements.product or "items"
    rfq = RFQ(
        user_id=user_id,
        role=requirements.role,
        status=RFQStatus.ACTIVE,
        category=requirements.category or "General",
        title=f"Looking for {descriptor}",
        quantity_value=requirements.quantity_value,
        quantity_unit=requirements.quantity_unit,
        price_amount=requirements.price_amount,
        # Explicit: column defaults only apply on INSERT, and this row is never
        # inserted.
        # Falls back to the city's currency rather than a global default: a
        # price quoted in Hamburg is not rupees.
        price_currency=(
            requirements.price_currency or requirements.currency_hint or "USD"
        ),
        price_per_unit=requirements.price_per_unit,
        location_city=requirements.city,
        location_state=requirements.state,
        location_country=requirements.country,
        latitude=requirements.latitude,
        longitude=requirements.longitude,
        deadline_at=(
            datetime.now(UTC) + timedelta(days=requirements.deadline_days)
            if requirements.deadline_days is not None
            else None
        ),
        product_details=details,
    )
    rfq.search_tags = build_search_tags(rfq)
    rfq.search_text = build_search_text(rfq)
    return rfq


def _describe(requirements: Requirements) -> str:
    parts: list[str] = []
    color = requirements.attributes.get("color")
    if isinstance(color, str):
        parts.append(color)
    if requirements.product:
        parts.append(requirements.product)
    text = " ".join(parts) or "that"
    if requirements.city:
        text += f" in {requirements.city}"
    elif requirements.state:
        text += f" in {requirements.state}"
    elif requirements.country:
        text += f" in {requirements.country}"
    return text


def _acknowledge(requirements: Requirements) -> str:
    """A short readback, so the user can see what has landed so far."""
    parts: list[str] = []
    color = requirements.attributes.get("color")
    if isinstance(color, str):
        parts.append(color)
    if requirements.product:
        parts.append(requirements.product)

    summary = " ".join(parts)
    extras: list[str] = []
    if requirements.quantity_value is not None:
        extras.append(f"{requirements.quantity_value:,} {requirements.quantity_unit or 'units'}")
    if requirements.price_amount is not None:
        unit = f"/{requirements.price_per_unit}" if requirements.price_per_unit else ""
        extras.append(f"{requirements.price_amount:,} {requirements.price_currency or 'INR'}{unit}")
    if requirements.city:
        extras.append(requirements.city)
    elif requirements.state:
        extras.append(requirements.state)
    elif requirements.country:
        extras.append(requirements.country)
    if requirements.deadline_days is not None:
        extras.append(f"within {requirements.deadline_days} days")

    if extras:
        summary = f"{summary} \u2014 {', '.join(extras)}" if summary else ", ".join(extras)
    return summary


def _compose_reply(
    requirements: Requirements,
    total: int,
    shown: int,
    pending: Optional[str],
    notes: list[str],
) -> str:
    target = "sellers" if requirements.role.value == "buyer" else "buyers"

    if total == 0:
        lines = [
            f"I could not find any {target} for {_describe(requirements)} right now."
        ]
        if requirements.attributes:
            lines.append("You could try relaxing an attribute, or widening the location.")
    else:
        lines = [f"Found {total} {target} for {_describe(requirements)}, showing {shown}."]

    lines.extend(notes)
    if pending:
        lines.append(f"{QUESTIONS[pending]} (or say “skip”)")
    return " ".join(lines)


def _currency_note(requirements: Requirements, results: list[MatchCandidate]) -> list[str]:
    """Say so when the price could not be compared, rather than silently not scoring it."""
    if requirements.price_amount is None or not results:
        return []
    wanted = requirements.price_currency or "INR"
    theirs = {c.price.currency for c in results if c.price is not None}
    if theirs and wanted not in theirs:
        unconvertible = [
            c for c in theirs if currency_service.convert(Decimal("1"), c, wanted) is None
        ]
        if unconvertible:
            listed = ", ".join(sorted(unconvertible))
            return [
                f"Note: your target is in {wanted} but some listings are quoted in {listed} "
                "(which could not be converted), so their price was not scored."
            ]
    return []


class ConversationNotFound(Exception):
    """A conversation id was supplied that this account cannot continue."""


async def _load_conversation(
    db: AsyncSession, user: User, conversation_id: Optional[uuid.UUID]
) -> Conversation:
    """Continue the named conversation, or start one when none was named.

    An id that does not resolve is an error rather than a fresh start. Quietly
    opening a new conversation answered 200 while discarding everything the user
    had already said, and did the same for somebody else's id -- so a stale tab
    looked like it was still refining a search that no longer existed.
    """
    if conversation_id is not None:
        conversation = await db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
                Conversation.type == ConversationType.DIRECT_SEARCH,
            )
        )
        if conversation is None:
            raise ConversationNotFound(str(conversation_id))
        return conversation

    conversation = Conversation(
        user_id=user.id, type=ConversationType.DIRECT_SEARCH, state={}
    )
    db.add(conversation)
    await db.flush()
    return conversation


def _load_requirements(conversation: Conversation) -> Requirements:
    stored = (conversation.state or {}).get("requirements")
    if not stored:
        return Requirements()
    try:
        return Requirements.model_validate(stored)
    except Exception:
        # A state written by an older version should not break the chat.
        return Requirements()


def _record(message: Message, sequence: int, type_: str, content: str, metadata: dict) -> MessageEvent:
    return MessageEvent(
        message=message,
        sequence=sequence,
        type=type_,
        content=content,
        event_metadata=metadata,
    )


async def handle_message(
    db: AsyncSession,
    user: User,
    text: str,
    *,
    conversation_id: Optional[uuid.UUID] = None,
    limit: int = MAX_RESULTS,
) -> DirectSearchResult:
    conversation = await _load_conversation(db, user, conversation_id)
    state = dict(conversation.state or {})

    # Check if this session has already been locked due to a policy violation
    if state.get("blocked"):
        reason = state.get("block_reason") or "Prohibited items detected."
        raise ModerationSessionBlockedError(
            f"This search session has been terminated and locked due to a safety policy violation: {reason}"
        )

    # AI Safety Moderation Guardrail Check
    mod_check = await AIContentModerator.audit_and_verify(
        db, user_id=user.id, action="direct_search", title=text
    )
    if not mod_check.is_safe:
        # Policy violation: immediately lock this conversation session
        state["blocked"] = True
        state["block_reason"] = mod_check.reason
        state["block_category"] = mod_check.category
        state["flagged_terms"] = mod_check.flagged_terms
        conversation.state = state

        # Record user message and assistant safety warning in chat history
        db.add(Message(conversation_id=conversation.id, role=MessageRole.USER, content=text))
        warning_reply = (
            f"⚠️ {mod_check.reason}\n\n"
            "This search session has been locked and terminated due to a violation of our safety policies. "
            "You cannot continue chatting in this session."
        )
        assistant = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=warning_reply,
        )
        db.add(assistant)
        seq = 0
        db.add(_record(assistant, seq, "moderation_blocked", warning_reply, {
            "category": mod_check.category,
            "flagged_terms": mod_check.flagged_terms,
        }))

        await db.commit()
        await db.refresh(conversation)

        return DirectSearchResult(
            conversation=conversation,
            requirements=_load_requirements(conversation),
            reply=warning_reply,
            results=[],
            total=0,
            pending=None,
            search_id=None,
            notes=[],
            blocked=True,
            block_reason=mod_check.reason,
            block_category=mod_check.category,
        )

    requirements = _load_requirements(conversation)
    product_before = requirements.product
    category_before = requirements.category
    pending_before: Optional[str] = state.get("pending")
    confirming_before: bool = bool(state.get("confirming_product"))

    # Load conversation history for the LLM agent
    history_records = (
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc())
        )
    ).all()
    history_dicts = [
        {"role": m.role.value if hasattr(m.role, "value") else str(m.role), "content": m.content}
        for m in history_records
        if m.content
    ]

    db.add(Message(conversation_id=conversation.id, role=MessageRole.USER, content=text))

    # Autonomous LLM Search Agent: decides what to ask next, manages pivots, and calls search_marketplace
    agent_decision: Optional[conversation_llm.SearchAgentDecision] = None
    if settings.DIRECT_SEARCH_LLM:
        agent_decision = await conversation_llm.run_search_agent(
            history=history_dicts,
            current_requirements=requirements,
            user_message=text,
            already_searched=bool(state.get("searched")),
        )

    if agent_decision is not None:
        requirements = agent_decision.updated_requirements
        assistant = Message(
            conversation_id=conversation.id, role=MessageRole.ASSISTANT, content=""
        )
        db.add(assistant)
        sequence = 0
        db.add(_record(assistant, sequence, "status", "Understanding your request", {"thought": agent_decision.thought or ""}))

        results: list[MatchCandidate] = []
        total = 0
        search_id: Optional[uuid.UUID] = None
        notes: list[str] = []

        if agent_decision.action == "search_marketplace" and requirements.product:
            sequence += 1
            db.add(
                _record(
                    assistant, sequence, "tool_call", "Searching the marketplace",
                    {"tool": "search_marketplace", "parameters": requirements.model_dump(mode="json")},
                )
            )
            probe = build_probe_rfq(requirements, user.id)
            response = await match_service.run_match(
                db, user, probe,
                rfq_id=None,
                conversation_id=conversation.id,
                query=text,
                source="direct_search",
                limit=limit,
                offset=0,
            )
            results, total, search_id = response.results, response.total, response.search_id
            notes = _currency_note(requirements, results)
            pending = None
            reply = _compose_reply(requirements, total, len(results), pending, notes)
            sequence += 1
            db.add(
                _record(
                    assistant, sequence, "tool_result", f"Found {total} matches",
                    {"count": total, "search_id": str(search_id)},
                )
            )
            sequence += 1
            db.add(
                _record(
                    assistant, sequence, "search_results", "",
                    {"results": [c.model_dump(mode="json") for c in results]},
                )
            )
            state["searched"] = True
            state["pending"] = None
            state["confirming_product"] = False
        else:
            reply = agent_decision.reply or "Could you specify what product or details you need?"
            pending = "clarify"
            state["pending"] = pending

        assistant.content = reply
        sequence += 1
        db.add(_record(assistant, sequence, "done", "", {}))

        state["requirements"] = requirements.model_dump(mode="json")
        conversation.state = state
        if conversation.title is None and requirements.product:
            conversation.title = f"Search: {requirements.product}"[:300]

        await db.commit()
        await db.refresh(conversation)

        return DirectSearchResult(
            conversation=conversation,
            requirements=requirements,
            reply=reply,
            results=results,
            total=total,
            pending=pending,
            search_id=search_id,
            notes=notes,
        )

    # --- Fallback to deterministic engine when LLM is unavailable or offline ---
    # Ask the model what the reply meant; fall back to keyword matching when it
    # is unavailable or too slow.
    intent = await conversation_llm.interpret(text, pending_before)
    if intent is None:
        intent = (
            "show_results" if _is_show_now(text)
            else "skip" if (_is_skip(text) and pending_before)
            else "answer"
        )

    if intent == "show_results":
        # Retire every outstanding question and search with what is known.
        for field in _missing_fields(requirements):
            requirements.skipped.append(field)
    elif intent == "skip" and pending_before:
        if pending_before not in requirements.skipped:
            requirements.skipped.append(pending_before)
    else:
        # A reply to a specific question is read as an answer to that field
        # only. Parsing it as a whole new query is how "i need it before 30 sep"
        # ended up setting the product to "sep" and the quantity to 30.
        scoped = None
        if pending_before in QUESTION_ORDER and not confirming_before:
            scoped = parse_answer(pending_before, text)
            if scoped is None:
                # Check other missing fields first, then any other field,
                # but only if the user is not stating a new product or pivot.
                cand_parsed = extract(text)
                user_stated_product = bool(
                    cand_parsed.product
                    and cand_parsed.product.lower() != (requirements.product or "").lower()
                )
                if not user_stated_product:
                    missing_fields = [f for f in QUESTION_ORDER if not requirements.known(f) and f != pending_before]
                    other_candidates = missing_fields + [f for f in QUESTION_ORDER if f != pending_before and f not in missing_fields]
                    for other_field in other_candidates:
                        cand = parse_answer(other_field, text)
                        if cand is not None:
                            scoped = cand
                            break

        if scoped is not None:
            requirements = merge_answer(requirements, scoped)
        else:
            parsed = extract(text)
            if settings.DIRECT_SEARCH_LLM:
                parsed = await llm_extractor.enrich(text, parsed)
            requirements = merge(requirements, parsed, text)

    results: list[MatchCandidate] = []
    total = 0
    search_id: Optional[uuid.UUID] = None
    notes: list[str] = []

    assistant = Message(
        conversation_id=conversation.id, role=MessageRole.ASSISTANT, content=""
    )
    db.add(assistant)
    sequence = 0
    db.add(_record(assistant, sequence, "status", "Understanding your request", {}))

    missing = _missing_fields(requirements)
    already_searched = bool(state.get("searched"))

    product_now = requirements.product
    product_changed = bool(
        product_before and product_now and product_now.lower() != product_before.lower()
    )
    is_broad = is_broad_product(product_now)
    explicit_search_permission = _is_show_now(text) or any(
        w in text.lower().split() for w in ("search", "find", "yes", "proceed", "same", "keep", "sure", "ok", "okay")
    )

    if not requirements.product:
        pending = None
        reply = (
            "What product are you looking for? For example: "
            "“white USB type-c cables in Indore within 7 days”."
        )

    elif confirming_before:
        # User is answering the product confirmation / clarification prompt
        if explicit_search_permission or not is_broad:
            state["confirming_product"] = False
            state["searched"] = True
            sequence += 1
            db.add(
                _record(
                    assistant, sequence, "tool_call", "Searching the marketplace",
                    {"tool": "search_marketplace"},
                )
            )
            probe = build_probe_rfq(requirements, user.id)
            response = await match_service.run_match(
                db, user, probe,
                rfq_id=None,
                conversation_id=conversation.id,
                query=text,
                source="direct_search",
                limit=limit,
                offset=0,
            )
            results, total, search_id = response.results, response.total, response.search_id
            notes = _currency_note(requirements, results)
            pending = None
            reply = _compose_reply(requirements, total, len(results), pending, notes)
            sequence += 1
            db.add(
                _record(
                    assistant, sequence, "tool_result", f"Found {total} matches",
                    {"count": total, "search_id": str(search_id)},
                )
            )
            sequence += 1
            db.add(
                _record(
                    assistant, sequence, "search_results", "",
                    {"results": [c.model_dump(mode="json") for c in results]},
                )
            )
        else:
            examples = _CATEGORY_EXAMPLES.get(requirements.category or "Agriculture", "specific items")
            reply = (
                f"Could you specify which item you need (e.g., {examples})? "
                "Or say “search” to find all suppliers right now."
            )
            pending = "clarify_product"

    elif product_changed and (is_broad or not explicit_search_permission):
        # Product changed mid-conversation! Pause RAG and confirm details or clarify broad term
        state["confirming_product"] = True
        state["searched"] = False
        if is_broad:
            category_label = requirements.category or "Agriculture"
            examples = _CATEGORY_EXAMPLES.get(category_label, "specific items")
            reply = (
                f"Got it — switching to {category_label} ({requirements.product}). "
                f"Which specific item are you looking for (for example: {examples})? "
                "Also, would you like to specify target quantity and price, or any configurations like organic or grade? "
                "(Or say “search” to find all sellers now.)"
            )
            pending = "clarify_product"
        elif category_before and requirements.category and requirements.category != category_before:
            reply = (
                f"Got it — switching to {requirements.product} ({requirements.category}). "
                "Would you like to set your target quantity and price for this item, or any specific requirements (such as organic, grade, or variety)? "
                "(Or say “search” to find sellers now.)"
            )
            pending = "confirm_details"
        else:
            carried = []
            if requirements.quantity_value:
                carried.append(f"{requirements.quantity_value:,} {requirements.quantity_unit or 'units'}")
            if requirements.price_amount:
                carried.append(f"{requirements.price_amount:,} {requirements.price_currency or 'INR'}")
            carried_str = f" ({', '.join(carried)})" if carried else ""
            loc_str = f" in {requirements.city}" if requirements.city else (f" in {requirements.state}" if requirements.state else "")
            reply = (
                f"Got it — switching to {requirements.product}{loc_str}. "
                f"Would you like to keep the same target details{carried_str}, or specify new requirements (such as length, color, or grade)? "
                "(Or say “search” to find sellers now.)"
            )
            pending = "confirm_details"

    elif missing and not already_searched:
        # Gather first, search once. The tool is not called until the picture is
        # complete, so the first table the user sees reflects everything they
        # said, rather than a partial guess they then have to correct.
        pending = missing[0]
        known = _acknowledge(requirements)
        lead = f"Got it — {known}." if known else "Got it."
        question = await compose_question(pending, requirements)
        tail = "" if len(missing) == 1 else f" ({len(missing)} quick questions left.)"
        reply = f"{lead} {question} (or say “skip”){tail}"
    else:
        sequence += 1
        db.add(
            _record(
                assistant, sequence, "tool_call", "Searching the marketplace",
                {"tool": "search_marketplace"},
            )
        )

        probe = build_probe_rfq(requirements, user.id)
        response = await match_service.run_match(
            db, user, probe,
            rfq_id=None,
            conversation_id=conversation.id,
            query=text,
            source="direct_search",
            limit=limit,
            offset=0,
        )
        results, total, search_id = response.results, response.total, response.search_id
        notes = _currency_note(requirements, results)

        pending = None
        reply = _compose_reply(requirements, total, len(results), pending, notes)

        sequence += 1
        db.add(
            _record(
                assistant, sequence, "tool_result", f"Found {total} matches",
                {"count": total, "search_id": str(search_id)},
            )
        )
        sequence += 1
        # Structured, not stringified JSON inside a text event -- the frontend
        # renders a table from this.
        db.add(
            _record(
                assistant, sequence, "search_results", "",
                {"results": [c.model_dump(mode="json") for c in results]},
            )
        )

    assistant.content = reply
    sequence += 1
    db.add(_record(assistant, sequence, "done", "", {}))

    state["requirements"] = requirements.model_dump(mode="json")
    state["pending"] = pending
    state["searched"] = (already_searched and not state.get("confirming_product")) or bool(search_id)
    conversation.state = state
    if conversation.title is None and requirements.product:
        conversation.title = f"Search: {requirements.product}"[:300]

    await db.commit()
    await db.refresh(conversation)

    return DirectSearchResult(
        conversation=conversation,
        requirements=requirements,
        reply=reply,
        results=results,
        total=total,
        pending=pending,
        search_id=search_id,
        notes=notes,
    )

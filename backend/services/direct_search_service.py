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
from services import conversation_llm, llm_extractor, match_service
from services.query_extractor import (
    Requirements,
    extract,
    merge,
    merge_answer,
    parse_answer,
)
from services.rfq_indexing import build_search_tags, build_search_text

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
    ) -> None:
        self.conversation = conversation
        self.requirements = requirements
        self.reply = reply
        self.results = results
        self.total = total
        self.pending = pending
        self.search_id = search_id
        self.notes = notes


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
        listed = ", ".join(sorted(theirs))
        return [
            f"Note: your target is in {wanted} but these are quoted in {listed}, "
            "so price was not scored."
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
    requirements = _load_requirements(conversation)
    pending_before: Optional[str] = state.get("pending")

    db.add(Message(conversation_id=conversation.id, role=MessageRole.USER, content=text))

    # Ask the model what the reply meant; fall back to keyword matching when it
    # is unavailable or too slow. "doesn't really matter, just show me what's
    # out there" is a sentence no word list handles well.
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
        scoped = parse_answer(pending_before, text) if pending_before else None
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

    if not requirements.product:
        pending = None
        reply = (
            "What product are you looking for? For example: "
            "“white USB type-c cables in Indore within 7 days”."
        )

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
    # Sticky: once the search has run, later messages refine live rather than
    # dropping the user back into a questionnaire.
    state["searched"] = already_searched or bool(search_id)
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

"""Conversation storage for the chat: create, save messages, store the turn's state, history, list/get/delete, mark-sent; plus the turn result types."""


from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.conversation import Conversation, Message
from models.enums import ConversationType, MessageRole



logger = logging.getLogger(__name__)


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


MAX_STORED_HISTORY = 24  # stored messages read back for the prompt window and the previous assistant turn


_ERROR_REPLY_MARKERS = ("[Connection to local AI assistant interrupted.]", "[Response timed out.")


async def _stored_history(db: AsyncSession, conversation_id: uuid.UUID) -> list[dict[str, str]]:
    """The conversation's latest turns from the database, oldest first.

    Error notices an older version stored as assistant turns are skipped, so they never reach a prompt.
    """
    try:
        rows = (
            await db.execute(
                select(Message.role, Message.content)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc())
                .limit(MAX_STORED_HISTORY)
            )
        ).all()
    except Exception as exc:
        logger.warning("Could not read the stored history: %s", exc)
        return []
    out: list[dict[str, str]] = []
    for role, content in reversed(rows):
        role_value = role.value if hasattr(role, "value") else str(role)
        text_value = str(content or "")
        if role_value not in ("user", "assistant") or not text_value.strip():
            continue
        if role_value == "assistant" and any(m in text_value for m in _ERROR_REPLY_MARKERS):
            continue
        out.append({"role": role_value, "content": text_value})
    return out


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
    quick_replies: Optional[list[str]] = None

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
            "quick_replies": self.quick_replies,
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

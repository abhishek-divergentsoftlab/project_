"""The AI chat's entry points: one turn per request, serialised per conversation, run by ``chat_agent.engine``.

``/message`` and ``/stream`` call ``generate_business_chat_reply`` / ``stream_business_chat_reply``; the
conversation list, load, delete and mark-sent endpoints use the storage functions of ``chat_agent.persistence``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncIterator, Optional, Union

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User
from services.agent_registry import GENERAL_ASSISTANT, AgentDefinition, agent_info
from services.chat_agent import rfq_draft
from services.chat_agent.persistence import (  # noqa: F401  (the API imports these from here)
    ChatEvent, TurnResult, delete_user_conversation, get_conversation_with_messages, list_user_conversations,
    mark_counterparty_message_sent,
)

logger = logging.getLogger(__name__)

# One chat turn is serialised per conversation and bounded in time.
# The deadline covers the model-dependent part of a turn (reading the message, the answer engine's model calls and
# tool rounds). Without it a stalled or swapped-out local model holds the request for minutes (each model call can
# wait 180 s, several rounds per turn) while the user stares at a spinner; with it the user gets a clear "busy" reply
# and their draft is untouched.
CHAT_TURN_DEADLINE_SECONDS = 75
CHAT_TURN_LOCK_WAIT_SECONDS = 90  # a second turn on the same conversation waits this long for the first to finish
_BUSY_REPLY = "The assistant is busy right now — your draft is saved; please try again in a minute."
_UNAVAILABLE_REPLY = "The assistant can't be reached right now — your draft is saved; please try again in a minute."


class _ConversationLock:
    """Session-level Postgres advisory lock on a dedicated connection, one holder per conversation id.

    A turn commits several times, so a ``SELECT ... FOR UPDATE`` row lock (released by the first commit) cannot cover
    it; a lock on its own connection lives until released, whatever the turn's session does. Two tabs, or a double
    Enter, therefore run one after the other: the second turn reads the state the first one stored.
    """

    def __init__(self, conversation_id: uuid.UUID) -> None:
        self.key = conversation_id.int % (2 ** 63)
        self._conn: Any = None

    async def acquire(self, timeout: float) -> bool:
        from db.session import engine

        try:
            self._conn = await engine.connect()
            deadline = time.monotonic() + timeout
            while True:
                got = (await self._conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key})).scalar()
                if got:
                    return True
                if time.monotonic() >= deadline:
                    await self.release(unlock=False)
                    return False
                await asyncio.sleep(0.1)
        except Exception as exc:  # the lock is a safeguard: when it cannot be taken the turn still runs
            logger.warning("Could not take the conversation lock: %s", exc)
            await self.release(unlock=False)
            return True

    async def release(self, unlock: bool = True) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            if unlock:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
        except Exception as exc:
            logger.debug("advisory unlock failed: %s", exc)
            try:
                await conn.invalidate()  # never hand a connection that may still hold the lock back to the pool
            except Exception:
                pass
        finally:
            try:
                await asyncio.shield(conn.close())
            except Exception:
                pass



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
    """Run one chat turn under its conversation's lock: the agent engine."""
    lock = _ConversationLock(conversation_id) if (db is not None and user is not None and conversation_id) else None
    try:
        if lock is not None and not await lock.acquire(CHAT_TURN_LOCK_WAIT_SECONDS):
            logger.warning("Chat turn gave up waiting for conversation %s", conversation_id)
            yield _busy_event(conversation_id)
            return
        from services.chat_agent import engine as agent_engine  # late: the engine imports this package's storage

        async for event in agent_engine.run_turn(
            messages, current_rfq, db, user, conversation_id, matched_candidates, active_connection_id, agent, rfq_id
        ):
            yield event
    finally:
        if lock is not None:
            await lock.release()



def _busy_event(conversation_id: Optional[uuid.UUID]) -> ChatEvent:
    """The final event of a turn that did nothing: the busy notice, and no state or history change."""
    info = agent_info(GENERAL_ASSISTANT)
    readiness = rfq_draft.evaluate_rfq_readiness({})
    chunk: dict[str, Any] = {
        "content": _BUSY_REPLY, "thinking": "", "thinking_after": "", "tool_step": None, "rfq_draft": None,
        "readiness": readiness, "created_rfq": None, "counterparty_message": None, "done": True, "routed_agent": info,
    }
    if conversation_id:
        chunk["conversation_id"] = str(conversation_id)
    result = TurnResult(
        reply=_BUSY_REPLY, thinking=None, rfq_draft={}, readiness=readiness, created_rfq=None,
        counterparty_message=None, conversation_id=conversation_id, routed_agent=info,
    )
    return ChatEvent("error", chunk, result)


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

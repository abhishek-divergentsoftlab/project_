"""One chat turn as a model loop: the model reads the conversation, the draft and tool results and decides everything.

``run_turn`` has the signature and events the API expects (it runs under the per-conversation lock of ``ai_chat_service``), so ``/message`` and ``/stream`` work unchanged. Code only validates what the model does through
tools (``tools`` / ``guards``); it never reads the user's intent.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Union

from core.config import settings
from services import llm_client
from services.agent_registry import GENERAL_ASSISTANT, AgentDefinition, agent_info
from services.chat_agent import actions, draft_assist, guards, negotiation, persistence, prompt, recaps, rfq_draft, tools
from services.chat_agent.state import AgentState
from services.untrusted_text import as_data

logger = logging.getLogger(__name__)

MAX_STEPS = 5  # model calls per turn; the last one is offered no tools so it has to answer
MAX_CALLS_PER_STEP = 4
HISTORY_MESSAGES = 14  # stored turns sent to the model
MAX_HISTORY_CHARS = 1500
MAX_GROUNDING_USER_TURNS = 12  # earlier user text a ``said`` may quote
TURN_DEADLINE_SECONDS = 120  # model-dependent part of a turn (a cold model load alone takes ~45 s)
MIN_CALL_SECONDS = 5.0
MAX_TOOL_RESULT_CHARS = 7000
STREAM_FLUSH_SECONDS = 0.08  # text is sent to the client in batches of this age

_ERROR_REPLIES = {
    "busy": (
        "The assistant is busy right now — your draft is saved; please try again in a minute.",
        "Assistant abhi busy hai — aapka draft save hai; thodi der baad phir try kijiye.",
    ),
    "timeout": (
        "The assistant took too long to answer — your draft is saved; please try again.",
        "Assistant ko jawab dene mein bahut time laga — aapka draft save hai; kripya dobara try kijiye.",
    ),
    "unavailable": (
        "The assistant can't be reached right now — your draft is saved; please try again in a minute.",
        "Assistant abhi available nahi hai — aapka draft save hai; thodi der baad phir try kijiye.",
    ),
}
_EMPTY_REPLY = (
    "Tell me what you want to buy or sell (product, quantity, place) and I will help.",
    "Bataiye aap kya kharidna ya bechna chahte hain (product, quantity, jagah), main madad karunga.",
)
_NO_ANSWER_REPLY = (
    "Sorry, I could not put an answer together. Could you say that again?",
    "Maaf kijiye, main jawab nahi bana paya. Kya aap dobara bata sakte hain?",
)


@dataclass
class _Step:
    """What one model call returned."""

    content: str = ""
    thinking: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    streamed: bool = False  # some of ``content`` already went to the wire


async def _parts(res: Any) -> AsyncIterator[dict[str, Any]]:
    """The model's reply as stream parts, whether ``llm_client.chat`` returned a stream or an awaitable."""
    if inspect.isawaitable(res):
        whole = await res
        yield {"content": whole.get("content") or "", "thinking": whole.get("thinking") or ""}
        yield {"done": True, "tool_calls": whole.get("tool_calls") or []}
        return
    async for part in res:
        yield part


def _model_view(out: dict[str, Any]) -> str:
    """A tool result as the model sees it: everything but the wire-only ``ui``."""
    shown = {k: v for k, v in out.items() if k != "ui"}
    result = shown.get("result")
    if shown.get("ok") and isinstance(result, dict) and result.get("rejected"):  # fields NOT saved: first, never truncated away
        rest = {k: v for k, v in result.items() if k not in ("rejected", "rejected_note")}
        shown = {"ok": True, "REJECTED_FIELDS_NOT_SAVED": result["rejected"], "do_now": result.get("rejected_note"), "result": rest}
    text = json.dumps(shown, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_TOOL_RESULT_CHARS else text[:MAX_TOOL_RESULT_CHARS] + "…(truncated)"


class _Turn:
    """The state and events of one chat turn."""

    def __init__(
        self,
        messages: list[Any],
        current_rfq: Optional[dict[str, Any]],
        db: Any,
        user: Any,
        conversation_id: Optional[uuid.UUID],
        matched_candidates: Optional[list[dict[str, Any]]],
        active_connection_id: Optional[Union[str, uuid.UUID]],
        rfq_id: Optional[uuid.UUID],
    ) -> None:
        self.messages, self.current_rfq = messages, current_rfq
        self.db, self.user = db, user
        self.conversation_id, self.rfq_id = conversation_id, rfq_id
        self.panel_open = bool(matched_candidates)
        self.active_connection_id = active_connection_id
        self.info = agent_info(GENERAL_ASSISTANT)
        self.deadline = time.monotonic() + TURN_DEADLINE_SECONDS
        self.conversation: Any = None
        self.history: list[dict[str, str]] = []
        self.last_user = ""
        self.state = AgentState()
        self.assist = draft_assist.DraftAssist({})
        self.ctx = tools.ToolContext(state=self.state, assist=self.assist, user_texts=[])
        self.focus: Optional[str] = None
        self.negotiations: Optional[dict[str, Any]] = None
        self.own_rfqs: list[dict[str, Any]] = []
        self.thinking = ""
        self.final_text = ""  # the model's last text step
        self.streamed_text = ""  # what the client has accumulated from content chunks
        self.last_step: Optional[dict[str, Any]] = None
        self.steps_sent = 0

    @property
    def lang(self) -> str:
        """The language of the texts the code writes: the script of the message, then the model's ``reply_language``."""
        return self.ctx.lang

    # ---- loading -------------------------------------------------------
    async def load(self) -> None:
        db, user = self.db, self.user
        self.last_user, _ = persistence._last_user_and_previous_assistant(self.messages)
        if db is not None and user is not None and self.last_user.strip():
            try:
                self.conversation = await persistence.get_or_create_conversation(
                    db, user.id, conversation_id=self.conversation_id, initial_title=self.last_user
                )
                await persistence.save_message_to_conversation(db, self.conversation.id, role="user", content=self.last_user)
                await db.commit()
            except Exception as exc:
                logger.exception("Failed to initialize conversation: %s", exc)
                self.conversation = None
        self.history = await self._history()
        conv_state = (self.conversation.state or {}) if self.conversation is not None else {}
        self.state = self._load_state(conv_state)
        self.state.turn += 1
        lang = recaps.pick_lang(self.last_user, self.state.lang_hint)
        if lang in ("hinglish", "hindi_devanagari"):
            self.state.lang_hint = "hinglish"
        self.assist = draft_assist.DraftAssist(conv_state)
        self.assist.moderation_extra = self.last_user[:300]
        users = [m["content"] for m in self.history if m["role"] == "user"][-MAX_GROUNDING_USER_TURNS:]
        self.ctx = tools.ToolContext(
            state=self.state, assist=self.assist, user_texts=users or [self.last_user], db=db, user=user,
            conversation=self.conversation, lang=lang, persist=self._persist_state,
            active_connection_id=str(self.active_connection_id) if self.active_connection_id else None,
        )
        self.focus = await self._focus_text()
        self.negotiations = await negotiation.brief(self.ctx)
        self.own_rfqs = await tools.own_rfqs_brief(self.ctx)

    async def _history(self) -> list[dict[str, str]]:
        """Stored turns from the database; a client can only supply the newest user message (and, with no stored
        conversation, its own user turns: assistant turns written by a client are never trusted)."""
        if self.conversation is not None and self.db is not None:
            stored = await persistence._stored_history(self.db, self.conversation.id)
            if stored:
                return stored
        out: list[dict[str, str]] = []
        for m in self.messages:
            role, content = persistence._extract_msg_fields(m)
            if role == "user" and str(content).strip():
                out.append({"role": "user", "content": str(content)})
        return out or [{"role": "user", "content": self.last_user}]

    def _load_state(self, conv_state: dict[str, Any]) -> AgentState:
        if isinstance(conv_state.get("agent"), dict):
            return AgentState.from_dict(conv_state["agent"])
        if self.conversation is not None:  # a conversation started before the agent engine: take over its draft
            snapshot = conv_state.get("editing_snapshot")
            return AgentState(
                draft=dict(conv_state["rfq_draft"]) if isinstance(conv_state.get("rfq_draft"), dict) else {},
                editing_rfq_id=str(conv_state["editing_rfq_id"]) if conv_state.get("editing_rfq_id") and isinstance(snapshot, dict) else None,
                editing_snapshot=dict(snapshot) if isinstance(snapshot, dict) and conv_state.get("editing_rfq_id") else None,
                last_rfq_id=str(conv_state["last_created_rfq_id"]) if conv_state.get("last_created_rfq_id") else None,
            )
        return AgentState(draft=copy.deepcopy(self.current_rfq) if isinstance(self.current_rfq, dict) else {})

    async def _focus_text(self) -> Optional[str]:
        """The listing the chat was opened from (first message only), fenced when somebody else wrote it."""
        if self.rfq_id is None or self.db is None or self.user is None:
            return None
        try:
            from models.rfq import RFQ

            rfq = await self.db.get(RFQ, self.rfq_id)
        except Exception as exc:
            logger.debug("focus RFQ not loaded: %s", exc)
            return None
        if rfq is None:
            return None
        if rfq.user_id == self.user.id:
            return f"the user's own RFQ {rfq.id} '{rfq.title}' ({rfq.status.value})"
        if rfq.status.value != "active":
            return None
        return f"a listing by another user (id {rfq.id}): {as_data(str(rfq.title)[:120], 'listing_title')}"

    # ---- events --------------------------------------------------------
    def _wire_draft(self) -> dict[str, Any]:
        draft = self.state.draft
        if draft and self.assist.blocked_category(draft):
            return {}  # a prohibited product is never streamed to the UI
        return copy.deepcopy(draft)

    def chunk(
        self, *, content: str = "", thinking: str = "", tool_step: Optional[dict[str, Any]] = None,
        created: Optional[dict[str, Any]] = None, done: bool = False, replace: bool = False,
    ) -> dict[str, Any]:
        """One SSE chunk, with exactly the fields the legacy ``wire()`` emits."""
        draft = self._wire_draft()
        out: dict[str, Any] = {"content": content}
        if replace:
            out["replace_content"] = True
        out.update({
            "thinking": thinking, "thinking_after": "", "tool_step": tool_step, "rfq_draft": draft,
            "readiness": rfq_draft.evaluate_rfq_readiness(draft), "created_rfq": created,
            "counterparty_message": self.ctx.counterparty_message, "done": done,
        })
        if self.conversation is not None:
            out["conversation_id"] = str(self.conversation.id)
        out["routed_agent"] = self.info
        if done:
            shown = self._matched()
            if shown:
                out["matched_candidates"] = shown
            out["draft_suggestions"], out["draft_moderation"] = self.assist.public()
            out["editing_rfq_id"] = self.state.editing_rfq_id
        return out

    def _matched(self) -> list[dict[str, Any]]:
        if self.ctx.matched:
            return self.ctx.matched
        if self.panel_open and self.conversation is not None:
            stored = (self.conversation.state or {}).get("matched_candidates") or []
            return [c for c in stored if isinstance(c, dict)]
        return []

    def _tool_events(self) -> list["Any"]:
        """tool_step events for the steps the tools recorded since the last call."""
        events = []
        for step in self.ctx.steps[self.steps_sent:]:
            self.last_step = step
            created = self.ctx.created if step.get("status") == "completed" and step["name"] in (
                "create_rfq", "publish_rfq", "close_rfq", "save_rfq_edit") else None
            events.append(self._event("tool_step", self.chunk(tool_step=step, created=created)))
        self.steps_sent = len(self.ctx.steps)
        return events

    @staticmethod
    def _event(kind: str, chunk: dict[str, Any], result: Any = None) -> Any:
        return persistence.ChatEvent(kind, chunk, result)

    # ---- the model loop --------------------------------------------------
    def _call_timeout(self) -> float:
        left = self.deadline - time.monotonic()
        if left < MIN_CALL_SECONDS:
            raise llm_client.LLMTimeout("the turn's model time budget is used up")
        return min(float(settings.AI_CHAT_TIMEOUT_SECONDS), left)

    def _system_prompt(self) -> str:
        state, user = self.state, self.user
        draft = state.draft
        pending = state.pending_valid()
        shown = bool(pending and pending.turn < state.turn)  # a recap the user has already seen
        editing = {"id": state.editing_rfq_id, "title": (state.editing_snapshot or {}).get("title")} if state.editing_rfq_id else None
        return prompt.build_system_prompt(
            user_name=getattr(getattr(user, "profile", None), "name", None),
            postable_roles=tools.postable_roles(user),
            draft=draft,
            readiness=rfq_draft.evaluate_rfq_readiness(draft),
            open_specs=tools.open_specs(draft, self.assist),
            hsn_hint=tools.hsn_hint(draft, self.assist),
            pending={"action": pending.action, "recap": as_data(pending.recap, "recap")
                     if pending.action in negotiation.NEGOTIATION_ACTIONS else pending.recap} if pending and shown else None,
            editing=editing, focus=self.focus, lang_hint=state.lang_hint, can_act=self.ctx.can_act,
            negotiations=self.negotiations, own_rfqs=self.own_rfqs, latest_message=self.last_user,
        )

    def _chat_messages(self) -> list[dict[str, Any]]:
        turns = [m for m in self.history if m["role"] in ("user", "assistant")][-HISTORY_MESSAGES:]
        out = [{"role": m["role"], "content": m["content"][:MAX_HISTORY_CHARS]} for m in turns]
        if not out or out[-1]["role"] != "user":
            out.append({"role": "user", "content": self.last_user})
        return out

    async def _model_step(self, messages: list[dict[str, Any]], offer_tools: bool) -> AsyncIterator[Any]:
        """Call the model once; yield wire events as text arrives, then the ``_Step``."""
        step = _Step()
        stream = llm_client.chat(
            [{"role": "system", "content": self._system_prompt()}, *messages],
            tools.tool_specs() if offer_tools else None, timeout=self._call_timeout(), stream=True,
        )
        buffered = {"content": "", "thinking": ""}
        last_flush = time.monotonic()

        def flush() -> list[Any]:
            events = []
            if buffered["thinking"]:
                events.append(self._event("status", self.chunk(thinking=buffered["thinking"])))
            if buffered["content"]:
                self.streamed_text += buffered["content"]
                events.append(self._event("status", self.chunk(content=buffered["content"])))
            buffered["content"] = buffered["thinking"] = ""
            return events

        async for part in _parts(stream):
            if part.get("done"):
                step.calls = list(part.get("tool_calls") or [])
                continue
            for key in ("thinking", "content"):
                if part.get(key):
                    setattr(step, key, getattr(step, key) + part[key])
                    buffered[key] += part[key]
            step.streamed = step.streamed or bool(part.get("content"))
            if time.monotonic() - last_flush >= STREAM_FLUSH_SECONDS:  # token-sized chunks would each carry the whole draft
                last_flush = time.monotonic()
                for event in flush():
                    yield event
        for event in flush():
            yield event
        yield step

    async def _run_calls(self, calls: list[dict[str, Any]], messages: list[dict[str, Any]]) -> AsyncIterator[Any]:
        """Run the model's tool calls (read-only ones that touch nothing shared may run together)."""
        run, skipped = calls[:MAX_CALLS_PER_STEP], calls[MAX_CALLS_PER_STEP:]
        for call in run:
            note = tools.READ_ONLY_STATUS.get(call["name"])
            if note:
                yield self._event("status", self.chunk(thinking=note))
        together = len(run) > 1 and all(c["name"] in tools.TOOLS and tools.TOOLS[c["name"]].concurrent_ok for c in run)
        if together:
            outs = list(await asyncio.gather(*(tools.run_tool(c["name"], c["arguments"], self.ctx) for c in run)))
        else:
            outs = [await tools.run_tool(c["name"], c["arguments"], self.ctx) for c in run]
        for call, out in zip(run, outs):
            logger.info(
                "chat_agent tool %s args=%.300s -> ok=%s %.200s", call["name"],
                json.dumps(call["arguments"], ensure_ascii=False, default=str), out.get("ok"), out.get("error") or "",
            )
            messages.append({"role": "tool", "tool_name": call["name"], "content": _model_view(out)})
        for call in skipped:
            messages.append({"role": "tool", "tool_name": call["name"], "content": _model_view(tools._err("too many tool calls at once: call it again"))})
        events = self._tool_events()
        for event in events:
            yield event
        if not events and any((o.get("ui") or {}).get("rfq_draft") is not None for o in outs):
            yield self._event("status", self.chunk())  # the draft panel follows the draft

    async def _loop(self) -> AsyncIterator[Any]:
        messages = self._chat_messages()
        force_text = False
        for step_no in range(MAX_STEPS):
            offer_tools = step_no < MAX_STEPS - 1 and not force_text
            step = _Step()
            async for item in self._model_step(messages, offer_tools):
                if isinstance(item, _Step):
                    step = item
                else:
                    yield item
            self.thinking += step.thinking
            if not step.calls:
                self.final_text = self._without_markup(step.content)
                if self.final_text or step_no == MAX_STEPS - 1:
                    return
                messages.append({"role": "system", "content": "Answer the user now in plain text."})
                force_text = True
                continue
            if step.streamed:  # text before a tool call is not the answer: clear it from the screen
                self.streamed_text = ""
                yield self._event("replace_content", self.chunk(content="", replace=True))
            messages.append({
                "role": "assistant", "content": step.content, **({"thinking": step.thinking} if step.thinking else {}),
                "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in step.calls],
            })
            async for event in self._run_calls(step.calls, messages):
                yield event
        # tools used up the steps without a text answer: the final text stays empty and is built from the facts

    def _without_markup(self, content: str) -> str:
        """The model's answer without a tool call it wrote out as text; quick-reply options in it become real chips."""
        text, options, found = tools.strip_tool_markup(content)
        if found:
            logger.info("chat_agent: tool markup in the answer stripped: %s options=%s", sorted(set(found)), options)
            if options and len(options) >= 2 and not self.ctx.quick_replies:
                self.ctx.quick_replies = options
                self.ctx.new_shown.extend(options)
        if self.ctx.quick_replies:  # the chips are shown under the message: a block repeating them in the text is dropped
            text = tools.strip_chip_block(text, self.ctx.quick_replies)
        return text.strip()

    # ---- finishing -------------------------------------------------------
    def _compose(self) -> tuple[str, bool]:
        """The reply to show and whether it differs from the text already streamed (so it replaces it)."""
        ctx, state = self.ctx, self.state
        reply = self.final_text
        if ctx.forced_reply:
            reply = ctx.forced_reply
        else:
            pending = state.pending
            if ctx.recap and pending is not None and pending.recap == ctx.recap:
                if pending.facts:  # the model may present the recap in the user's language, if every value is in it
                    if guards.missing_facts(reply, pending.facts):
                        reply = f"{reply}\n\n{ctx.recap}".strip()
                elif ctx.recap not in reply:
                    reply = ctx.recap  # nothing to compare: the recap is shown as built
            for needle, fact in ctx.facts:
                if needle not in reply and guards.canon(needle) not in guards.canon(reply):
                    reply = f"{fact}\n\n{reply}".strip()
        if not reply.strip():
            readiness = rfq_draft.evaluate_rfq_readiness(state.draft)
            reply = (
                actions.format_rfq_summary_text(state.draft, readiness) if state.draft
                else _NO_ANSWER_REPLY[1 if recaps.latin_lang(self.lang) == "hinglish" else 0]
            )
        return reply, reply.strip() != self.streamed_text.strip()

    def _result(self, reply: str, chips: list[str], created: Optional[dict[str, Any]], chunk: dict[str, Any]) -> Any:
        return persistence.TurnResult(
            reply=reply, thinking=self.thinking or None, rfq_draft=chunk["rfq_draft"], readiness=chunk["readiness"],
            created_rfq=created, counterparty_message=self.ctx.counterparty_message,
            conversation_id=self.conversation.id if self.conversation is not None else None, routed_agent=self.info,
            matched_candidates=self._matched() or None, draft_suggestions=chunk.get("draft_suggestions"),
            draft_moderation=chunk.get("draft_moderation"), editing_rfq_id=self.state.editing_rfq_id,
            quick_replies=chips or None,
        )

    async def _persist_state(self) -> None:
        """Store the agent state alone (before a binding action runs, so it can never run twice)."""
        if self.db is None or self.conversation is None:
            return
        try:
            await persistence.update_conversation_after_turn(self.db, self.conversation, state_patch={"agent": self.state.to_dict()})
        except Exception as exc:
            logger.exception("Could not store the agent state: %s", exc)

    async def _persist_turn(self, reply: str, created: Optional[dict[str, Any]], *, message: bool) -> None:
        if self.db is None or self.conversation is None:
            return
        state = self.state
        try:
            if message:
                await persistence.save_message_to_conversation(
                    self.db, self.conversation.id, role="assistant", content=reply,
                    metadata={
                        "thinking": self.thinking or None, "thinking_after": None, "tool_step": self.last_step,
                        "created_rfq": created, "counterparty_message": self.ctx.counterparty_message,
                        "routed_agent": self.info,
                    },
                )
            patch: dict[str, Any] = {"agent": state.to_dict()}
            if created and created.get("id"):
                patch.update(
                    last_created_rfq_id=created["id"], last_created_rfq_title=created.get("title"),
                    last_created_rfq_status=created.get("status"),
                )
            await persistence.update_conversation_after_turn(
                self.db, self.conversation, draft=state.draft,
                readiness=rfq_draft.evaluate_rfq_readiness(state.draft),
                rfq_id=uuid.UUID(created["id"]) if created and created.get("id") else None,
                user_prompt=self.last_user, matched_candidates=self.ctx.matched,
                active_connection_id=self.active_connection_id,
                draft_suggestions=self.assist.suggestions, draft_moderation=self.assist.moderation or {},
                state_patch=patch,
            )
        except Exception as exc:
            logger.exception("Failed to persist turn: %s", exc)

    async def finish(self) -> Any:
        """Settle the background refresh, shape the reply, persist the turn once, build the final event."""
        await tools.settle(self.ctx)
        reply, replace = self._compose()
        ctx = self.ctx
        self.state.remember_shown(ctx.new_shown)
        chips = [] if ctx.forced_reply else list(ctx.quick_replies or [])
        created = ctx.created
        await self._persist_turn(reply, created, message=True)
        chunk = self.chunk(content=reply if replace else "", tool_step=self.last_step, created=created, done=True, replace=replace)
        if chips:
            chunk["quick_replies"] = chips
        return self._event("final", chunk, self._result(reply, chips, created, chunk))

    async def fail(self, kind: str) -> Any:
        """An honest short error: the model could not be used. Nothing the user said is lost, nothing is claimed."""
        ctx = self.ctx
        created = ctx.created
        if created and ctx.facts:  # an RFQ went through before the model failed: say so, from the facts, and keep it
            reply = ctx.facts[-1][1]
            await self._persist_turn(reply, created, message=True)
        else:
            reply = _ERROR_REPLIES.get(kind, _ERROR_REPLIES["unavailable"])[1 if recaps.latin_lang(self.lang) == "hinglish" else 0]
            if ctx.mutated:  # a tool already changed the draft: keep it (an error notice is never stored as a message)
                await tools.settle(ctx)
                await self._persist_turn(reply, None, message=False)
        chunk = self.chunk(content=reply, tool_step=self.last_step, created=created, done=True, replace=bool(self.streamed_text))
        return self._event("error", chunk, self._result(reply, [], created, chunk))

    async def run(self) -> AsyncIterator[Any]:
        try:
            await self.load()
            if not self.last_user.strip():
                self.final_text = _EMPTY_REPLY[1 if recaps.latin_lang(self.lang) == "hinglish" else 0]
                yield await self.finish()
                return
            async for event in self._loop():
                yield event
            yield await self.finish()
        except llm_client.LLMError as exc:
            logger.info("Chat agent: model unavailable (%s)", exc)
            yield await self.fail(exc.kind)
        except Exception as exc:
            logger.exception("Chat agent turn failed: %s", exc)
            yield await self.fail("busy")
        finally:
            task = self.ctx.refresh_task
            if task is not None and not task.done():
                task.cancel()


async def run_turn(
    messages: list[Any],
    current_rfq: Optional[dict[str, Any]] = None,
    db: Any = None,
    user: Any = None,
    conversation_id: Optional[uuid.UUID] = None,
    matched_candidates: Optional[list[dict[str, Any]]] = None,
    active_connection_id: Optional[Union[str, uuid.UUID]] = None,
    agent: Optional[AgentDefinition] = None,
    rfq_id: Optional[uuid.UUID] = None,
) -> AsyncIterator[Any]:
    """Run one chat turn and yield its ``ChatEvent``s (the contract of ``ai_chat_service._run_chat_turn``).

    The newest user message comes from ``messages``; the history and the agent state come from the database.
    """
    turn = _Turn(messages, current_rfq, db, user, conversation_id, matched_candidates, active_connection_id, rfq_id)
    async for event in turn.run():
        yield event

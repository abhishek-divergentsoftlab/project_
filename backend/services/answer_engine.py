"""Tool loop plus verification: how the chat produces an answer it can stand behind.

The model is given read-only data tools (``market_data_tools``) and, when the
router granted them, the existing action tools (draft/create/close/message).
It may call tools for up to ``MAX_TOOL_ROUNDS`` rounds, then writes its answer.
The answer is checked by ``answer_verifier``: numbers must trace to a tool
result, the user's message or the RFQ draft. A failing answer gets exactly one
retry that names the offending numbers; a second failure is replaced by a
deterministic answer built only from the gathered facts. ``answer`` never
raises: if Ollama is down the facts gathered so far are rendered instead.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.user import User
from services import answer_verifier, drafting_reply, market_data_tools, rfq_tool_service
from services.answer_verifier import Verification

logger = logging.getLogger(__name__)
timing_log = logging.getLogger("chat.timing")

MAX_TOOL_ROUNDS = 4
DRAFT_ACTION_TOOLS = frozenset({"update_rfq_draft", "create_rfq"})

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
ProgressCallback = Callable[[dict[str, Any]], Union[None, Awaitable[None]]]


@dataclass
class AnswerContext:
    """What the caller knows beyond the chat history."""

    rfq_id: Optional[str] = None
    # The live RFQ draft; its values may be repeated back to the user.
    draft: Union[None, dict[str, Any], Callable[[], dict[str, Any]]] = None
    # Other text whose numbers the answer may quote (e.g. the matched-candidate cards in the prompt, the suggested specs
    # and their options). A callable is read when the answer is verified, so late-arriving facts still count.
    extra_allowed_text: Union[str, Callable[[], str]] = ""
    on_progress: Optional[ProgressCallback] = None
    # True when the user is drafting an RFQ this turn (parser intent buy_request / sell_offer).
    drafting: bool = False
    # The draft as it was BEFORE this turn's model tool call. When set, the verifier takes the draft's
    # values from it, so the model cannot make a number "allowed" by writing it into the draft itself.
    verify_draft: Union[None, dict[str, Any], Callable[[], dict[str, Any]]] = None
    # The conversation's next step for the live draft, used by the drafting fallback reply: the next
    # question, "" when the caller appends the recap itself, None when there is no flow (legacy wording).
    drafting_closing: Optional[Callable[[dict[str, Any]], Optional[str]]] = None
    # Awaited just before an answer is verified: the caller's background work (the draft helpers' suggestions) that
    # feeds ``extra_allowed_text`` is finished by then, so a suggestion the reply quotes is never "unverified".
    before_verify: Optional[Callable[[], Awaitable[None]]] = None
    # The caller words the whole reply itself (a drafting turn: the acknowledgement is computed from the draft), so
    # once the model's tool calls have been run there is no second model call for text.
    tool_round_only: bool = False


@dataclass
class AnswerResult:
    text: str
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    verification: Verification = field(default_factory=lambda: Verification(ok=True))
    used_fallback: bool = False
    retried: bool = False
    action_results: list[dict[str, Any]] = field(default_factory=list)
    thinking: Optional[str] = None
    # True when the model could not be reached (the text is then only a facts-only rendering).
    llm_error: bool = False
    # Why the answer was or was not shown as written: ok, retried, used_fallback, unverified, banned, llm_error.
    diagnostics: dict[str, Any] = field(default_factory=dict)


async def _progress(context: Optional[AnswerContext], event: dict[str, Any]) -> None:
    if context is None or context.on_progress is None:
        return
    try:
        out = context.on_progress(event)
        if inspect.isawaitable(out):
            await out
    except Exception as exc:  # progress is cosmetic; never let it break an answer
        logger.debug("progress callback failed: %s", exc)


async def _chat(messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
    """One non-streaming Ollama /api/chat call; returns the assistant message."""
    from services.ai_chat_service import _build_ollama_options

    options = _build_ollama_options()
    options["temperature"] = 0.1
    payload: dict[str, Any] = {
        "model": settings.AI_CHAT_MODEL,
        "messages": messages,
        "stream": False,
        "options": options,
    }
    if tools:
        payload["tools"] = tools
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    async with httpx.AsyncClient(timeout=float(settings.AI_CHAT_TIMEOUT_SECONDS)) as client:
        started = time.perf_counter()
        response = await client.post(url, json=payload)
    timing_log.debug("model call %.2fs (tools offered: %s)", time.perf_counter() - started, bool(tools))
    if response.status_code != 200:
        raise RuntimeError(f"Ollama returned HTTP {response.status_code}")
    return response.json().get("message") or {}


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _tool_message(name: str, payload: Any) -> dict[str, Any]:
    return {"role": "tool", "tool_name": name, "content": json.dumps(payload, default=str, ensure_ascii=False)}


async def _run_rounds(
    db: AsyncSession,
    user: User,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    max_rounds: int,
    action_names: set[str],
    tool_executor: Optional[ToolExecutor],
    tool_results: list[dict[str, Any]],
    action_results: list[dict[str, Any]],
    context: Optional[AnswerContext],
    thinking_out: list[str],
    tool_round_only: bool = False,
) -> str:
    """Call the model, run its tool calls, repeat; returns the final text (may be empty).

    With ``tool_round_only`` the loop ends after the first round that made tool calls, with no text."""
    for round_no in range(1, max_rounds + 1):
        message = await _chat(messages, tools)
        if message.get("thinking"):
            thinking_out.append(str(message["thinking"]))
        calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()
        if not calls:
            return content

        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            args = _parse_args(fn.get("arguments"))
            await _progress(context, {"event": "tool_call", "name": name, "args": args, "round": round_no})
            if name in market_data_tools.READ_TOOL_NAMES:
                result = await market_data_tools.run_tool(db, user, name, args)
                tool_results.append(result)
                messages.append(_tool_message(name, result))
            elif name in action_names and tool_executor is not None:
                try:
                    outcome = await tool_executor(name, args)
                except Exception as exc:
                    logger.warning("action tool %s failed: %s", name, exc)
                    outcome = {"ok": False, "error": f"{name} failed"}
                action_results.append({"tool": name, "args": args, "result": outcome})
                messages.append(_tool_message(name, outcome))
            else:
                logger.warning("Skipping tool call '%s' that was not offered", name)
                messages.append(_tool_message(name, {"ok": False, "error": f"tool '{name}' is not available"}))
        if tool_round_only:
            return ""

    # Rounds exhausted: ask for the answer without offering tools again.
    message = await _chat(messages, None)
    if message.get("thinking"):
        thinking_out.append(str(message["thinking"]))
    return (message.get("content") or "").strip()


def _verification_inputs(tool_results: list[dict[str, Any]], action_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Action outcomes (e.g. the draft just saved) are legitimate sources too, but are not read tools.
    return list(tool_results) + [{"tool": a["tool"], "args": a["args"], "result": a["result"]} for a in action_results]


def _diagnostics(check: Verification, retried: bool, used_fallback: bool, llm_error: bool) -> dict[str, Any]:
    return {
        "ok": bool(check.ok) and not used_fallback,
        "retried": retried,
        "used_fallback": used_fallback,
        "unverified": [str(getattr(c, "text", c)) for c in check.unverified],
        "banned": list(check.banned),
        "llm_error": llm_error,
    }


def _retry_message(check: Verification) -> str:
    lines = ["Your answer cannot be shown yet. Problems:"]
    lines += [f"- {p}" for p in check.problems()]
    lines += [
        "Rewrite it. Remove every number that is not in a tool result or in the user's message, or fetch it with a "
        "read tool. Remove example numbers: when asking for missing details, never give example quantities or prices. Do not use outside knowledge, typical rates or assumed pack sizes. If there is no data, say so "
        "plainly and suggest a next step.",
    ]
    return "\n".join(lines)


async def answer(
    db: AsyncSession,
    user: User,
    *,
    history_messages: list[dict[str, Any]],
    system_prompt: str,
    user_message: str,
    extra_tools: Optional[list[dict[str, Any]]] = None,
    tool_executor: Optional[ToolExecutor] = None,
    context: Optional[AnswerContext] = None,
) -> AnswerResult:
    """Produce a verified answer. Never raises."""
    tool_results: list[dict[str, Any]] = []
    action_results: list[dict[str, Any]] = []
    thinking: list[str] = []
    retried = False
    check = Verification(ok=True)

    prompt = system_prompt
    if context and context.rfq_id:
        prompt += (
            f"\n\nThe user is asking about listing {context.rfq_id}. Call get_listing first, and pass "
            f"exclude_rfq_id={context.rfq_id} to search_listings and market_price_stats so the listing is not "
            "compared with itself."
        )
    messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}, *history_messages]
    extra = list(extra_tools or [])
    action_names = {((t or {}).get("function") or {}).get("name") for t in extra} - {None}
    tools = [*market_data_tools.READ_TOOL_SCHEMAS, *extra]
    # The user's earlier turns are theirs too: "the 50,000 pcs I mentioned" is not an invention.
    earlier_user_text = "\n".join(
        str(m.get("content") or "") for m in history_messages if isinstance(m, dict) and m.get("role") == "user"
    )
    def _extra_text() -> str:
        extra_ctx = context.extra_allowed_text if context else ""
        extra_ctx = extra_ctx() if callable(extra_ctx) else extra_ctx
        return "\n".join(t for t in (extra_ctx, earlier_user_text) if t)

    def _current_draft() -> Optional[dict[str, Any]]:
        d = context.draft if context else None
        return d() if callable(d) else d

    def _verification_draft() -> Optional[dict[str, Any]]:
        d = context.verify_draft if context and context.verify_draft is not None else (context.draft if context else None)
        return d() if callable(d) else d

    def _fallback(text_when_actions: str = "", llm_error: bool = False) -> AnswerResult:
        # A turn that only performed actions has no data answer to replace; the caller words those.
        drafting = bool(context and context.drafting) or any(
            a.get("tool") in DRAFT_ACTION_TOOLS for a in action_results
        )
        if drafting:
            draft_now = _current_draft() or {}
            closing = context.drafting_closing(draft_now) if context and context.drafting_closing else None
            text = drafting_reply.build_drafting_reply(
                draft_now, rfq_tool_service.evaluate_rfq_readiness(draft_now), user_message, closing=closing
            )
        elif action_results and not tool_results:
            text = text_when_actions
        else:
            text = answer_verifier.render_facts_answer(tool_results, user_message)
        logger.warning(
            "Answer engine used fallback (drafting=%s, retried=%s, llm_error=%s); unverified=%s banned=%s",
            drafting, retried, llm_error, [str(c) for c in check.unverified], check.banned,
        )
        return AnswerResult(
            text=text, tool_results=tool_results, verification=check, used_fallback=True,
            retried=retried, action_results=action_results, thinking="".join(thinking) or None,
            llm_error=llm_error, diagnostics=_diagnostics(check, retried, True, llm_error),
        )

    try:
        text = await _run_rounds(
            db, user, messages, tools, max_rounds=MAX_TOOL_ROUNDS, action_names=action_names,
            tool_executor=tool_executor, tool_results=tool_results, action_results=action_results,
            context=context, thinking_out=thinking, tool_round_only=bool(context and context.tool_round_only),
        )
        if not text:
            if tool_results and not (context and context.tool_round_only):
                return _fallback()
            return AnswerResult(
                text="", tool_results=tool_results, verification=check,
                action_results=action_results, thinking="".join(thinking) or None,
                diagnostics=_diagnostics(check, retried, False, False),
            )

        await _progress(context, {"event": "verifying"})
        if context and context.before_verify:
            await context.before_verify()
        check = answer_verifier.verify(
            text, _verification_inputs(tool_results, action_results), user_message, _verification_draft(), _extra_text()
        )
        if not check.ok:
            logger.info("Answer failed verification (%s); retrying once", check.problems())
            retried = True
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": _retry_message(check)})
            text = await _run_rounds(
                db, user, messages, tools, max_rounds=MAX_TOOL_ROUNDS - 1, action_names=action_names,
                tool_executor=tool_executor, tool_results=tool_results, action_results=action_results,
                context=context, thinking_out=thinking,
            )
            check = answer_verifier.verify(
                text, _verification_inputs(tool_results, action_results), user_message, _verification_draft(), _extra_text()
            )
            if not check.ok or not text:
                logger.warning("Answer failed verification twice; using facts-only answer")
                return _fallback()
        return AnswerResult(
            text=text, tool_results=tool_results, verification=check, used_fallback=False,
            retried=retried, action_results=action_results, thinking="".join(thinking) or None,
            diagnostics=_diagnostics(check, retried, False, False),
        )
    except Exception as exc:
        logger.error("Answer engine failed: %s", exc)
        return _fallback(llm_error=True)

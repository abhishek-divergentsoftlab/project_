"""One way to ask the local model for structured JSON.

Every short AI call (message understanding, the AI lexicon, HSN classification,
attribute suggestions) goes through ``generate_json`` so they all hit Ollama with
the same model, context size and keep-alive as the chat.

That sameness is not style. Ollama keeps one runner per (model, num_ctx): a call
with a different ``num_ctx`` -- including no ``num_ctx`` at all, which means the
server default -- unloads the resident model and loads it again, 40-50 seconds
for gpt-oss:120b, while every other caller waits. On the dev box the same
model is shared with another project, so a mismatched call evicts it for them
too. ``shared_options`` is the single place that decides those values.
"""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, AsyncIterator, Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# After a failure skip the model briefly, so an unreachable or saturated Ollama
# costs one timeout rather than one per message. Per model.
_BACKOFF_SECONDS = 20.0
_down_until: dict[str, float] = {}

# The GPU is shared (one generation model at a time, other projects use it too), so a burst of chat turns, lexicon
# lookups and translations must not all be queued on Ollama at once: past two in flight every extra request only
# makes all of them slower. Callers wait for a slot up to QUEUE_WAIT_SECONDS, then get LLMBusy rather than a
# multi-minute hang.
MAX_CONCURRENT_GENERATIONS = 2
QUEUE_WAIT_SECONDS = 20.0


class LLMError(Exception):
    """Base of the typed model errors; ``kind`` is what a caller shows the user."""

    kind = "unavailable"


class LLMTimeout(LLMError):
    kind = "timeout"


class LLMBusy(LLMError):
    kind = "busy"


class LLMUnavailable(LLMError):
    kind = "unavailable"


_semaphore: Optional[asyncio.Semaphore] = None
_semaphore_loop: Optional[asyncio.AbstractEventLoop] = None


def _generation_semaphore() -> asyncio.Semaphore:
    """One semaphore per event loop (a semaphore must not be shared across loops, e.g. between tests)."""
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore, _semaphore_loop = asyncio.Semaphore(MAX_CONCURRENT_GENERATIONS), loop
    return _semaphore


@contextlib.asynccontextmanager
async def generation_slot(wait: Optional[float] = None) -> AsyncIterator[None]:
    """Hold one of the process-wide generation slots; raises ``LLMBusy`` if none frees up within ``wait`` seconds."""
    sem = _generation_semaphore()
    limit = QUEUE_WAIT_SECONDS if wait is None else max(0.0, wait)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=limit)
    except asyncio.TimeoutError:
        raise LLMBusy("the assistant is busy") from None
    try:
        yield
    finally:
        sem.release()


def mark_down(model: Optional[str] = None, scope: str = "") -> None:
    """Put the model in back-off after a timeout or connection error."""
    _down_until[_slot(model, scope)] = time.monotonic() + _BACKOFF_SECONDS


def chat_model() -> str:
    return settings.AI_CHAT_MODEL


def shared_options(temperature: float = 0.0) -> dict[str, Any]:
    """Ollama options for any call to the chat model: same ``num_ctx`` as the chat."""
    options: dict[str, Any] = {"temperature": temperature}
    num_ctx = getattr(settings, "AI_CHAT_NUM_CTX", 0)
    if num_ctx and num_ctx > 0:
        options["num_ctx"] = num_ctx
    return options


def keep_alive() -> str:
    """Ollama applies the keep-alive of the latest request, so a short one here
    would unload a model other callers still use."""
    return getattr(settings, "OLLAMA_KEEP_ALIVE", "24h")


def _slot(model: Optional[str], scope: str) -> str:
    return (model or chat_model()) + (f"#{scope}" if scope else "")


def available(model: Optional[str] = None, scope: str = "") -> bool:
    """False while a recent failure keeps the model in back-off.

    ``scope`` keeps independent back-offs: a short helper call (understanding, lexicon) that timed out while the
    model was still loading must not make the chat answer refuse to try; the chat uses scope "chat"."""
    return time.monotonic() >= _down_until.get(_slot(model, scope), 0.0)


def reset_backoff() -> None:
    _down_until.clear()


async def generate_json(
    prompt: str,
    schema: Optional[dict[str, Any]] = None,
    *,
    timeout: float,
    model: Optional[str] = None,
    temperature: float = 0.0,
) -> Optional[Any]:
    """The decoded JSON the model returns for ``prompt``, or None. Never raises.

    ``schema`` constrains the output (Ollama structured outputs); without it the
    model is only asked for JSON. Timeouts and connection errors put the model
    in a short back-off; a reply that is not JSON does not.
    """
    name = model or chat_model()
    if not available(name):
        return None

    # 1. Try vLLM if the model explicitly targets the vLLM model, or if vLLM is the configured provider
    vllm_configured = getattr(settings, "LLM_PROVIDER", "ollama") == "vllm"
    is_vllm_model = (model == getattr(settings, "VLLM_MODEL", "Qwen/Qwen3-8B"))
    target_is_vllm = is_vllm_model or (vllm_configured and (model is None or model != settings.AI_CHAT_MODEL))
    if target_is_vllm:
        vllm_url = f"{settings.VLLM_BASE_URL.rstrip('/')}/chat/completions"
        vllm_model = model or getattr(settings, "VLLM_MODEL", "Qwen/Qwen3-8B")
        vllm_payload: dict[str, Any] = {
            "model": vllm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": False,
        }
        if schema:
            vllm_payload["response_format"] = {
                "type": "json_object",
            }
        else:
            vllm_payload["response_format"] = {"type": "json_object"}

        try:
            async with generation_slot():
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(vllm_url, json=vllm_payload)
            if resp.status_code == 200:
                raw_text = (resp.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
                if raw_text.startswith("```json"):
                    raw_text = raw_text[len("```json"):].strip()
                if raw_text.startswith("```"):
                    raw_text = raw_text[len("```"):].strip()
                if raw_text.endswith("```"):
                    raw_text = raw_text[:-3].strip()
                try:
                    return json.loads(raw_text or "null")
                except (ValueError, AttributeError) as exc:
                    logger.debug("vLLM returned non-JSON (%s): %s", exc, raw_text[:100])
                    return None
            else:
                logger.info("vLLM %s returned HTTP %s; falling back to Ollama", vllm_model, resp.status_code)
        except LLMBusy:
            logger.info("vLLM %s busy: no generation slot within %.0fs", vllm_model, QUEUE_WAIT_SECONDS)
            return None
        except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
            logger.info("vLLM unavailable (%s); attempting Ollama fallback", type(exc).__name__)

    # 2. Ollama provider / fallback
    payload: dict[str, Any] = {
        "model": name,
        "prompt": prompt,
        "stream": False,
        "format": schema or "json",
        "keep_alive": keep_alive(),
        "options": shared_options(temperature) if name == chat_model() else {"temperature": temperature},
    }
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate"
    try:
        async with generation_slot():
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
    except LLMBusy:
        logger.info("model %s busy: no generation slot within %.0fs", name, QUEUE_WAIT_SECONDS)
        return None
    except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
        logger.info("model %s unavailable (%s); backing off %.0fs", name, type(exc).__name__, _BACKOFF_SECONDS)
        _down_until[name] = time.monotonic() + _BACKOFF_SECONDS
        return None
    if resp.status_code != 200:
        logger.info("model %s returned HTTP %s", name, resp.status_code)
        return None
    try:
        return json.loads(resp.json().get("response") or "null")
    except (ValueError, AttributeError) as exc:
        logger.debug("model %s returned non-JSON (%s)", name, exc)
        return None


# ---------------------------------------------------------------------------
# Tool-calling chat (the chat agent): Ollama /api/chat with ``tools``
# ---------------------------------------------------------------------------
CHAT_SCOPE = "chat"  # back-off scope shared with the answer engine, so one failed call spares the next


def _chat_payload(messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]], stream: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": chat_model(),
        "messages": messages,
        "stream": stream,
        "options": shared_options(0.1),  # num_ctx included: a different one would reload the model
        "keep_alive": keep_alive(),
    }
    if tools:
        payload["tools"] = tools
    return payload


_UNICODE_FIXES = {
    **{ord(c): " " for c in "\u00a0\u2007\u202f\u2009\u200a"},
    **{ord(c): "-" for c in "\u2010\u2011\u2012"},
    **{ord(c): None for c in "\u200b\ufeff"},
}


def normalize_text(value: Any) -> Any:
    """Model-output hygiene: exotic spaces/hyphens/zero-width chars become plain ones (curly quotes, em dash stay)."""
    return value.translate(_UNICODE_FIXES) if isinstance(value, str) else value


def _normalize_args(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, dict):
        return {k: _normalize_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_args(v) for v in value]
    return value


def _tool_calls_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``[{"name", "arguments"}]`` of a message; arguments are always a dict."""
    out: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        fn = (call or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if fn.get("name"):
            out.append({"name": str(fn["name"]), "arguments": _normalize_args(args) if isinstance(args, dict) else {}})
    return out


async def _chat_once(
    messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]], timeout: float
) -> dict[str, Any]:
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    try:
        async with generation_slot():
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=_chat_payload(messages, tools, False))
    except httpx.TimeoutException as exc:
        mark_down(scope=CHAT_SCOPE)
        raise LLMTimeout(f"chat call timed out ({type(exc).__name__})") from exc
    except (httpx.TransportError, OSError) as exc:
        mark_down(scope=CHAT_SCOPE)
        raise LLMUnavailable(f"model unreachable ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise LLMUnavailable(f"Ollama returned HTTP {resp.status_code}")
    message = resp.json().get("message") or {}
    return {
        "content": normalize_text(message.get("content") or ""),
        "thinking": message.get("thinking") or "",
        "tool_calls": _tool_calls_of(message),
    }


async def _chat_stream(
    messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]], timeout: float
) -> AsyncIterator[dict[str, Any]]:
    """Yield ``{"content", "thinking"}`` deltas as they arrive, then one ``{"done": True, "tool_calls": [...]}``."""
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    deadline = time.monotonic() + timeout
    calls: list[dict[str, Any]] = []
    try:
        async with generation_slot():
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                async with client.stream("POST", url, json=_chat_payload(messages, tools, True)) as resp:
                    if resp.status_code != 200:
                        raise LLMUnavailable(f"Ollama returned HTTP {resp.status_code}")
                    async for line in resp.aiter_lines():
                        if time.monotonic() > deadline:
                            raise LLMTimeout("chat stream passed its deadline")
                        if not line.strip():
                            continue
                        try:
                            part = json.loads(line)
                        except ValueError:
                            continue
                        message = part.get("message") or {}
                        calls.extend(_tool_calls_of(message))
                        if message.get("content") or message.get("thinking"):
                            yield {"content": normalize_text(message.get("content") or ""), "thinking": message.get("thinking") or ""}
                        if part.get("done"):
                            break
    except httpx.TimeoutException as exc:
        mark_down(scope=CHAT_SCOPE)
        raise LLMTimeout(f"chat call timed out ({type(exc).__name__})") from exc
    except (httpx.TransportError, OSError) as exc:
        mark_down(scope=CHAT_SCOPE)
        raise LLMUnavailable(f"model unreachable ({type(exc).__name__})") from exc
    yield {"done": True, "tool_calls": calls}


def chat(
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    *,
    timeout: float,
    stream: bool = False,
) -> Any:
    """One tool-calling chat call to the chat model (Ollama ``/api/chat``). Raises ``LLMError`` subclasses.

    ``stream=False``: awaitable returning ``{"content", "thinking", "tool_calls": [{"name", "arguments"}]}``.
    ``stream=True``: async iterator of ``{"content", "thinking"}`` deltas ending with ``{"done": True, "tool_calls"}``.
    Both honour the shared generation slots and the chat back-off scope.
    """
    if not available(scope=CHAT_SCOPE):
        raise LLMUnavailable("the model is in back-off after a recent failure")
    return _chat_stream(messages, tools, timeout) if stream else _chat_once(messages, tools, timeout)

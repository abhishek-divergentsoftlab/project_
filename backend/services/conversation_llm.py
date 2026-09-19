"""Model-driven conversation: phrasing the questions and reading the replies.

Two jobs, both optional and both time-boxed:

* ``next_question`` writes the next question in natural language instead of
  reciting a fixed string.
* ``interpret`` decides what a reply meant -- an answer, a refusal, "just show
  me", or a change of product -- instead of matching against a keyword list.

Both return None on timeout or malformed output, and the caller falls back to
the deterministic path. That matters on this machine: generation measured
40-60 seconds even with the model resident and the GPU idle, which is unusable
in a chat, while embeddings are comfortably fast. The timeout is deliberately
short so a slow model costs a moment, not the turn.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from core.config import settings
from services.query_extractor import Requirements

logger = logging.getLogger(__name__)

INTENTS = ("answer", "skip", "show_results", "change_product")

_INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": list(INTENTS)}},
    "required": ["intent"],
}

_FIELD_HINTS = {
    "quantity": "how many units they need, and in what unit",
    "price": "their target price per unit",
    "location": "which city it should be delivered to",
    "deadline": "by when they need it",
}


def _generate(prompt: str, schema: Optional[dict] = None) -> Optional[str]:
    body: dict[str, Any] = {
        "model": settings.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 60},
    }
    if schema:
        body["format"] = schema

    request = urllib.request.Request(
        f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=settings.LLM_TIMEOUT_SECONDS
        ) as response:
            return (json.loads(response.read()).get("response") or "").strip()
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.info("conversation model unavailable, using the static path: %s", exc)
        return None


def _describe_state(requirements: Requirements) -> str:
    bits: list[str] = []
    if requirements.product:
        bits.append(f"product: {requirements.product}")
    for key, value in requirements.attributes.items():
        bits.append(f"{key}: {value}")
    if requirements.quantity_value is not None:
        bits.append(f"quantity: {requirements.quantity_value} {requirements.quantity_unit or ''}".strip())
    if requirements.price_amount is not None:
        bits.append(f"target price: {requirements.price_amount} {requirements.price_currency or ''}".strip())
    if requirements.city:
        bits.append(f"city: {requirements.city}")
    if requirements.deadline_days is not None:
        bits.append(f"needed within: {requirements.deadline_days} days")
    return "; ".join(bits) or "nothing yet"


async def next_question(requirements: Requirements, field: str) -> Optional[str]:
    """One natural question about ``field``, or None to use the static path."""
    if not settings.DIRECT_SEARCH_LLM:
        return None

    prompt = (
        "You are helping someone search a B2B marketplace.\n"
        "Ask ONE short, natural question about the missing detail.\n"
        "Do not greet, do not explain, do not list options. "
        "One sentence, under 15 words.\n\n"
        f"Known so far: {_describe_state(requirements)}\n"
        f"Missing detail: {_FIELD_HINTS.get(field, field)}\n\n"
        "Question:"
    )
    text = await asyncio.to_thread(_generate, prompt)
    if not text:
        return None

    # Keep the first line only; small models like to add commentary.
    question = text.splitlines()[0].strip().strip('"')
    if not question or len(question) > 160 or "?" not in question:
        return None
    return question


async def interpret(message: str, pending_field: Optional[str]) -> Optional[str]:
    """What the reply meant, or None to fall back to keyword matching."""
    if not settings.DIRECT_SEARCH_LLM or not pending_field:
        return None

    prompt = (
        "Classify the user's reply in a product search conversation.\n"
        f'The assistant just asked about: {_FIELD_HINTS.get(pending_field, pending_field)}\n'
        f"Reply: {message!r}\n\n"
        "answer = they gave the detail\n"
        "skip = they do not care about this one detail\n"
        "show_results = they want the results now, no more questions\n"
        "change_product = they are asking about a different product\n"
    )
    raw = await asyncio.to_thread(_generate, prompt, _INTENT_SCHEMA)
    if not raw:
        return None
    try:
        intent = json.loads(raw).get("intent")
    except (ValueError, AttributeError):
        return None
    return intent if intent in INTENTS else None

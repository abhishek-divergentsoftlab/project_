"""Optional model assist for the fields a parser cannot reach.

Opt in with ``DIRECT_SEARCH_LLM=true``. It is off by default because local
models were measurably worse than the parser at the mechanical fields --
see the table in the README -- while adding seconds of latency.

Its one job is to **fill gaps**. It never overrides a value the parser found,
because the parser is the more reliable of the two on prices, quantities,
units, dates and city names. If the model is slow, unreachable or returns
nonsense, the parsed requirements are returned unchanged and the chat carries
on; a search box must not fail because a side-car is down.
"""

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Optional

from core.config import settings
from services.query_extractor import Requirements

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "product": {"type": ["string", "null"]},
        "category": {
            "type": ["string", "null"],
            "enum": [
                "Electronics", "Packaging", "Furniture", "Agriculture",
                "Textiles", None,
            ],
        },
        "attributes": {"type": "object"},
    },
}

_PROMPT = """You extract the product being searched for in a B2B marketplace.

Return only what the message actually says. Use null when it is not stated.
- product: the product name alone, with no quantity, price, city or date
- category: one of Electronics, Packaging, Furniture, Agriculture, Textiles
- attributes: qualities such as color, material, ply, type, grade

Do not invent anything.

Message: {message}"""


def _call_ollama(message: str) -> Optional[dict[str, Any]]:
    body = json.dumps(
        {
            "model": settings.OLLAMA_MODEL,
            "prompt": _PROMPT.format(message=message),
            "format": _SCHEMA,
            "stream": False,
            "options": {"temperature": 0},
        }
    ).encode()

    request = urllib.request.Request(
        f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=settings.OLLAMA_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read())
        return json.loads(payload.get("response") or "{}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _clean(value: Any) -> Optional[str]:
    """Models emit the string "null" surprisingly often."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return text


async def enrich(message: str, parsed: Requirements) -> Requirements:
    """Fill only the gaps the parser left. Never overrides, never raises."""
    if not settings.DIRECT_SEARCH_LLM:
        return parsed

    # Nothing to gain: the parser already identified the product.
    if parsed.product and parsed.category:
        return parsed

    raw = await asyncio.to_thread(_call_ollama, message)
    if not raw:
        return parsed

    enriched = parsed.model_copy(deep=True)

    if not enriched.product:
        enriched.product = _clean(raw.get("product"))
    if not enriched.category:
        enriched.category = _clean(raw.get("category"))

    attributes = raw.get("attributes")
    if isinstance(attributes, dict):
        for key, value in attributes.items():
            cleaned = _clean(value) if isinstance(value, str) else value
            # The parser's attributes win; only genuinely new keys are added.
            if cleaned not in (None, "") and key not in enriched.attributes:
                enriched.attributes[key] = cleaned

    return enriched

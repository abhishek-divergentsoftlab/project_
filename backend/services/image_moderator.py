"""Image content moderation service using local Ollama vision models.

Analyzes live webcam uploads for vulgarity, violence, and sexual content
before allowing transmission into chat sessions.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

MODERATION_PROMPT = """You are an AI content safety moderator for a B2B marketplace chat application.
Analyze the attached image strictly for the following disallowed categories:
1. Vulgarity or profanity: offensive, obscene text, gestures, slurs, or vulgar symbols.
2. Violence: weapons (guns, knives held threateningly), blood, gore, physical fights, violence, or threats of harm.
3. Sexual content: nudity, sexually explicit material, exposed genitalia, breasts, buttocks, pornography, or sexually suggestive acts.

Evaluate the image carefully. Normal workplace photos, warehouse products, machinery, documents, industrial goods, and authentic inventory snapshots are SAFE.

Respond ONLY with a JSON object in this format:
{
  "flagged": true,
  "category": "vulgarity" or "violence" or "sexual" or null,
  "reason": "short explanation if flagged, or empty string if safe"
}
"""


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    """Extract and parse JSON object from text, handling markdown fences and thoughts."""
    if not text:
        return None

    # Remove markdown code fences if present
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)

    # Try direct parse
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Extract outermost { ... }
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return None


_TRUE_STRINGS = {"true", "yes", "1", "flagged", "unsafe"}
_FALSE_STRINGS = {"false", "no", "0", "none", "null", "safe", ""}


def _parse_flagged(value: Any) -> Optional[bool]:
    """Strict reading of the model's ``flagged`` field.

    ``bool("false")`` is True, so strings are matched explicitly. Returns None
    for anything that is not a recognisable yes/no, which the caller treats as
    "no verdict". A missing field (None) also counts as no verdict.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    return None


async def inspect_live_image(
    image_bytes: bytes,
    content_type: str = "image/jpeg",
) -> tuple[bool, Optional[str], Optional[dict[str, Any]]]:
    """Inspect a live capture image using Ollama qwen3-vl:8b.

    Returns:
        tuple[is_safe, warning_message, raw_details]
        - is_safe: True if image is safe to upload and send, False if flagged or blocked
        - warning_message: Human-readable warning message if not safe, None if safe
        - raw_details: Dict of moderation verdict or None
    """
    if not settings.IMAGE_MODERATION_ENABLED:
        return True, None, {"skipped": True}

    if not image_bytes:
        return False, "⚠️ Invalid image: empty file received.", None

    # Convert to base64 for Ollama API
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "model": settings.IMAGE_MODERATION_MODEL,
        "prompt": MODERATION_PROMPT,
        "images": [img_b64],
        "stream": False,
        "format": "json",
        "options": {
            "num_ctx": 4096,
            "temperature": 0.0,
        },
    }

    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate"

    try:
        async with httpx.AsyncClient(timeout=float(settings.IMAGE_MODERATION_TIMEOUT_SECONDS)) as client:
            response = await client.post(url, json=payload)

        if response.status_code != 200:
            logger.error("Ollama moderation returned HTTP %d: %s", response.status_code, response.text)
            if settings.IMAGE_MODERATION_FAIL_CLOSED:
                return (
                    False,
                    "⚠️ Image safety verification failed (AI service error). Image upload blocked.",
                    {"status_code": response.status_code, "error": response.text},
                )
            return True, None, {"error": "non-200 from ollama"}

        data = response.json()

        # qwen3-vl:8b thinking models emit output in 'response' or 'thinking'
        response_text = (data.get("response") or "").strip()
        thinking_text = (data.get("thinking") or "").strip()

        parsed = _extract_json(response_text) or _extract_json(thinking_text)

        is_flagged = _parse_flagged(parsed.get("flagged")) if parsed else None

        if is_flagged is None:
            # No usable verdict: not JSON, a refusal ("I'm sorry, I can't..."),
            # or a "flagged" value that is neither true nor false.
            logger.warning("Could not parse a moderation verdict from Ollama: %s / %s", response_text, thinking_text)
            combined = f"{response_text} {thinking_text}".lower()
            if settings.IMAGE_MODERATION_FAIL_CLOSED or any(
                term in combined for term in ('flagged": true', "vulgar", "violence", "sexual")
            ):
                return (
                    False,
                    "⚠️ Image safety verification could not confirm this image is safe. Upload blocked.",
                    {"raw_response": response_text, "raw_thinking": thinking_text, "unparseable": True},
                )
            return True, None, {"raw_response": response_text, "unparseable": True}

        category = parsed.get("category") or "disallowed content"
        reason = parsed.get("reason") or "Image contains content violating marketplace policies."

        if is_flagged:
            warning_msg = (
                f"⚠️ Content Warning: Image flagged by AI safety moderation for {category}. "
                f"Live photo upload rejected. Reason: {reason}"
            )
            logger.info("Image upload flagged: category=%s reason=%s", category, reason)
            return False, warning_msg, parsed

        return True, None, parsed

    except (httpx.RequestError, httpx.TimeoutException, OSError) as exc:
        logger.error("Ollama moderation connection failed: %s", exc)
        if settings.IMAGE_MODERATION_FAIL_CLOSED:
            return (
                False,
                "⚠️ Image safety moderation service is temporarily unavailable. Image upload blocked.",
                {"exception": str(exc)},
            )
        return True, None, {"exception": str(exc)}

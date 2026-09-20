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
from decimal import Decimal
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


class SearchAgentDecision:
    """Structured decision from the LLM Search Agent."""

    def __init__(
        self,
        action: str,  # "reply" or "search_marketplace"
        reply: str,
        updated_requirements: Requirements,
        thought: Optional[str] = None,
    ) -> None:
        self.action = action
        self.reply = reply
        self.updated_requirements = updated_requirements
        self.thought = thought


def _generate(
    prompt: str,
    schema: Optional[dict] = None,
    num_predict: int = 120,
) -> Optional[str]:
    body: dict[str, Any] = {
        "model": settings.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": 0.1, "num_predict": num_predict},
    }
    if schema:
        body["format"] = schema
    else:
        body["format"] = "json"

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
        logger.info("conversation model unavailable, using static fallback: %s", exc)
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

    # Keep the first line or unpack JSON if the model answered with a JSON object
    cleaned = text.strip()
    if cleaned.startswith("{") and "}" in cleaned:
        try:
            data = json.loads(cleaned)
            question = data.get("question") or data.get("reply") or cleaned
        except Exception:
            question = cleaned
    else:
        question = cleaned.splitlines()[0].strip().strip('"')

    question = str(question).strip().strip('"')
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


async def run_search_agent(
    history: list[dict[str, str]],
    current_requirements: Requirements,
    user_message: str,
    *,
    already_searched: bool = False,
) -> Optional[SearchAgentDecision]:
    """Autonomous LLM Agent: understands conversation, updates requirements, and decides to reply or search."""
    if not settings.DIRECT_SEARCH_LLM:
        return None

    # Format history concisely for context window
    history_lines = []
    for m in history[-6:]:
        role = "User" if m.get("role") == "user" else "Assistant"
        content = (m.get("content") or "").strip()
        if content:
            history_lines.append(f"{role}: {content}")
    history_str = "\n".join(history_lines) if history_lines else "(No previous messages)"

    current_state = {
        "product": current_requirements.product,
        "category": current_requirements.category,
        "attributes": current_requirements.attributes,
        "quantity": f"{current_requirements.quantity_value} {current_requirements.quantity_unit or ''}".strip() or None,
        "target_price": f"{current_requirements.price_amount} {current_requirements.price_currency or ''}".strip() or None,
        "city": current_requirements.city,
        "state": current_requirements.state,
        "country": current_requirements.country,
        "deadline_days": current_requirements.deadline_days,
        "already_searched": already_searched,
    }

    prompt = f"""You are an intelligent, human-like B2B marketplace sourcing assistant.
Your goal is to converse with the buyer, clarify product needs, handle category pivots, and execute the marketplace search tool when ready.

CURRENT REQUIREMENTS STATE:
{json.dumps(current_state, indent=2)}

RECENT CONVERSATION HISTORY:
{history_str}

LATEST USER MESSAGE:
"{user_message}"

AGENT GUIDELINES:
1. NATURAL CONVERSATION:
   - Talk conversationally, politely, and concisely (1-2 sentences). Do not use rigid templates or recite robotic questions.
2. PRODUCT & CATEGORY HANDLING:
   - Extract the specific item (e.g. "tomatoes", "hdmi cables", "usb cables").
   - If the user uses a broad generic term (e.g. "vegetables", "electronics", "clothes"), ask them what specific product they need before searching. Do NOT set product to "vegetables".
   - PRODUCT PIVOTS: If the user changes product (e.g. "instead of cables i need tomatoes"):
     * Switch product to the new item ("tomatoes") and update category (e.g. "Agriculture").
     * CLEAR obsolete attributes from the previous product (e.g. remove cable color/type).
     * Ask if they want to specify quantity/price or configurations (like organic, grade, or variety), or confirm if they want to search now.
3. DETAILED CONFIGURATIONS & REFINEMENTS:
   - Extract any attributes mentioned: organic (boolean true/false), grade ("Grade A", "Grade B"), color, size, variety, etc., and put them into `attributes`.
4. TOOL EXECUTION (search_marketplace):
   - You decide when to search!
   - Set action to "search_marketplace" IF:
     * Requirements are established and clear, OR
     * The user explicitly says "search", "find", "show me", "proceed", "yes", "sure", "ok", OR
     * The user is refining an already searched query (e.g. changing city to "delhi" or adding "organic Grade A").
   - Set action to "reply" IF:
     * Product is unknown or too broad, OR
     * The user just changed product and you need to confirm details/clarify before searching.

OUTPUT FORMAT (Valid JSON only):
{{
  "thought": "Brief reasoning about what the user wants and why you chose this action",
  "action": "reply" or "search_marketplace",
  "reply": "Your conversational message or question to the user",
  "updated_requirements": {{
    "product": "specific product name or null",
    "category": "Agriculture | Electronics | Textiles | etc.",
    "attributes": {{"key": "value"}},
    "quantity_value": number or null,
    "quantity_unit": "units | kg | pcs | etc. or null",
    "price_amount": number or null,
    "price_currency": "INR | USD | etc. or null",
    "city": "city name or null",
    "state": "state name or null",
    "country": "country name or null",
    "deadline_days": integer or null
  }}
}}"""

    raw = await asyncio.to_thread(_generate, prompt, schema=None, num_predict=400)
    if not raw:
        return None

    try:
        text = raw.strip()
        if text.startswith("```"):
            # strip markdown code blocks
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        data = json.loads(text)
        action = data.get("action")
        if action not in ("reply", "search_marketplace"):
            return None

        reply = (data.get("reply") or "").strip()
        thought = data.get("thought")
        req_data = data.get("updated_requirements") or {}

        # Merge requirements
        new_req = current_requirements.model_copy(deep=True)
        new_product = req_data.get("product")
        if new_product and isinstance(new_product, str) and new_product.strip():
            clean_product = new_product.strip().lower()
            if current_requirements.product and clean_product != current_requirements.product.lower():
                # Product pivot: replace attributes
                new_req.product = clean_product
                new_req.attributes = dict(req_data.get("attributes") or {})
            else:
                new_req.product = clean_product
                if isinstance(req_data.get("attributes"), dict):
                    new_req.attributes.update(req_data["attributes"])

        if isinstance(req_data.get("attributes"), dict) and not new_req.attributes:
            new_req.attributes = dict(req_data["attributes"])

        if req_data.get("category"):
            new_req.category = str(req_data["category"]).strip()

        if req_data.get("quantity_value") is not None:
            try:
                new_req.quantity_value = Decimal(str(req_data["quantity_value"]))
            except Exception:
                pass
        if req_data.get("quantity_unit"):
            new_req.quantity_unit = str(req_data["quantity_unit"]).strip()

        if req_data.get("price_amount") is not None:
            try:
                new_req.price_amount = Decimal(str(req_data["price_amount"]))
            except Exception:
                pass
        if req_data.get("price_currency"):
            new_req.price_currency = str(req_data["price_currency"]).strip().upper()

        if req_data.get("city"):
            new_req.city = str(req_data["city"]).strip().title()
        if req_data.get("state"):
            new_req.state = str(req_data["state"]).strip().title()
        if req_data.get("country"):
            new_req.country = str(req_data["country"]).strip().title()

        if req_data.get("deadline_days") is not None:
            try:
                new_req.deadline_days = int(req_data["deadline_days"])
            except (ValueError, TypeError):
                pass

        if action == "reply" and not reply:
            reply = "Could you tell me more about your requirements?"

        return SearchAgentDecision(
            action=action,
            reply=reply,
            updated_requirements=new_req,
            thought=thought,
        )
    except Exception as exc:
        logger.warning("failed to parse search agent response: %s (raw=%r)", exc, raw)
        return None


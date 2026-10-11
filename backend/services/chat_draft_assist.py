"""The web form's three AI helpers (HSN suggestion, spec suggestions, live moderation) for the AI chat.

The RFQ form (RFQNew.tsx) shows an HSN suggestion, suggested spec chips and a live
moderation warning while the user types. In chat the same helpers run on the draft:
suggestions are kept in ``conversation.state["draft_suggestions"]`` and offered by the
assistant, never written into the draft until the user accepts; moderation is cached in
``state["draft_moderation"]`` and blocks creation when unsafe, as the form blocks submit.

Every helper is best effort. Each runs with its own timeout, concurrently, and a failure
or timeout is silently skipped (the form fails silently too).
"""

import asyncio
import hashlib
import logging
import re
from typing import Any, Optional

from services import hsn_service
from services.categories import normalize_category

logger = logging.getLogger(__name__)

HSN_TIMEOUT = 3.0  # same budget as rfq_service.HSN_AI_TIMEOUT
SPECS_TIMEOUT = 5.0
MODERATION_TIMEOUT = 4.0
MAX_SPEC_PROMPTS = 5
MUST_MATCH_SUFFIX = "__must_match"
_NOT_SPECS = {"name", "hsn_code", "hsn_source"}


# ---------------------------------------------------------------------------
# Draft readers
# ---------------------------------------------------------------------------
def product_name(draft: Optional[dict[str, Any]]) -> str:
    return str(((draft or {}).get("product_details") or {}).get("name") or "").strip()


def user_hsn(draft: Optional[dict[str, Any]]) -> Optional[str]:
    return hsn_service.stored_hsn((draft or {}).get("product_details"))


def moderation_text(draft: Optional[dict[str, Any]]) -> str:
    """The text POST /moderation/check receives from the form: title, description, category."""
    name = product_name(draft)
    if not name:
        return ""
    d = draft or {}
    return f"{name}\n{d.get('description') or ''}\n{d.get('category') or ''}"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _norm_key(key: Any) -> str:
    return re.sub(r"[\s_\-]+", " ", str(key)).strip().lower()


def spec_keys(details: Optional[dict[str, Any]]) -> list[str]:
    return [
        k for k in (details or {})
        if str(k) not in _NOT_SPECS and not str(k).endswith(MUST_MATCH_SUFFIX)
    ]


# ---------------------------------------------------------------------------
# HSN
# ---------------------------------------------------------------------------
_HSN_CODE_RE = re.compile(
    r"\b(?:hsn|sac|tariff)(?:\s+(?:code|no\.?|number))?\s*(?:is|:|=|-|#)?\s*(\d{2,8})\b", re.IGNORECASE
)
_ACCEPT_HSN_RE = re.compile(
    r"\b(?:(?:use|apply|take|add|set|keep|go\s+with|accept|choose|select|put)\b[^.?!]{0,40}\b(?:hsn|suggest\w*|that\s+code|this\s+code)"
    r"|(?:hsn|suggest\w*)\b[^.?!]{0,30}\b(?:yes|ok|okay|theek|thik|sahi|correct|fine|use|apply)"
    r"|(?:yes|haan|han|ha|ji|ok|okay)\b[^.?!]{0,30}\b(?:hsn|suggest\w*|wahi|wohi|same)"
    r"|(?:wahi|wohi|vahi|yahi)\s+hsn|hsn\s+(?:laga|use\s+kar|daal|dal|rakh))",
    re.IGNORECASE,
)
_NEGATED_RE = re.compile(r"\b(?:don'?t|do\s+not|no|not|nahi|nahin|mat)\b", re.IGNORECASE)
_PROPOSES_HSN_RE = re.compile(r"\bhsn\b", re.IGNORECASE)


def explicit_hsn_code(user_text: str) -> Optional[str]:
    """The tariff code a user typed ("HSN 100630", "hsn code 1006"), digits only."""
    m = _HSN_CODE_RE.search(user_text or "")
    return m.group(1) if m else None


def accepts_hsn(user_text: str, previous_assistant: str, suggestion: Optional[dict[str, Any]]) -> bool:
    """True when the user agrees to the pending HSN suggestion.

    A bare "yes" counts only when the assistant's previous turn offered an HSN (the
    chat_permissions convention for bare affirmatives); an explicit phrase counts alone.
    """
    if not suggestion or not suggestion.get("code"):
        return False
    from services import chat_permissions

    text = (user_text or "").strip()
    if chat_permissions.is_bare_affirmative(text):
        return bool(_PROPOSES_HSN_RE.search(previous_assistant or ""))
    if _NEGATED_RE.search(text) and not re.search(r"\b(?:nahi|no)\s+(?:problem|issue)\b", text, re.IGNORECASE):
        return False
    return bool(_ACCEPT_HSN_RE.search(text))


def lookup_hsn(raw: Any) -> tuple[str, Optional[dict[str, Any]]]:
    """Look a code up like GET /hsn/code/{code}. Returns (status, item): ok | unknown | invalid | unavailable."""
    code = hsn_service.normalize_hsn(raw)
    if not code:
        return "invalid", None
    try:
        from api.v1.hsn import lookup_hsn_item

        item = lookup_hsn_item(code)
    except Exception as exc:  # no database, etc.: do not claim the code is wrong
        logger.debug("HSN lookup unavailable: %s", exc)
        return "unavailable", None
    if item is None:
        return "unknown", None
    return "ok", {
        "code": item.code,
        "description": item.description,
        "formatted_title": item.formatted_title,
        "category": item.category,
    }


def apply_hsn(draft: dict[str, Any], code: str, category: Optional[str]) -> None:
    """What the form's Apply does: store the code as a manual pick and take its category."""
    details = dict(draft.get("product_details") or {})
    details["hsn_code"] = code
    details["hsn_source"] = "manual"
    draft["product_details"] = details
    if category and str(category).strip():
        normalized = normalize_category(category)
        if normalized != "General Goods" or not draft.get("category"):
            draft["category"] = normalized


def apply_explicit_hsn(draft: dict[str, Any], raw: Any) -> tuple[str, Optional[dict[str, Any]]]:
    """Validate a typed code and apply it when known. Returns the lookup (status, item)."""
    status, item = lookup_hsn(raw)
    if status == "ok" and item:
        apply_hsn(draft, item["code"], item.get("category"))
    return status, item


def unrecognised_hsn_note(raw: Any) -> str:
    return f"The HSN code {str(raw).strip()} wasn't recognised, so I haven't set it."


# ---------------------------------------------------------------------------
# Spec values and must-match
# ---------------------------------------------------------------------------
def apply_spec_suggestions(draft: dict[str, Any], specs: Optional[list[dict[str, Any]]]) -> None:
    """File a value the user gave for a suggested spec under the suggestion's label (as the form's chip does).

    A model-chosen key that names a suggestion (by key or label) is renamed to the label, and
    a suggestion that comes with must_match preset sets ``<label>__must_match``.
    """
    details = draft.get("product_details")
    if not isinstance(details, dict) or not specs:
        return
    for spec in specs:
        label = str(spec.get("label") or spec.get("key") or "").strip()
        if not label:
            continue
        wanted = {_norm_key(label), _norm_key(spec.get("key") or label)}
        if label not in details:
            for k in spec_keys(details):
                if _norm_key(k) in wanted:
                    details[label] = details.pop(k)
                    old_flag = f"{k}{MUST_MATCH_SUFFIX}"
                    if old_flag in details:
                        details[f"{label}{MUST_MATCH_SUFFIX}"] = details.pop(old_flag)
                    break
        if spec.get("must_match") and label in details and f"{label}{MUST_MATCH_SUFFIX}" not in details:
            details[f"{label}{MUST_MATCH_SUFFIX}"] = True


def set_must_match(details: dict[str, Any], names: Any) -> list[str]:
    """Flag attributes already in ``details`` (case-insensitive) as must-match; unknown names are ignored."""
    done: list[str] = []
    if not isinstance(details, dict) or not isinstance(names, (list, tuple, set)):
        return done
    by_norm = {_norm_key(k): k for k in spec_keys(details)}
    for name in names:
        key = by_norm.get(_norm_key(name))
        if key is not None:
            details[f"{key}{MUST_MATCH_SUFFIX}"] = True
            done.append(key)
    return done


_MUST_PHRASE = (
    r"(?:must\s+match|should\s+match|has\s+to\s+match|need(?:s)?\s+to\s+match|match\s+karna\s+(?:zaroori|zaruri|jaruri)"
    r"|(?:is|are|hai|h)?\s*(?:mandatory|strict(?:ly)?|non[\s-]?negotiable|compulsory|a\s+must)"
    r"|(?:zaroori|zaruri|jaruri|zarori|jaroori)(?:\s+(?:hai|h))?)"
)


def must_match_from_text(draft: dict[str, Any], user_text: str) -> list[str]:
    """Phrases like "<attr> must match", "<attr> is mandatory", "<attr> zaroori hai" flag that attribute."""
    details = draft.get("product_details")
    text = user_text or ""
    if not isinstance(details, dict) or not text:
        return []
    done: list[str] = []
    for key in spec_keys(details):
        words = [re.escape(w) for w in re.split(r"[\s_\-]+", str(key).strip()) if w]
        if not words:
            continue
        key_re = r"[\s_\-]+".join(words)
        pattern = rf"(?<!\w){key_re}(?!\w)[^.;!?\n]{{0,30}}?{_MUST_PHRASE}"
        if re.search(pattern, text, re.IGNORECASE):
            details[f"{key}{MUST_MATCH_SUFFIX}"] = True
            done.append(key)
    return done


# ---------------------------------------------------------------------------
# Remote helpers (each swallowed by DraftAssist.refresh on failure)
# ---------------------------------------------------------------------------
async def _fetch_hsn_suggestion(name: str) -> Optional[dict[str, Any]]:
    from api.v1 import hsn as hsn_api

    item = await hsn_api.suggest_hsn(name)
    if item is None:
        return None
    return {
        "code": item.code,
        "description": item.description,
        "formatted_title": item.formatted_title,
        "category": item.category,
        "confidence": item.confidence,
    }


async def _fetch_spec_suggestions(name: str, category: Optional[str], role: Optional[str]) -> list[dict[str, Any]]:
    from schemas.rfq import SuggestedAttribute
    from services import attribute_recommender

    attrs, _source = await attribute_recommender.recommend_product_attributes(name, category, role)
    out: list[dict[str, Any]] = []
    for a in attrs or []:
        try:
            out.append(SuggestedAttribute(**a).model_dump())
        except Exception:
            continue
    return out


async def _fetch_moderation(text: str) -> dict[str, Any]:
    from services.moderation_service import AIContentModerator

    res = await asyncio.to_thread(AIContentModerator.check_text, text)
    return {"is_safe": bool(res.is_safe), "reason": res.reason, "category": res.category}


async def _guard(coro: Any, timeout: float, what: str) -> tuple[bool, Any]:
    try:
        return True, await asyncio.wait_for(coro, timeout=timeout)
    except Exception as exc:  # includes timeout
        logger.debug("Chat %s helper skipped: %s", what, exc)
        return False, None


# ---------------------------------------------------------------------------
# Per-conversation state
# ---------------------------------------------------------------------------
class DraftAssist:
    """Suggestions and moderation for one conversation's draft, loaded from and saved to conversation state."""

    def __init__(self, state: Optional[dict[str, Any]] = None) -> None:
        state = state or {}
        self.suggestions: dict[str, Any] = dict(state.get("draft_suggestions") or {})
        mod = state.get("draft_moderation")
        self.moderation: Optional[dict[str, Any]] = dict(mod) if isinstance(mod, dict) else None
        self.notes: list[str] = []  # short sentences appended to this turn's reply
        self.applied_hsn = False
        self.moderation_checked_now = False  # moderation was (re)computed this turn
        self._mod_tried: set[str] = set()

    # -- lifecycle -----------------------------------------------------
    def reset(self) -> None:
        self.suggestions = {}
        self.moderation = None

    @property
    def pending_hsn(self) -> Optional[dict[str, Any]]:
        hsn = self.suggestions.get("hsn")
        return hsn if isinstance(hsn, dict) and hsn.get("code") else None

    def public(self) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        return (self.suggestions or None), self.moderation

    # -- refresh -------------------------------------------------------
    async def refresh(self, draft: dict[str, Any], *, moderate: bool = True) -> None:
        """Recompute whatever the draft's changed fields call for, concurrently. Never raises."""
        try:
            await self._refresh(draft, moderate=moderate)
        except Exception as exc:
            logger.debug("Draft assist refresh failed: %s", exc)

    async def _refresh(self, draft: dict[str, Any], *, moderate: bool) -> None:
        name = product_name(draft)
        if not name:
            return
        sug = self.suggestions
        jobs: dict[str, Any] = {}
        if user_hsn(draft):
            sug["hsn"] = None
            sug["hsn_for"] = name
        elif sug.get("hsn_for") != name:
            jobs["hsn"] = _guard(_fetch_hsn_suggestion(name), HSN_TIMEOUT, "hsn")
        if sug.get("specs_for") != name:
            jobs["specs"] = _guard(
                _fetch_spec_suggestions(name, draft.get("category"), draft.get("role")), SPECS_TIMEOUT, "specs"
            )
        mtext = moderation_text(draft)
        mhash = text_hash(mtext)
        if moderate and (self.moderation or {}).get("text_hash") != mhash and mhash not in self._mod_tried:
            self._mod_tried.add(mhash)
            jobs["mod"] = _guard(_fetch_moderation(mtext), MODERATION_TIMEOUT, "moderation")
        if not jobs:
            return
        results = await asyncio.gather(*jobs.values())
        out = dict(zip(jobs.keys(), results))
        if "hsn" in out:
            _ok, value = out["hsn"]
            sug["hsn_for"] = name  # attempted: not recomputed until the product changes
            sug["hsn"] = value
            sug.pop("hsn_offered", None)
        if "specs" in out:
            ok, value = out["specs"]
            sug["specs_for"] = name
            sug["specs"] = value if ok and isinstance(value, list) else []
        if "mod" in out:
            ok, value = out["mod"]
            if ok and isinstance(value, dict):
                self.moderation = {"text_hash": mhash, **value}
                self.moderation_checked_now = True

    # -- moderation ----------------------------------------------------
    async def block_reason(self, draft: dict[str, Any]) -> Optional[str]:
        """Why creating this draft must be refused (unsafe content), or None. Re-checks only when the text changed."""
        if not moderation_text(draft):
            return None
        await self.refresh(draft)
        mod = self.moderation or {}
        if mod.get("text_hash") == text_hash(moderation_text(draft)) and mod.get("is_safe") is False:
            return self.warning_text(mod)
        return None

    @staticmethod
    def warning_text(mod: dict[str, Any]) -> str:
        reason = mod.get("reason") or "it contains prohibited content"
        return f"This listing may violate our content policy: {reason}. Please change it before publishing."

    def current_warning(self, draft: dict[str, Any]) -> Optional[str]:
        mod = self.moderation or {}
        if (
            mod.get("is_safe") is False
            and moderation_text(draft)
            and mod.get("text_hash") == text_hash(moderation_text(draft))
        ):
            return self.warning_text(mod)
        return None

    # -- prompt / reply ------------------------------------------------
    def unfilled_specs(self, draft: dict[str, Any]) -> list[str]:
        details = (draft or {}).get("product_details") or {}
        have = {_norm_key(k) for k in details}
        labels: list[str] = []
        for spec in self.suggestions.get("specs") or []:
            label = str(spec.get("label") or spec.get("key") or "").strip()
            if label and _norm_key(label) not in have and _norm_key(spec.get("key") or "") not in have:
                labels.append(label)
        return labels[:MAX_SPEC_PROMPTS]

    def prompt_section(self, draft: dict[str, Any]) -> str:
        lines: list[str] = []
        hsn = self.pending_hsn
        if hsn and not user_hsn(draft):
            title = hsn.get("formatted_title") or hsn.get("description") or ""
            line = f"- Suggested HSN {hsn['code']} - {title} (category {hsn.get('category') or 'n/a'}); NOT applied yet."
            if self.suggestions.get("hsn_offered"):
                line += " It was already offered; do not repeat the offer. Apply it only if the user agrees."
            else:
                line += f" Offer it ONCE in one short sentence ('Suggested HSN {hsn['code']} - {title}; use it?')."
            lines.append(line)
        if self.applied_hsn and user_hsn(draft):
            lines.append(f"- HSN {user_hsn(draft)} was just applied to the draft. Confirm it in one short sentence.")
        specs = self.unfilled_specs(draft)
        if specs:
            lines.append(
                "- Optional specifications you may ask about (one short question, not all at once): " + ", ".join(specs)
            )
        warn = self.current_warning(draft)
        if warn:
            lines.append(f"- CONTENT POLICY: {warn} Tell the user; do not offer to create the RFQ.")
        if not lines:
            return ""
        return "\nDRAFT HELPERS (same assistance as the RFQ form):\n" + "\n".join(lines)

    def extra_allowed_text(self, draft: dict[str, Any]) -> str:
        """Numbers the reply may quote: the suggested and the applied HSN code."""
        parts: list[str] = []
        hsn = self.pending_hsn
        if hsn:
            parts.append(f"HSN {hsn['code']} {hsn.get('description') or ''} {hsn.get('formatted_title') or ''}")
        applied = user_hsn(draft)
        if applied:
            parts.append(f"HSN {applied}")
        return "\n".join(parts)

    def note_reply(self, reply: str) -> None:
        """Remember that the assistant has offered the suggestion, so it is not offered again."""
        hsn = self.pending_hsn
        if hsn and str(hsn["code"]) in (reply or ""):
            self.suggestions["hsn_offered"] = True

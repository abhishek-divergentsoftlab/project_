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
import time
from typing import Any, Iterable, Optional

from services import hsn_service, spec_catalog
from services.categories import normalize_category

logger = logging.getLogger(__name__)

HSN_TIMEOUT = 3.0  # how long ONE TURN waits for the HSN suggestion at first; the fetch itself keeps running
HSN_TASK_BUDGET = 20.0  # the background fetch's own budget (the model call it makes queues behind the chat model's)
HSN_SETTLE_WAIT = 8.0  # extra wait, before the reply offers the suggestion, for a fetch that is still running
SPECS_TIMEOUT = 5.0  # how long ONE TURN waits for the spec suggestions; the fetch itself keeps running
SPECS_TASK_BUDGET = 30.0  # the background fetch's own budget
SPECS_RECAP_WAIT = 25.0  # extra wait, before a recap, for a fetch that is still running (a first generation takes ~12-30 s)
SPECS_CACHE_TTL = 24 * 3600.0
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


_QUALIFIER_WORDS = frozenset({
    "fresh", "dried", "dry", "frozen", "raw", "cooked", "roasted", "boiled", "processed", "ripe", "whole", "ground",
    "crushed", "smoked", "salted", "canned", "chilled", "husked", "milled", "polished", "refined", "crude", "unprocessed",
})


def hsn_qualifiers(suggestion: Optional[dict[str, Any]]) -> list[str]:
    """The distinguishing words of a suggested HSN's description ("Fresh Or Dried" -> fresh, dried)."""
    if not suggestion:
        return []
    text = f"{suggestion.get('description') or ''} {suggestion.get('formatted_title') or ''}".lower()
    found = [w for w in dict.fromkeys(re.findall(r"[a-z]+", text)) if w in _QUALIFIER_WORDS]
    for m in re.finditer(r"\b([a-z]+)\s+or\s+([a-z]+)\b", text):
        found.extend(w for w in m.groups() if w not in found and w not in ("including", "other", "not"))
    return found


def hsn_qualifier_answer(user_text: str, suggestion: Optional[dict[str, Any]]) -> Optional[str]:
    """A one-word answer ("fresh") that picks a qualifier of the pending HSN suggestion, or None."""
    words = re.findall(r"[a-z]+", (user_text or "").lower())
    if not words or len(words) > 3 or not suggestion or not suggestion.get("code"):
        return None
    quals = hsn_qualifiers(suggestion)
    for w in words:
        if w in quals:
            return w.capitalize()
    return None


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


def category_change_note(code: str, category: str) -> str:
    return f"Category set to {category} from HSN {code}."


def apply_hsn(
    draft: dict[str, Any], code: str, category: Optional[str], notes: Optional[list[str]] = None
) -> None:
    """What the form's Apply does: store the code as a manual pick and take its category.

    When that changes the draft's category the user is told once (``notes`` is the turn's reply notes).
    """
    details = dict(draft.get("product_details") or {})
    details["hsn_code"] = code
    details["hsn_source"] = "manual"
    draft["product_details"] = details
    if category and str(category).strip():
        normalized = normalize_category(category)
        if normalized != "General Goods" or not draft.get("category"):
            before = draft.get("category")
            draft["category"] = normalized
            if notes is not None and before != normalized:
                note = category_change_note(code, normalized)
                if note not in notes:
                    notes.append(note)


def apply_explicit_hsn(
    draft: dict[str, Any], raw: Any, notes: Optional[list[str]] = None
) -> tuple[str, Optional[dict[str, Any]]]:
    """Validate a typed code and apply it when known. Returns the lookup (status, item)."""
    status, item = lookup_hsn(raw)
    if status == "ok" and item:
        apply_hsn(draft, item["code"], item.get("category"), notes)
    return status, item


def unrecognised_hsn_note(raw: Any) -> str:
    return f"The HSN code {str(raw).strip()} wasn't recognised, so I haven't set it."


# ---------------------------------------------------------------------------
# Spec values and must-match
# ---------------------------------------------------------------------------
def family_for(draft: Optional[dict[str, Any]], specs: Optional[list[dict[str, Any]]] = None) -> spec_catalog.Family:
    """The catalogue family of the draft's product, built in memory from the suggestions (plus the common attributes)."""
    return spec_catalog.family_from_suggestions(
        product_name(draft), (draft or {}).get("category"), specs, user_hsn(draft)
    )


def apply_spec_suggestions(
    draft: dict[str, Any], specs: Optional[list[dict[str, Any]]], answered: Iterable[str] = ()
) -> None:
    """Put the draft's specs into the catalogue's canonical form (as the form's chips do).

    Every key the user or the model wrote ("Apple Variety", "pack_weight_kg", "Packaging Type") becomes the catalogue's
    key ("variety", "pack_size", "packaging_type") and every value its canonical value ("20000 g" -> "20 kg"). A suggestion
    that comes with a must_match preset sets ``<key>__must_match`` unless the user already answered that spec
    (``answered``: the keys they gave a value for) -- there is no ``False`` flag any more.
    """
    details = draft.get("product_details")
    if not isinstance(details, dict) or not product_name(draft):
        return
    fam = family_for(draft, specs)
    out = fam.normalize_details(details, strip_core=True)
    skip = set(answered)
    for spec in specs or []:
        key = str(spec.get("key") or "").strip()
        if key and spec.get("must_match") and key in out and key not in skip and f"{key}{MUST_MATCH_SUFFIX}" not in out:
            out[f"{key}{MUST_MATCH_SUFFIX}"] = True
    draft["product_details"] = out


def set_must_match(details: dict[str, Any], names: Any, family: Optional[spec_catalog.Family] = None) -> list[str]:
    """Flag attributes already in ``details`` (case-insensitive; labels and synonyms with a ``family``) as must-match."""
    done: list[str] = []
    if not isinstance(details, dict) or not isinstance(names, (list, tuple, set)):
        return done
    by_norm = {_norm_key(k): k for k in spec_keys(details)}
    for name in names:
        key = by_norm.get(_norm_key(name))
        if key is None and family is not None:
            canonical = family.canonical_key(str(name))
            key = canonical if canonical in details else None
        if key is not None:
            details[f"{key}{MUST_MATCH_SUFFIX}"] = True
            done.append(key)
    return done


_MUST_PHRASE = (
    r"(?:must\s+match|should\s+match|has\s+to\s+match|need(?:s)?\s+to\s+match|match\s+karna\s+(?:zaroori|zaruri|jaruri)"
    r"|(?:is|are|hai|h)?\s*(?:mandatory|strict(?:ly)?|non[\s-]?negotiable|compulsory|a\s+must)"
    r"|(?:zaroori|zaruri|jaruri|zarori|jaroori)(?:\s+(?:hai|h))?)"
)


def must_match_from_text(
    draft: dict[str, Any], user_text: str, specs: Optional[list[dict[str, Any]]] = None
) -> list[str]:
    """Phrases like "<attr> must match", "<attr> is mandatory", "<attr> zaroori hai" flag that attribute.

    ``<attr>`` is the spec's key, label or any synonym ("certification must of organic" flags ``certifications``).
    """
    details = draft.get("product_details")
    text = user_text or ""
    if not isinstance(details, dict) or not text:
        return []
    fam = family_for(draft, specs)
    done: list[str] = []
    for key in spec_keys(details):
        alternatives = []
        for phrase in fam.phrases(str(key)):
            words = [re.escape(w) for w in re.split(r"[\s_\-]+", phrase.strip()) if w]
            if words:
                alternatives.append(r"[\s_\-]+".join(words) + r"(?:s|es)?")
        if not alternatives:
            continue
        key_re = "(?:" + "|".join(alternatives) + ")"
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
    """The product family's attributes from the spec catalogue (generated once by the recommender, then stored)."""
    family = await spec_catalog.ensure_family(name, category, None, role)
    return family.suggestions() if family.persisted else []


# ---------------------------------------------------------------------------
# Spec suggestions run in the background: the recommender can take far longer than a turn may wait.
# ---------------------------------------------------------------------------
SpecKey = tuple[str, str, str]
_spec_tasks: dict[SpecKey, "asyncio.Task[list[dict[str, Any]]]"] = {}  # strong references: a task is never garbage-collected midway
_spec_cache: dict[SpecKey, tuple[float, list[dict[str, Any]]]] = {}


def spec_key(name: str, category: Optional[str], role: Optional[str]) -> SpecKey:
    return (_norm_key(name), _norm_key(category or ""), _norm_key(role or ""))


def cached_specs(key: SpecKey) -> Optional[list[dict[str, Any]]]:
    hit = _spec_cache.get(key)
    if hit is None:
        return None
    if time.monotonic() - hit[0] > SPECS_CACHE_TTL:
        _spec_cache.pop(key, None)
        return None
    return hit[1]


def reset_spec_state() -> None:
    """Forget cached results and cancel running fetches (tests, shutdown)."""
    for task in [*_spec_tasks.values(), *_hsn_tasks.values()]:
        try:
            task.cancel()
        except Exception:  # the loop is already closed
            pass
    _spec_tasks.clear()
    _spec_cache.clear()
    _hsn_tasks.clear()
    _hsn_cache.clear()


# ---------------------------------------------------------------------------
# The HSN suggestion runs in the background too (it asks the model, which is busy with the chat reply meanwhile).
# ---------------------------------------------------------------------------
_hsn_tasks: dict[str, "asyncio.Task[Optional[dict[str, Any]]]"] = {}
_hsn_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def hsn_key(name: str) -> str:
    return _norm_key(name)


def cached_hsn(key: str) -> Optional[dict[str, Any]]:
    hit = _hsn_cache.get(key)
    if hit is None:
        return None
    if time.monotonic() - hit[0] > SPECS_CACHE_TTL:
        _hsn_cache.pop(key, None)
        return None
    return hit[1]


async def _hsn_job(key: str, name: str) -> Optional[dict[str, Any]]:
    value = await asyncio.wait_for(_fetch_hsn_suggestion(name), timeout=HSN_TASK_BUDGET)
    if isinstance(value, dict) and value.get("code"):
        _hsn_cache[key] = (time.monotonic(), value)
    return value if isinstance(value, dict) else None


def running_hsn_task(key: str) -> Optional["asyncio.Task[Optional[dict[str, Any]]]"]:
    task = _hsn_tasks.get(key)
    if task is None:
        return None
    try:
        if task.done() or task.get_loop() is not asyncio.get_running_loop():
            _hsn_tasks.pop(key, None)
            return None
    except RuntimeError:
        return None
    return task


def start_hsn_task(key: str, name: str) -> "asyncio.Task[Optional[dict[str, Any]]]":
    task = running_hsn_task(key)
    if task is None:
        task = asyncio.create_task(_hsn_job(key, name))
        _hsn_tasks[key] = task

        def _done(t: "asyncio.Task[Any]", k: str = key) -> None:
            if _hsn_tasks.get(k) is t:
                _hsn_tasks.pop(k, None)
            if not t.cancelled() and t.exception() is not None:
                logger.debug("Chat HSN fetch failed: %s", t.exception())

        task.add_done_callback(_done)
    return task


async def _spec_job(key: SpecKey, name: str, category: Optional[str], role: Optional[str]) -> list[dict[str, Any]]:
    value = await asyncio.wait_for(_fetch_spec_suggestions(name, category, role), timeout=SPECS_TASK_BUDGET)
    if isinstance(value, list) and value:
        value = spec_catalog.canonicalize_suggestions(name, category, value)
        _spec_cache[key] = (time.monotonic(), value)
    return value if isinstance(value, list) else []


def _spec_task_done(key: SpecKey, task: "asyncio.Task[Any]") -> None:
    if _spec_tasks.get(key) is task:
        _spec_tasks.pop(key, None)
    if not task.cancelled() and task.exception() is not None:
        logger.debug("Chat specs fetch failed: %s", task.exception())


def running_spec_task(key: SpecKey) -> Optional["asyncio.Task[list[dict[str, Any]]]"]:
    task = _spec_tasks.get(key)
    if task is None:
        return None
    try:
        if task.done() or task.get_loop() is not asyncio.get_running_loop():
            _spec_tasks.pop(key, None)
            return None
    except RuntimeError:
        return None
    return task


def start_spec_task(
    key: SpecKey, name: str, category: Optional[str], role: Optional[str]
) -> "asyncio.Task[list[dict[str, Any]]]":
    """The one background fetch for this (product, category, role); started if none is running."""
    task = running_spec_task(key)
    if task is None:
        task = asyncio.create_task(_spec_job(key, name, category, role))
        _spec_tasks[key] = task
        task.add_done_callback(lambda t, k=key: _spec_task_done(k, t))
    return task


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
            hit_hsn = cached_hsn(hsn_key(name))
            if hit_hsn is not None:
                self._take_hsn(name, (True, hit_hsn, True))
            else:
                jobs["hsn"] = self._wait_specs(start_hsn_task(hsn_key(name), name), HSN_TIMEOUT)
        if sug.get("specs_for") != name:
            key = spec_key(name, draft.get("category"), draft.get("role"))
            hit = cached_specs(key)
            if hit is not None:
                sug["specs"], sug["specs_for"] = hit, name
            else:
                task = start_spec_task(key, name, draft.get("category"), draft.get("role"))
                jobs["specs"] = self._wait_specs(task, SPECS_TIMEOUT)
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
            self._take_hsn(name, out["hsn"])
        if "specs" in out:
            self._take_specs(name, out["specs"])
            if sug.get("specs_for") != name:
                sug["specs"] = []  # still running: never keep another product's suggestions meanwhile
        if "mod" in out:
            ok, value = out["mod"]
            if ok and isinstance(value, dict):
                self.moderation = {"text_hash": mhash, **value}
                self.moderation_checked_now = True

    # -- spec suggestions (background) ---------------------------------
    @staticmethod
    async def _wait_specs(task: "asyncio.Task[Any]", timeout: float) -> tuple[bool, Any, bool]:
        """Wait at most ``timeout`` for the fetch, without cancelling it. Returns (ok, value, finished)."""
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if task not in done:
            return False, None, False
        if task.cancelled() or task.exception() is not None:
            return False, None, True
        return True, task.result(), True

    def _take_specs(self, name: str, outcome: tuple[bool, Any, bool]) -> None:
        """Record a finished fetch. A fetch still running is NOT marked attempted: a later turn picks it up."""
        ok, value, finished = outcome
        if not finished:
            return
        self.suggestions["specs_for"] = name
        self.suggestions["specs"] = value if ok and isinstance(value, list) else []

    def _take_hsn(self, name: str, outcome: tuple[bool, Any, bool]) -> None:
        """Record a finished HSN fetch (a failed one counts as attempted). One still running is picked up by a later turn."""
        ok, value, finished = outcome
        if not finished:
            return
        sug = self.suggestions
        sug["hsn_for"] = name  # attempted: not recomputed until the product changes
        sug["hsn"] = value if ok and isinstance(value, dict) else None
        sug.pop("hsn_offered", None)
        sug.pop("hsn_offers", None)

    def hsn_pending(self, draft: dict[str, Any]) -> bool:
        """True while the HSN fetch for this draft's product is still running."""
        name = product_name(draft)
        if not name or user_hsn(draft) or self.suggestions.get("hsn_for") == name:
            return False
        return running_hsn_task(hsn_key(name)) is not None

    async def settle_hsn(self, draft: dict[str, Any], timeout: float = HSN_SETTLE_WAIT) -> None:
        """Wait up to ``timeout`` for a running HSN fetch (before the reply that offers it)."""
        name = product_name(draft)
        if not name or user_hsn(draft) or self.suggestions.get("hsn_for") == name:
            return
        hit = cached_hsn(hsn_key(name))
        if hit is not None:
            self._take_hsn(name, (True, hit, True))
            return
        task = running_hsn_task(hsn_key(name))
        if task is not None:
            self._take_hsn(name, await self._wait_specs(task, timeout))

    def specs_pending(self, draft: dict[str, Any]) -> bool:
        """True while the spec fetch for this draft's product is still running."""
        name = product_name(draft)
        if not name or self.suggestions.get("specs_for") == name:
            return False
        return running_spec_task(spec_key(name, draft.get("category"), draft.get("role"))) is not None

    async def settle_specs(self, draft: dict[str, Any], timeout: float = SPECS_RECAP_WAIT) -> None:
        """Wait up to ``timeout`` for a running spec fetch (before a recap, so the suggestions are not skipped)."""
        name = product_name(draft)
        if not name or self.suggestions.get("specs_for") == name:
            return
        key = spec_key(name, draft.get("category"), draft.get("role"))
        hit = cached_specs(key)
        if hit is not None:
            self.suggestions["specs"], self.suggestions["specs_for"] = hit, name
            return
        task = running_spec_task(key)
        if task is not None:
            self._take_specs(name, await self._wait_specs(task, timeout))

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

    def prompt_section(self, draft: dict[str, Any], flow_asks: bool = False) -> str:
        """The helper facts for the model. With ``flow_asks`` the conversation flow makes every offer and asks every
        question itself, so the model is told the suggestions only as facts (never to offer or ask)."""
        lines: list[str] = []
        hsn = self.pending_hsn
        if hsn and not user_hsn(draft):
            title = hsn.get("formatted_title") or hsn.get("description") or ""
            line = f"- Suggested HSN {hsn['code']} - {title} (category {hsn.get('category') or 'n/a'}); NOT applied yet."
            if flow_asks:
                line += " The offer is added automatically after your reply: do not mention or offer it yourself."
            elif self.suggestions.get("hsn_offered"):
                line += " It was already offered; do not repeat the offer. Apply it only if the user agrees."
            else:
                line += f" Offer it ONCE in one short sentence ('Suggested HSN {hsn['code']} - {title}; use it?')."
            lines.append(line)
        if self.applied_hsn and user_hsn(draft):
            lines.append(f"- HSN {user_hsn(draft)} was just applied to the draft. Confirm it in one short sentence.")
        if hsn or user_hsn(draft):
            lines.append("- Never say that no HSN code was found while an HSN is listed above or applied to the draft.")
        specs = [] if flow_asks else self.unfilled_specs(draft)
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
        """Numbers the reply may quote: the suggested and the applied HSN code, and the suggested specs with their
        options (they are facts the assistant itself offered, like the HSN suggestion)."""
        parts: list[str] = []
        for spec in self.suggestions.get("specs") or []:
            if isinstance(spec, dict):
                parts.append(" ".join(str(x) for x in (
                    spec.get("label"), spec.get("key"), spec.get("placeholder"), *(spec.get("options") or [])
                ) if x))
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

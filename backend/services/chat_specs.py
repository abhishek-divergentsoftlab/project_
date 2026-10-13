"""Keeping the chat draft's ``product_details`` honest and clean.

Three jobs, all pure functions:

* ``clean_product_details`` strips the form's core fields out of the free-form specs (a model that
  writes ``quantity_value`` into ``product_details`` must not create a "spec" of that name), flattens
  nested values to text and drops unit-suffixed duplicates.
* ``ground_product_details`` keeps only the entries the USER's own messages support, so a model that
  volunteers "grade Class I, min Brix 14" for a product the user described in four words writes none of it.
* ``normalize_per_unit`` turns "per kg" / "/kg" / True into the bare unit the price is quoted per.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from services.query_grounding import _number_grounded

logger = logging.getLogger(__name__)

MUST_MATCH_SUFFIX = "__must_match"

# Keys that belong to the RFQ form itself, never to the free-form specs.
CORE_KEYS = frozenset({
    "quantity_value", "quantity_unit", "quantity", "price_amount", "price_currency", "price_per_unit", "price",
    "min_order_value", "min_order_unit", "min_order", "minimum_order", "city", "state", "country", "location",
    "deadline_days", "deadline_date", "deadline", "role", "category", "status", "title", "description",
    "product_name",
})
_KEEP_HSN = {"hsn_code", "hsn_source"}


def _norm(key: Any) -> str:
    return re.sub(r"[\s\-]+", "_", str(key).strip().lower())


def is_core_key(key: Any) -> bool:
    k = _norm(key)
    if k.endswith(MUST_MATCH_SUFFIX):
        return False
    if k.startswith("hsn_"):
        return k not in _KEEP_HSN
    return k in CORE_KEYS


# ---------------------------------------------------------------------------
# Price unit
# ---------------------------------------------------------------------------
_PER_PREFIX_RE = re.compile(r"^\s*(?:(?:per|par|prati|प्रति)\b\s*|/\s*)+", re.IGNORECASE)


def normalize_per_unit(value: Any) -> Optional[str]:
    """"per kg" / "par kg" / "/kg" -> "kg"; booleans and empty values -> None."""
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    text = _PER_PREFIX_RE.sub("", str(value)).strip(" .")
    if not text or text.lower() in ("true", "false", "yes", "no", "none", "null", "n/a", "per"):
        return None
    return text


# ---------------------------------------------------------------------------
# Flatten and clean
# ---------------------------------------------------------------------------
_VALUE_KEYS = {"value", "amount", "qty", "quantity", "number"}
_UNIT_KEYS = {"unit", "units", "uom"}


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _flatten_into(out: dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, dict):
        keys = {_norm(k) for k in value}
        if keys and keys <= (_VALUE_KEYS | _UNIT_KEYS) and keys & _VALUE_KEYS:
            val = next(v for k, v in value.items() if _norm(k) in _VALUE_KEYS)
            unit = next((v for k, v in value.items() if _norm(k) in _UNIT_KEYS), "")
            text = f"{_as_text(val)} {unit or ''}".strip()
            if text:
                out.setdefault(key, text)
            return
        for inner_key, inner in value.items():
            _flatten_into(out, str(inner_key), inner)
        return
    if isinstance(value, (list, tuple, set)):
        items = [_as_text(i) if not isinstance(i, dict) else ", ".join(
            f"{k} {_as_text(v)}" for k, v in i.items()
        ) for i in value if i not in (None, "")]
        if items:
            out[key] = items
        return
    if value is None or value == "":
        return
    out[key] = value


_UNIT_SUFFIX_RE = re.compile(r"^(.+?)_(kg|g|mg|gm|mm|cm|m|l|ml|pcs|pc|days|day|mt|n_cm2|percent|pct)$")
_NUM_IN_TEXT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    return [float(m.replace(",", "")) for m in _NUM_IN_TEXT_RE.findall(str(value))]


def drop_unit_variants(details: dict[str, Any]) -> dict[str, Any]:
    """Drop ``pack_weight_kg: 20`` when ``pack_weight: "20 kg"`` already says it."""
    by_norm = {_norm(k): k for k in details}
    for key in list(details):
        m = _UNIT_SUFFIX_RE.match(_norm(key))
        if not m:
            continue
        base_key = by_norm.get(m.group(1))
        if base_key is None or base_key == key:
            continue
        mine, base = _numbers(details[key]), _numbers(details[base_key])
        if mine and base and mine == base:
            details.pop(key, None)
            details.pop(f"{key}{MUST_MATCH_SUFFIX}", None)
    return details


def flatten_details(details: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Nested dicts become text or top-level keys; ``{"value": 20, "unit": "kg"}`` becomes "20 kg"."""
    out: dict[str, Any] = {}
    for key, value in (details or {}).items():
        _flatten_into(out, str(key), value)
    return drop_unit_variants(out)


def strip_core(details: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split ``details`` into (specs, core entries) where core entries are the form's own fields."""
    clean: dict[str, Any] = {}
    core: dict[str, Any] = {}
    for key, value in details.items():
        (core if is_core_key(key) else clean)[key] = value
    return clean, core


def clean_product_details(details: Optional[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flatten, then split into (clean specs, core entries that were stripped out)."""
    return strip_core(flatten_details(details))


def core_as_fields(core: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """Core entries a model put into the specs, as tool arguments for the draft fields that are still empty."""
    fields: dict[str, Any] = {}
    norm = {_norm(k): v for k, v in core.items()}
    if not draft.get("quantity") and norm.get("quantity_value") is not None:
        fields["quantity_value"] = norm["quantity_value"]
        if norm.get("quantity_unit"):
            fields["quantity_unit"] = norm["quantity_unit"]
    if not draft.get("price_target") and norm.get("price_amount") is not None:
        fields["price_amount"] = norm["price_amount"]
        for k in ("price_currency", "price_per_unit"):
            if norm.get(k) is not None:
                fields[k] = norm[k]
    if not draft.get("minimum_order") and norm.get("min_order_value") is not None:
        fields["min_order_value"] = norm["min_order_value"]
        if norm.get("min_order_unit"):
            fields["min_order_unit"] = norm["min_order_unit"]
    loc = draft.get("location") or {}
    for k in ("city", "state", "country"):
        if not loc.get(k) and norm.get(k):
            fields[k] = norm[k]
    dl = draft.get("deadline") or {}
    if not dl and norm.get("deadline_days") is not None:
        fields["deadline_days"] = norm["deadline_days"]
    if not dl and norm.get("deadline_date"):
        fields["deadline_date"] = norm["deadline_date"]
    if not draft.get("category") and norm.get("category"):
        fields["category"] = norm["category"]
    return fields


# ---------------------------------------------------------------------------
# Grounding in the user's own words
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[a-zऀ-ॿ]+|\d+(?:\.\d+)?")
_STOP = frozenset({
    "a", "an", "the", "of", "and", "or", "in", "on", "to", "for", "with", "per", "each", "is", "are", "be",
    "at", "as", "by", "from", "kg", "g", "mg", "gm", "mm", "cm", "m", "l", "ml", "pcs", "mt",
})
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "organic": ("jaivik", "bio", "organically"),
    "kg": ("kilo", "kilos", "kilogram", "kilograms", "kgs"),
    "tonne": ("ton", "tons", "tonnes", "mt"),
    "carton": ("box", "boxes", "cartons"),
    "box": ("carton", "cartons", "boxes"),
    "bag": ("bags", "sack", "sacks", "bori", "bora"),
    "yes": ("true",),
}


def _stem(word: str) -> str:
    w = word.lower()
    for suffix in ("ies", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return w


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower().replace("’", "'"))


def _word_grounded(word: str, user_words: set[str], user_stems: set[str]) -> bool:
    if word in user_words or _stem(word) in user_stems:
        return True
    if len(word) >= 5 and any(len(u) >= 5 and u[:5] == word[:5] for u in user_words):
        return True  # "organically" ~ "organic", "certified" ~ "certification": a shared five-letter stem
    for syn in _SYNONYMS.get(_stem(word), ()) + _SYNONYMS.get(word, ()):
        if syn in user_words or _stem(syn) in user_stems:
            return True
    return False


def text_grounded(value: str, user_text: str) -> bool:
    """True when every content word and number of ``value`` appears (or has a known synonym) in ``user_text``."""
    words = _tokens(value)
    if not words:
        return False
    user_tokens = _tokens(user_text)
    user_words = {t for t in user_tokens if not t[0].isdigit()}
    user_stems = {_stem(t) for t in user_words}
    seen = False
    for w in words:
        if w[0].isdigit():
            if not _number_grounded(w, user_text):
                return False
            seen = True
            continue
        if w in _STOP:
            continue
        if not _word_grounded(w, user_words, user_stems):
            return False
        seen = True
    if not seen:  # only filler words ("A", "I"): they count only when the user wrote exactly them
        return all(w in user_words for w in words)
    return True


def _key_words(key: str) -> list[str]:
    return [w for w in re.split(r"[\s_\-]+", str(key).lower()) if len(w) >= 3 and w not in _STOP]


def _entry_grounded(key: str, value: Any, user_text: str) -> Any:
    """The part of ``value`` the user's text supports, or None."""
    if isinstance(value, bool):
        if not value:
            return None  # "not organic" is a claim about the user's words, not a spec the model may invent
        user_words = {t for t in _tokens(user_text) if not t[0].isdigit()}
        user_stems = {_stem(t) for t in user_words}
        return True if any(_word_grounded(w, user_words, user_stems) for w in _key_words(key)) else None
    if isinstance(value, (int, float)):
        return value if _number_grounded(value, user_text) else None
    if isinstance(value, (list, tuple)):
        kept = [v for v in value if _entry_grounded(key, v, user_text) is not None]
        return kept or None
    text = str(value).strip()
    return text if text and text_grounded(text, user_text) else None


def ground_product_details(
    details: dict[str, Any], user_text: str, *, keep: frozenset[str] = frozenset(_KEEP_HSN)
) -> tuple[dict[str, Any], list[str]]:
    """Keep only the entries the user's own messages support. Returns (kept, dropped key names)."""
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in details.items():
        k = _norm(key)
        if k in keep or k.endswith(MUST_MATCH_SUFFIX):
            kept[key] = value
            continue
        grounded = _entry_grounded(str(key), value, user_text)
        if grounded is None:
            dropped.append(str(key))
            logger.info("dropped ungrounded spec %r=%r (not in the user's messages)", key, value)
            continue
        kept[key] = grounded
    # A must-match flag survives only with its spec.
    for key in list(kept):
        if _norm(key).endswith(MUST_MATCH_SUFFIX):
            base = str(key)[: -len(MUST_MATCH_SUFFIX)]
            if base not in kept:
                kept.pop(key)
    return kept, dropped


_ASKS_FOR_DESCRIPTION_RE = re.compile(
    r"\b(descriptions?|describe|notes?|remarks?|add\s+(?:a\s+)?(?:detail|comment)s?|comments?)\b", re.IGNORECASE
)


def description_allowed(description: Any, last_user_text: str) -> bool:
    """A model-written description is stored only when the user asked for one or wrote the text themselves."""
    text = str(description or "").strip()
    if not text:
        return False
    if _ASKS_FOR_DESCRIPTION_RE.search(last_user_text or ""):
        return True
    return text_grounded(text, last_user_text)


# ---------------------------------------------------------------------------
# Messages that say nothing about the draft, and the user's own role statements
# ---------------------------------------------------------------------------
import difflib  # noqa: E402

_OWN_PRONOUN = r"(?:i|we|main|mai|mein|hum|humein|ham)"
_SELL_OWN_RE = re.compile(
    rf"\b{_OWN_PRONOUN}\b(?:\s*'?\s*[\w']+){{0,4}}?\s+(?:sell|selling|supply|supplying|offer|offering|seller|supplier|vendor|exporter|"
    r"manufacturer|wholesaler|bech\w*|becn\w*)s?\b",
    re.IGNORECASE,
)
_BUY_OWN_RE = re.compile(
    rf"\b{_OWN_PRONOUN}\b(?:\s*'?\s*[\w']+){{0,4}}?\s+(?:buy|buying|purchase|purchasing|procure|procuring|source|sourcing|need|"
    r"require|buyer|importer|looking\s+for|khareed\w*|kharid\w*|chahiye|chahta|chahti|chahte)s?\b",
    re.IGNORECASE,
)
_ROLE_SWITCH_RE = re.compile(
    r"\b(?:from)\s+(?:a\s+)?(?:buyer|seller|supplier)\s+to\s+(?:a\s+|an\s+)?(buyer|seller|supplier)\b"
    r"|\b(?:change|switch|convert|make|set|update)\b[^.?!\n]{0,30}\b(?:to|as)\s+(?:a\s+|an\s+)?(buyer|seller|supplier|buying|selling)\b",
    re.IGNORECASE,
)
_ROLE_WORD = {"buyer": "buyer", "buying": "buyer", "seller": "seller", "selling": "seller", "supplier": "seller"}

# Words of a confirmation, a command, a greeting, thanks or an instruction about the conversation itself
# ("continue refining it", "go ahead", "change it", "skip"). None of them is a product, a role or a spec.
_COMMAND_WORDS = frozenset({
    "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "haan", "han", "ha", "ji", "please", "plz", "no", "nope", "nah",
    "nahi", "nahin", "wait", "hold", "on", "not", "yet", "stop", "cancel", "ruko", "publish", "post", "create",
    "submit", "proceed", "continue", "go", "ahead", "it", "this", "that", "now", "rfq", "listing", "the", "my",
    "confirm", "confirmed", "save", "draft", "as", "a", "do", "kar", "kardo", "karo", "dijiye", "done", "finalize",
    "finalise", "raise", "make", "and", "then", "theek", "thik", "hai", "sahi", "live", "looks", "good", "all", "set",
    "right", "correct", "bana", "banao", "de", "dena", "abhi", "fine", "great", "perfect", "cool", "hi", "hello", "hey",
    "hii", "namaste", "thanks", "thank", "you", "bye", "so", "just", "only", "more", "details", "detail", "refine",
    "refining", "refinement", "improve", "improving", "edit", "change", "modify", "adjust", "update", "tweak", "next",
    "skip", "ahead", "with", "me", "some", "again", "first", "also", "too", "other", "else", "anything", "something",
    "add", "adding", "keep", "going", "lets", "let's", "let", "us", "is", "to", "for", "i", "we",
})
_FUZZY_VOCAB = tuple(sorted(w for w in _COMMAND_WORDS if len(w) >= 5))
_REFINE_WORDS = frozenset({"continue", "refine", "refining", "refinement", "improve", "improving", "more", "details", "detail",
                           "tweak", "adjust", "modify", "add", "adding"})


def _command_token(token: str) -> bool:
    if token in _COMMAND_WORDS:
        return True
    # typos: "refinig", "proceeed", "continu"
    return len(token) >= 5 and bool(difflib.get_close_matches(token, _FUZZY_VOCAB, n=1, cutoff=0.8))


def _tokens_of(message: str) -> list[str]:
    return re.findall(r"[a-zऀ-ॿ']+", (message or "").lower().replace("’", "'"))


def is_content_free(message: str) -> bool:
    """A confirmation, command, greeting, thanks or instruction about the conversation ("continue refinig it").

    It carries no product, role or spec of its own. Token-level (typos included), never exact phrases.
    """
    tokens = _tokens_of(message)
    if not tokens or re.search(r"\d", message or ""):
        return False
    return all(_command_token(t) for t in tokens)


def wants_to_refine(message: str) -> bool:
    """"continue refining it", "add more details", "refine": the user wants to add more, not to publish."""
    tokens = _tokens_of(message)
    if not tokens or not is_content_free(message):
        return False
    close = lambda t: t in _REFINE_WORDS or (len(t) >= 5 and bool(difflib.get_close_matches(t, sorted(_REFINE_WORDS), n=1, cutoff=0.8)))  # noqa: E731
    return any(close(t) for t in tokens)


def explicit_own_role(message: str) -> Optional[str]:
    """The role the user states for themselves ("I am selling", "main bech raha hoon", "actually I'm the seller",
    "change it to seller"), or None. Commands and confirmations state none."""
    text = message or ""
    m = _ROLE_SWITCH_RE.search(text)
    if m:
        word = next(g for g in m.groups() if g)
        return _ROLE_WORD.get(word.lower(), "seller" if "sell" in word.lower() else "buyer")
    found = [(mm.start(), "seller") for mm in (_SELL_OWN_RE.search(text),) if mm]
    found += [(mm.start(), "buyer") for mm in (_BUY_OWN_RE.search(text),) if mm]
    mm = re.search(
        r"\b(?:i\s+am|i'm|im|we\s+are|we're)\s+(?:actually\s+)?(?:the\s+|a\s+|an\s+)?(buyer|seller|supplier|vendor)\b", text, re.IGNORECASE
    )
    if mm:
        found.append((mm.start(), "buyer" if mm.group(1).lower() == "buyer" else "seller"))
    return min(found)[1] if found else None


# ---------------------------------------------------------------------------
# Renaming the product
# ---------------------------------------------------------------------------
def _name_tokens(name: str) -> set[str]:
    return {_stem(t) for t in _tokens(name) if not t[0].isdigit() and t not in _STOP}


def same_product(a: str, b: str) -> bool:
    return bool(a and b) and _name_tokens(a) == _name_tokens(b)


def rename_allowed(new_name: str, existing_name: str, message: str, stated_product: Optional[str]) -> bool:
    """May ``new_name`` become the draft's product name?

    Never from a command or instruction ("continue refinig it" is not the product "Continue Refinig"). With a
    name already on the draft, only when this message itself states a product and the new name is that product.
    """
    if not new_name or is_content_free(message):
        return False
    if not existing_name or same_product(new_name, existing_name):
        return True
    if not stated_product:
        return False
    new_t, stated_t = _name_tokens(new_name), _name_tokens(stated_product)
    # the stated product itself, or its fuller form written in the message ("sabut moong" for "moong")
    return new_t <= stated_t or (stated_t <= new_t and text_grounded(new_name, message))


_CHANGE_PRODUCT_RE = re.compile(
    r"\b(?:instead|actually|rather|change|switch|replace|product\s*(?:name\s*)?(?:is|to|=)|item\s+(?:is|to))\b", re.IGNORECASE
)


def states_product_change(message: str) -> bool:
    """The message says outright that the product is a different one (a trade statement or a change phrase)."""
    return bool(explicit_own_role(message) or _CHANGE_PRODUCT_RE.search(message or ""))

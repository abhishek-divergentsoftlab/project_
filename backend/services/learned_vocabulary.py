"""Vocabulary that grows from grounded AI parses.

Closed lists in ``query_extractor`` are only seeds. Every time the model
understands something the rules did not (a new category, a role phrase, an
attribute value, a unit), the grounded result is remembered here so the offline
rule parser understands it next time without a model call.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from core.config import settings
from models.enums import RFQRole
from services.categories import CANONICAL_CATEGORIES

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 5000
# Anything learned from a chat message changes how every user is understood, so one user (or one session) must not
# be able to redefine a word for everyone ("by gold I mean TMT steel bars"). A new entry is only a candidate with the
# set of users who produced it; it becomes active when this many DISTINCT users produced the same mapping.
PROMOTE_MIN_USERS = 3
_MAX_CANDIDATES = 5000
# Words that are commands to the chat, not evidence of a role. "publish it" says nothing about being a seller.
_COMMAND_WORDS = frozenset({
    "publish", "post", "create", "submit", "confirm", "go", "live", "send", "list", "make", "proceed", "finalize",
    "finalise", "save", "start", "launch", "upload", "add", "rfq", "listing", "request", "draft", "it", "now", "this",
    "that", "please", "ok", "okay", "yes", "done", "do", "the", "a", "my", "new", "again", "karo", "kar", "do",
})

# Function words, pronouns, fillers and chat commands (English and Romanised Hindi). Generic grammar, not a product
# vocabulary: none of these is ever a property value, a product or a role phrase.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "so", "of", "to", "in", "on", "at", "by", "for", "from", "with",
    "without", "is", "are", "was", "were", "be", "been", "am", "i", "me", "my", "we", "us", "our", "you", "your", "he",
    "she", "it", "its", "they", "them", "this", "that", "these", "those", "what", "which", "who", "how", "why", "when",
    "where", "not", "no", "yes", "ok", "okay", "all", "any", "some", "more", "most", "very", "just", "also", "too",
    "need", "want", "have", "has", "had", "get", "got", "give", "take", "make", "sell", "buy", "show", "tell", "say",
    "call", "ask", "asking", "anything", "everything", "nothing", "something", "hai", "hain", "ka", "ki", "ke", "ko",
    "se", "me", "mein", "par", "pe", "aur", "ya", "nahi", "haan", "kya", "ye", "yeh", "wo", "woh", "mujhe", "chahiye",
    "sir", "madam", "hello", "hi", "thanks", "thank", "please", "sorry", "actually",
})
# Keys that are never a product property: they are the draft's own fields.
_CORE_FIELD_KEYS = frozenset({
    "quantity", "quantity_unit", "unit", "units", "price", "price_currency", "currency", "price_per_unit", "amount",
    "location", "city", "state", "country", "deadline", "role", "name", "product", "category", "min_quantity",
    "max_quantity", "moq", "hsn", "hsn_code",
})
_LETTERS_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def _alpha_len(text: str) -> int:
    return len(_LETTERS_RE.findall(text or ""))


def _tokens_of(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", (text or "").lower(), re.UNICODE)


def is_currency_word(text: str) -> bool:
    """An ISO 4217 code, a currency symbol or a currency name."""
    word = (text or "").strip().lower().rstrip(".")
    if not word:
        return False
    if word in {"₹", "$", "€", "£", "¥", "rs", "rupee", "rupees", "rupaye", "rupaya", "dollar", "dollars", "euro", "euros",
                "dirham", "dirhams", "yen", "yuan", "pound", "pounds"}:
        return True
    if re.fullmatch(r"[a-z]{3}", word):
        try:
            from services import query_grounding

            return query_grounding.valid_currency(word) is not None
        except Exception:
            return False
    return False


def is_unit_word(text: str) -> bool:
    """A measurement or packaging unit (kg, tonne, quintal, boxes, dozen ...), from the unit converter's tables."""
    try:
        from services import unit_converter

        word = (text or "").strip().lower()
        cleaned = unit_converter.clean_unit(word)
        if not cleaned:
            return False
        singular = cleaned[:-1] if cleaned.endswith("s") and len(cleaned) > 3 else cleaned
        return any(
            unit_converter.base_unit(c) is not None or c in unit_converter.PACKAGING_UNITS
            for c in (cleaned, singular, f"{cleaned}s")
        )
    except Exception:
        return False


def is_place_word(text: str) -> bool:
    """A city, state or country the gazetteer knows (exact names only: no fuzzy guess)."""
    try:
        from services import locations

        phrase = (text or "").strip()
        if not phrase or len(phrase.split()) > 3:
            return False
        loc = locations.exact_location(phrase)
        return loc is not None and (loc.matched_key or "").replace("-", " ") in " ".join(_tokens_of(phrase)) + " "
    except Exception:
        return False


def is_measurement(text: str) -> bool:
    """A number with its unit ("2mm", "99%", "540 watt", "18x12x12 in"): the extractor reads these by pattern."""
    t = (text or "").strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?\s*%?\s*[a-z\u00b5\u03bc\"']{0,8}", t):
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?(?:\s*x\s*\d+(?:\.\d+)?)+\s*[a-z]{0,6}", t):
        return True
    return bool(re.fullmatch(r"\d+(?:\.\d+)?\s*[a-z]+", t)) and is_unit_word(re.sub(r"^[\d.\s]+", "", t))


def valid_attribute_value(key: str, value: Any) -> bool:
    """May ``value`` be remembered as a value of property ``key``?

    Rejects what is not a property of goods: numbers and measurements, currencies, units, places, stop and command
    words, and any key that is one of the draft's own fields."""
    if not isinstance(value, str) or not isinstance(key, str):
        return False
    text = " ".join(value.strip().lower().split())
    norm_key = re.sub(r"[^\w]+", "_", key.strip().lower()).strip("_")
    if not text or not norm_key or norm_key in _CORE_FIELD_KEYS or len(text) > 40:
        return False
    if _alpha_len(text) < 2 or is_measurement(text):
        return False
    words = _tokens_of(text)
    if not words or all(w in _STOPWORDS or w in _COMMAND_WORDS for w in words):
        return False
    if any(is_currency_word(w) for w in words) or is_unit_word(text) or all(is_unit_word(w) for w in words):
        return False
    if is_place_word(text):
        return False
    return True


def valid_product_term(term: Any) -> bool:
    """A plausible product name or alias: letters, no markup or code, a few words, not a function word or a unit."""
    if not isinstance(term, str):
        return False
    text = " ".join(term.strip().lower().split())
    if not text or len(text) > 60 or len(text.split()) > 5 or _alpha_len(text) < 2:
        return False
    if re.search(r"[<>{}\[\]=;|\\]|--|\bdrop\s+table\b", text):
        return False
    words = _tokens_of(text)
    if not words or all(w in _STOPWORDS or w in _COMMAND_WORDS for w in words):
        return False
    if all(is_unit_word(w) or is_currency_word(w) for w in words):
        return False
    return True


def valid_role_phrase(phrase: Any) -> bool:
    text = " ".join(str(phrase or "").strip().lower().split())
    if not text or "?" in text or len(text.split()) > 6 or is_command_only(text):
        return False
    return not re.search(r"\d", text) and ".." not in text and "…" not in text


def scope_of(category: Optional[str]) -> str:
    """The product family an attribute value was seen in ("*" when unknown: hints only, never applied alone)."""
    return " ".join(str(category or "").strip().lower().split()) or "*"


def _split_scope(key: str) -> tuple[str, str]:
    if "::" in key:
        scope, value = key.split("::", 1)
        return scope, value
    return "*", key


def sanitize_store(data: dict[str, Any]) -> bool:
    """Drop every entry that the validators above reject; legacy attribute values with no product family are dropped.
    Returns True when something changed."""
    changed = False
    attrs = data.get("attribute_values")
    if isinstance(attrs, dict):
        clean: dict[str, str] = {}
        for raw_key, prop in attrs.items():
            scope, value = _split_scope(str(raw_key))
            # an entry with no product family cannot be scoped, so it cannot be trusted: it is dropped
            if scope != "*" and valid_attribute_value(str(prop), value):
                clean[f"{scope}::{value}"] = prop
        if clean != attrs:
            data["attribute_values"], changed = clean, True
    for kind in ("product_category", "product_synonyms"):
        mapping = data.get(kind)
        if isinstance(mapping, dict):
            clean_map = {k: v for k, v in mapping.items() if valid_product_term(k)}
            if clean_map != mapping:
                data[kind], changed = clean_map, True
    roles = data.get("role_phrases")
    if isinstance(roles, dict):
        clean_roles = {k: v for k, v in roles.items() if valid_role_phrase(k)}
        if clean_roles != roles:
            data["role_phrases"], changed = clean_roles, True
    terms = data.get("unknown_terms")
    if isinstance(terms, dict):
        clean_terms = {k: v for k, v in terms.items()
                       if valid_product_term(k) and not all(w in _STOPWORDS or w in _COMMAND_WORDS for w in _tokens_of(k))}
        if clean_terms != terms:
            data["unknown_terms"], changed = clean_terms, True
    cands = data.get("candidates")
    if isinstance(cands, dict):
        for kind, slots in list(cands.items()):
            if not isinstance(slots, dict):
                continue
            keep: dict[str, Any] = {}
            for slot, voters in slots.items():
                key, _, value = slot.partition("\t")
                if kind == "attribute_values":
                    scope, plain = _split_scope(key)
                    ok = valid_attribute_value(value, plain) if value else False
                elif kind in ("product_category", "product_synonyms"):
                    ok = valid_product_term(key)
                elif kind == "role_phrases":
                    ok = valid_role_phrase(key)
                else:
                    ok = True
                if ok:
                    keep[slot] = voters
            if keep != slots:
                cands[kind], changed = keep, True
    return changed

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_lock = threading.RLock()
_data: Optional[dict[str, Any]] = None
_path_override: Optional[Path] = None


def _empty() -> dict[str, Any]:
    return {
        "product_category": {},
        "role_phrases": {},
        "attribute_values": {},
        "units": [],
        "categories": [],
        "unknown_terms": {},
        # kind -> "key\tvalue" -> hashed ids of the distinct users who produced it (see PROMOTE_MIN_USERS)
        "candidates": {},
        "product_synonyms": {
            # Regional mandi terms (Tamil, Telugu, Gujarati, Marathi)
            "thakkali": "tomato",
            "vengayam": "onion",
            "urulaikizhangu": "potato",
            "poondu": "garlic",
            "inji": "ginger",
            "kathirikai": "brinjal",
            "vendaikkai": "okra",
            "tamata": "tomato",
            "ullipayalu": "onion",
            "bangaladumpa": "potato",
            "vankaya": "brinjal",
            "bhendakaya": "okra",
            "vellulli": "garlic",
            "allam": "ginger",
            "dungri": "onion",
            "batata": "potato",
            "bataka": "potato",
            "tameta": "tomato",
            "ringna": "brinjal",
            "bhinda": "okra",
            "vange": "brinjal",
            "kanda": "onion",
            # Industry terms
            "ipa": "isopropyl alcohol",
            "paracetamol": "paracetamol",
            "amoxicillin": "amoxicillin",
        },
    }


def _path() -> Path:
    if _path_override is not None:
        return _path_override
    raw = Path(getattr(settings, "LEARNED_VOCAB_PATH", "") or "data/learned_vocab.json")
    return raw if raw.is_absolute() else _BACKEND_DIR / raw


def set_path(path: Optional[Path | str]) -> None:
    """Point the store at another file (tests) and drop the in-memory copy."""
    global _path_override, _data
    with _lock:
        _path_override = Path(path) if path is not None else None
        _data = None


def _load() -> dict[str, Any]:
    global _data
    with _lock:
        if _data is not None:
            return _data
        data = _empty()
        try:
            path = _path()
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for key, default in data.items():
                        value = loaded.get(key)
                        if isinstance(value, type(default)):
                            data[key] = value
            if sanitize_store(data):
                logger.info("learned vocabulary: removed entries that failed validation")
                _save(data)
        except Exception as exc:
            logger.warning("learned vocabulary unreadable (%s); starting empty", exc)
        _data = data
        return data


def _save(data: dict[str, Any]) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("could not persist learned vocabulary (%s)", exc)


def _voter(user_id: Any) -> str:
    """A stable hash of the user id (never the id or an e-mail itself)."""
    salt = str(getattr(settings, "SECRET_KEY", "") or "")
    return hashlib.sha256(f"{salt}:{user_id}".encode("utf-8")).hexdigest()[:16]


def is_command_only(phrase: str) -> bool:
    """True when a role phrase is made only of command words ("publish it", "go live")."""
    words = re.findall(r"[\w']+", (phrase or "").lower())
    return not words or all(w in _COMMAND_WORDS for w in words)


def _known_product(name: str) -> bool:
    """A canonical product the catalogue or the lexicon already knows (so a synonym cannot invent one)."""
    key = " ".join((name or "").lower().split())
    if not key:
        return False
    try:
        from services import ai_lexicon, product_synonyms

        if key in product_synonyms._GROUPS or key in product_synonyms.ALIASES.values() \
                or key in product_synonyms.PHRASES.values():
            return True
        return ai_lexicon.term(key) is not None
    except Exception:
        return False


def _put(mapping: dict[str, Any], key: str, value: Any) -> bool:
    if key not in mapping and len(mapping) >= _MAX_ENTRIES:
        return False
    if mapping.get(key) == value:
        return False
    mapping[key] = value
    return True


def _contradicts_seed_role(evidence: str, role: Any) -> bool:
    """The seed rules read this evidence as the OTHER side ("i sell" cannot mean buyer)."""
    try:
        from services import query_extractor

        seed = query_extractor._extract_role(evidence)
        return seed is not None and role is not None and seed != role
    except Exception:
        return False


def learn_from(parse: Any, message: str, user_id: Any = None) -> None:
    """Propose the grounded fields of ``parse`` as vocabulary. Never raises.

    The entries are quarantined until ``PROMOTE_MIN_USERS`` distinct users produced them. Without a ``user_id``
    nothing is recorded (the caller cannot say who wrote the message, so it cannot count as a vote)."""
    try:
        _learn(parse, message, user_id)
    except Exception as exc:
        logger.debug("learning skipped (%s)", exc)


def _propose(data: dict[str, Any], kind: str, key: str, value: Any, user_id: Any) -> bool:
    """Record that ``user_id`` produced ``kind[key] = value``; apply it once enough distinct users agree.

    ``user_id`` None means unknown: the mapping is not recorded and can never be promoted. Returns True when the
    store changed."""
    if user_id is None or not key:
        return False
    target = data.setdefault(kind, {})
    if isinstance(target, list):
        if key in target:
            return False
    elif target.get(key) == value:
        return False
    cands = data.setdefault("candidates", {}).setdefault(kind, {})
    slot = f"{key}\t{value}"
    voters = cands.get(slot)
    if voters is None:
        if len(cands) >= _MAX_CANDIDATES:
            return False
        voters = cands[slot] = []
    who = _voter(user_id)
    if who not in voters:
        voters.append(who)
    if len(voters) < PROMOTE_MIN_USERS:
        return True  # the candidate list changed, nothing is active yet
    del cands[slot]
    if isinstance(target, list):
        if len(target) < _MAX_ENTRIES:
            target.append(key)
        return True
    _put(target, key, value)
    return True


def _learn(parse: Any, message: str, user_id: Any = None) -> None:
    changed = False
    with _lock:
        data = _load()

        product = (parse.product or "").strip().lower()
        if product and parse.category:
            for name in {product, (parse.product_as_written or "").strip().lower()}:
                if name and valid_product_term(name):
                    changed |= _propose(data, "product_category", name, parse.category, user_id)
            canonical = {c.lower() for c in CANONICAL_CATEGORIES}
            if parse.category.lower() not in canonical and parse.category not in data["categories"]:
                changed |= _propose(data, "categories", parse.category, True, user_id)

        written = (parse.product_as_written or "").strip().lower()
        if "product_synonyms" not in data:
            data["product_synonyms"] = {}
        # Only onto a product that is already known: a synonym cannot invent the product it points at.
        if product and written and product != written and len(written.split()) <= 4 and valid_product_term(written) \
                and _known_product(product):
            changed |= _propose(data, "product_synonyms", written, product, user_id)

        evidence = (parse.role_evidence or "").strip().lower()
        if parse.role is not None and evidence and 2 <= len(evidence.split()) <= 6 \
                and not re.search(r"\d", evidence) and ".." not in evidence and "…" not in evidence \
                and valid_role_phrase(evidence) and not _contradicts_seed_role(evidence, parse.role):
            changed |= _propose(data, "role_phrases", evidence, parse.role.value, user_id)

        # A property value is remembered only for the product family it was seen in, and only when it really is a
        # property (not a number, a unit, a currency, a place or a stop word). It is later offered to the model as a
        # hint for that family; it never writes a field by itself.
        scope = scope_of(parse.category)
        for key, value in (parse.attributes or {}).items():
            if scope != "*" and isinstance(value, str) and 0 < len(value.split()) <= 3 and valid_attribute_value(key, value):
                changed |= _propose(data, "attribute_values", f"{scope}::{value.strip().lower()}", key, user_id)

        unit = (parse.quantity_unit or "").strip().lower()
        if unit and len(unit) <= 20 and is_unit_word(unit):
            changed |= _propose(data, "units", unit, True, user_id)

        for term in parse.unknown_terms or []:
            term = term.strip().lower()
            if term and len(term) <= 40 and valid_product_term(term):
                if term in data["unknown_terms"] or len(data["unknown_terms"]) < _MAX_ENTRIES:
                    data["unknown_terms"][term] = data["unknown_terms"].get(term, 0) + 1
                    changed = True
        if changed:
            _save(data)


def _find_longest(mapping: dict[str, Any], text: str) -> Optional[Any]:
    lowered = " ".join((text or "").lower().replace("’", "'").split())
    if not lowered:
        return None
    for key in sorted((k for k in mapping if k in lowered), key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(key)}(?:s|es)?(?!\w)", lowered):
            return mapping[key]
    return None


def category_for(text: str) -> Optional[str]:
    """Category of the longest learned product mentioned in ``text``."""
    return _find_longest(_load()["product_category"], text)


def role_for(text: str) -> Optional[RFQRole]:
    phrases = {k: v for k, v in _load()["role_phrases"].items() if not is_command_only(k)}
    value = _find_longest(phrases, text)
    try:
        return RFQRole(value) if value else None
    except ValueError:
        return None


def learned_attribute_values(category: Optional[str] = None) -> dict[str, str]:
    """value -> property key remembered for ``category`` only.

    These are HINTS for the model and the grounding step, never rules that write a field: without a category the
    result is empty."""
    scope = scope_of(category)
    if scope == "*":
        return {}
    out: dict[str, str] = {}
    for raw_key, prop in _load()["attribute_values"].items():
        entry_scope, value = _split_scope(raw_key)
        if entry_scope == scope:
            out[value] = prop
    return out


def attribute_hints(category: Optional[str]) -> dict[str, list[str]]:
    """property key -> remembered values for this product family, for the model prompt."""
    hints: dict[str, list[str]] = {}
    for value, prop in learned_attribute_values(category).items():
        hints.setdefault(prop, []).append(value)
    return {k: sorted(v)[:8] for k, v in sorted(hints.items())[:12]}


def known_units() -> list[str]:
    return list(_load()["units"])


def known_categories() -> list[str]:
    seen: dict[str, str] = {}
    for name in [*CANONICAL_CATEGORIES, *_load()["categories"]]:
        seen.setdefault(name.lower(), name)
    return list(seen.values())


def canonical_product_for(term: str) -> Optional[str]:
    """Find learned canonical product for a regional / slang / raw term."""
    syns = _load().get("product_synonyms", {})
    term_lower = (term or "").strip().lower()
    if term_lower in syns:
        return syns[term_lower]
    return _find_longest(syns, term_lower)


def learned_product_synonyms() -> dict[str, str]:
    return dict(_load().get("product_synonyms", {}))


def learn_product_synonym(
    raw_term: str, canonical_product: str, user_id: Any = None, *, trusted: bool = False
) -> None:
    """Record that raw_term means canonical_product.

    From a chat user (``user_id``) it is a quarantined candidate, only onto a product that is already known.
    ``trusted=True`` (an admin or a seed script) applies it directly."""
    raw, canon = raw_term.strip().lower(), canonical_product.strip().lower()
    with _lock:
        data = _load()
        if "product_synonyms" not in data:
            data["product_synonyms"] = {}
        if trusted:
            if _put(data["product_synonyms"], raw, canon):
                _save(data)
        elif _known_product(canon) and _propose(data, "product_synonyms", raw, canon, user_id):
            _save(data)

"""Grounding checks for model-proposed message understanding.

The model is allowed to propose anything (open vocabulary), but it may never put
a value into the result that the user's message does not support. Every field is
validated against the message text here, in plain code, so a hallucinated
quantity, role or product is dropped instead of reaching a search or an RFQ.
"""

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from pydantic import BaseModel, Field

from models.enums import RFQRole
from services import categories, unit_converter
from services.locations import find_location
from services.match_scoring import singularise

logger = logging.getLogger(__name__)

try:  # ISO 4217 registry instead of a hand-kept currency list.
    import pycountry
except Exception:  # pragma: no cover - optional dependency
    pycountry = None
    logger.warning(
        "pycountry is not installed: currency codes are only shape-checked (3 letters), "
        "not validated against ISO 4217. Install pycountry for strict validation."
    )


class AIParse(BaseModel):
    """A grounded model parse. Every populated field has passed validation."""

    role: Optional[RFQRole] = None
    role_evidence: Optional[str] = None
    intent: Optional[str] = None
    product: Optional[str] = None
    product_as_written: Optional[str] = None
    category: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    negated_attributes: list[str] = Field(default_factory=list)
    quantity_value: Optional[Decimal] = None
    quantity_unit: Optional[str] = None
    price_amount: Optional[Decimal] = None
    price_currency: Optional[str] = None
    price_per_unit: Optional[str] = None
    location: Optional[str] = None
    deadline_days: Optional[int] = None
    unknown_terms: list[str] = Field(default_factory=list)


_EMPTY_STRINGS = frozenset({"", "null", "none", "n/a", "na", "unknown", "nil", "undefined"})


def normalise_text(text: str) -> str:
    """Lowercase, unify apostrophes and collapse whitespace."""
    cleaned = str(text or "").replace("’", "'").replace("‘", "'")
    return " ".join(cleaned.lower().split())


_ELLIPSIS_RE = re.compile(r"\.{3,}|…|\s\.\.\s")


def _evidence_grounded(evidence: str, lowered_message: str) -> bool:
    """True when every fragment of ``evidence`` (split on ellipses) appears in the message, in order."""
    fragments = [f.strip(" .,;:-") for f in _ELLIPSIS_RE.split(normalise_text(evidence))]
    fragments = [f for f in fragments if len(f) >= 2]
    if not fragments:
        return False
    pos = 0
    for frag in fragments:
        idx = lowered_message.find(frag, pos)
        if idx < 0:
            return False
        pos = idx + len(frag)
    return True


def _clean_str(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = " ".join(str(value).split())
    return None if text.lower() in _EMPTY_STRINGS else text


# --- numbers -----------------------------------------------------------------

# Words that legitimately turn "5" into 5000. These explain a number; they are
# not a vocabulary the parser depends on.
_MULTIPLIER_WORDS: dict[str, Decimal] = {
    "k": Decimal(1000), "thousand": Decimal(1000), "m": Decimal(1_000_000),
    "mn": Decimal(1_000_000), "million": Decimal(1_000_000), "lakh": Decimal(100_000),
    "lakhs": Decimal(100_000), "lac": Decimal(100_000), "lacs": Decimal(100_000),
    "crore": Decimal(10_000_000), "crores": Decimal(10_000_000), "cr": Decimal(10_000_000),
    "dozen": Decimal(12), "dozens": Decimal(12), "doz": Decimal(12),
    "hundred": Decimal(100), "gross": Decimal(144),
}
_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "thousand": 1000, "million": 1_000_000,
}
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _to_decimal(value: Any) -> Optional[Decimal]:
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _number_grounded(value: Any, message: str) -> bool:
    """True when the message supports ``value``: literal digits, a multiplier
    suffix/word ("5k", "2 lakh", "3 dozen") or spelled-out number words."""
    target = _to_decimal(value)
    if target is None:
        return False
    lowered = normalise_text(message)
    numbers = {n for n in (_to_decimal(m.group()) for m in _NUM_RE.finditer(lowered)) if n is not None}
    if target in numbers:
        return True
    words = set(re.findall(r"[a-z]+", lowered))
    multipliers = [mult for word, mult in _MULTIPLIER_WORDS.items() if word in words]
    # "5k" / "2.5m": the suffix is glued to the digits, so it is not a word above.
    for match in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(k|m|mn|cr)\b", lowered):
        mult = _MULTIPLIER_WORDS[match.group(2)]
        base = _to_decimal(match.group(1))
        if base is not None and base * mult == target:
            return True
    for number in numbers:
        if any(number * mult == target for mult in multipliers):
            return True
    spelled = {Decimal(v) for w, v in _NUMBER_WORDS.items() if w in words}
    if target in spelled:
        return True
    # "two hundred", "five thousand"
    for small_word, small in _NUMBER_WORDS.items():
        for big_word in ("hundred", "thousand", "million"):
            if small_word in words and big_word in words and small < 100 and Decimal(small * _NUMBER_WORDS[big_word]) == target:
                return True
    return False


# --- individual validators ---------------------------------------------------

def valid_currency(code: Any) -> Optional[str]:
    cleaned = _clean_str(code)
    if not cleaned:
        return None
    upper = cleaned.upper()
    if not re.fullmatch(r"[A-Z]{3}", upper):
        return None
    if pycountry is not None:
        return upper if pycountry.currencies.get(alpha_3=upper) is not None else None
    return upper


def _tokens(text: str) -> set[str]:
    return {singularise(t) for t in re.findall(r"\w+", normalise_text(text)) if len(t) > 1}


def _product_grounded(product: str, as_written: Optional[str], message: str) -> bool:
    if any(ord(ch) > 127 for ch in message):
        return True  # another script: the English name is a translation
    lowered = normalise_text(message)
    written = normalise_text(as_written or product)
    if written and written in lowered:
        return True
    msg_tokens = _tokens(message)
    return bool((_tokens(as_written or "") | _tokens(product)) & msg_tokens)


def _attribute_key(key: Any) -> Optional[str]:
    cleaned = _clean_str(key)
    if not cleaned:
        return None
    return re.sub(r"[^\w]+", "_", cleaned.lower()).strip("_") or None


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def ground(raw: Any, message: str) -> AIParse:
    """Validate a raw model dict against ``message`` and keep only supported fields."""
    parse = AIParse()
    if not isinstance(raw, dict):
        return parse
    lowered = normalise_text(message)

    role = _clean_str(raw.get("role"))
    evidence = _clean_str(raw.get("role_evidence"))
    if role and role.lower() in ("buyer", "seller") and evidence and _evidence_grounded(evidence, lowered):
        parse.role = RFQRole.BUYER if role.lower() == "buyer" else RFQRole.SELLER
        parse.role_evidence = normalise_text(evidence)

    parse.intent = _clean_str(raw.get("intent"))

    product = _clean_str(raw.get("product"))
    as_written = _clean_str(raw.get("product_as_written"))
    if product and _product_grounded(product, as_written, message):
        parse.product = product
        parse.product_as_written = as_written

    category = _clean_str(raw.get("category"))
    if category:
        parse.category = categories.normalize_category(category)

    attributes: dict[str, Any] = {}
    raw_attrs = raw.get("attributes")
    if isinstance(raw_attrs, dict):
        for key, value in raw_attrs.items():
            norm_key = _attribute_key(key)
            if not norm_key:
                continue
            if isinstance(value, bool):
                attributes[norm_key] = value
            elif isinstance(value, (int, float)):
                attributes[norm_key] = value
            elif isinstance(value, list):
                items = [v for v in (_clean_str(x) for x in value) if v]
                if items:
                    attributes[norm_key] = items
            else:
                text = _clean_str(value)
                if text:
                    attributes[norm_key] = text
    parse.attributes = attributes

    negated = raw.get("negated_attributes")
    if isinstance(negated, list):
        parse.negated_attributes = [
            n for n in (_attribute_key(x) for x in negated) if n
        ]

    qty = _to_decimal(raw.get("quantity_value"))
    if qty is not None and _number_grounded(qty, message):
        parse.quantity_value = qty
        unit = _clean_str(raw.get("quantity_unit"))
        if unit:
            parse.quantity_unit = unit_converter.clean_unit(unit) or unit.lower()

    price = _to_decimal(raw.get("price_amount"))
    if price is not None and _number_grounded(price, message):
        parse.price_amount = price
        parse.price_currency = valid_currency(raw.get("price_currency"))
        per_unit = _clean_str(raw.get("price_per_unit"))
        if per_unit:
            parse.price_per_unit = unit_converter.clean_unit(per_unit) or per_unit.lower()

    location = _clean_str(raw.get("location"))
    if location and (normalise_text(location) in lowered or any(ord(c) > 127 for c in message)
                     or find_location(location)):
        parse.location = location

    days = _as_float(raw.get("deadline_days"))
    if days is not None and 0 < days <= 3650 and (
        _number_grounded(days, message) or re.search(r"\b(tomorrow|today|week|month|asap|urgent)", lowered)
        or any(ord(c) > 127 for c in message)
    ):
        parse.deadline_days = int(days)

    terms = raw.get("unknown_terms")
    if isinstance(terms, list):
        parse.unknown_terms = [
            t for t in (_clean_str(x) for x in terms) if t and normalise_text(t) in lowered
        ][:20]
    return parse

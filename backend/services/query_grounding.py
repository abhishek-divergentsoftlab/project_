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
    language: Optional[str] = None
    product_is_generic: Optional[bool] = None
    # The price is the order's TOTAL value ("budget 1.5 crore for 50k pcs"), not a per-unit price.
    price_is_total: bool = False
    # Every distinct goods item of the message with its own quantity, as the model read them: [{"product", "quantity_value",
    # "quantity_unit"}]. More than one means several products in one message.
    products: list[dict[str, Any]] = Field(default_factory=list)


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



# --- price evidence ------------------------------------------------------------

_CURRENCY_WORD_RE = (
    r"₹|\$|€|£|¥|rs\.?|inr|usd|eur|gbp|aed|jpy|cny|rmb|cad|aud|sgd|chf|rupees?|rupaye|rupaya|rup+e+s*|dollars?|dol+ars?|euros?|"
    r"pounds?|dirhams?|yen|yuan|bucks?|रुपये|रुपया|/-"
)
_PRICE_CUE_RE = re.compile(
    r"\b(?:price|priced|rate|rates|budget|cost|costs|pay|paying|target|offer|offering|bhav|bhaav|daam|dam|quote|quoting|"
    r"worth|valued|upto|up\s+to|max|maximum|ceiling|asking|expect|expecting|per\s+unit|each)\b|@|भाव|दाम|कीमत|रेट",
    re.IGNORECASE,
)
_PER_AFTER_RE = re.compile(r"^\s*(?:/|per\b|par\b|pr\b|a\b|each\b|apiece\b|प्रति)", re.IGNORECASE)
_TOTAL_CUE_RE = re.compile(
    r"\b(?:total|overall|altogether|in\s+all|lump\s*sum|lumpsum|all\s+in|whole|entire|kul|poora|pura|sab\s+mila\s+ke)\b",
    re.IGNORECASE,
)
_MULT_SUFFIX = r"(?:\s*(?:k|m|mn|lakhs?|lacs?|crores?|cr|thousand|million|hundred))?"
_ANY_NUMBER_RE = re.compile(rf"(?<![\w.])\d[\d,]*(?:\.\d+)?{_MULT_SUFFIX}\b", re.IGNORECASE)
_ATTACHED_NUMBER_RE = re.compile(rf"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?={_CURRENCY_WORD_RE}|[a-zA-Z])", re.IGNORECASE)


def _value_of_span(span: str) -> Optional[Decimal]:
    m = re.match(r"(\d[\d,]*(?:\.\d+)?)\s*([a-z]*)", span.strip().lower())
    if not m:
        return None
    base = _to_decimal(m.group(1))
    if base is None:
        return None
    mult = _MULTIPLIER_WORDS.get(m.group(2)) if m.group(2) else None
    return base * mult if mult else base


def number_spans(message: str, value: Any) -> list[tuple[int, int]]:
    """Character spans of the message's numbers that read as ``value`` (digits, "5k", "1.5 crore")."""
    target = _to_decimal(value)
    spans: list[tuple[int, int]] = []
    if target is None:
        return spans
    text = normalise_text(message)
    for m in _ANY_NUMBER_RE.finditer(text):
        got = _value_of_span(m.group(0))
        if got is not None and got == target:
            spans.append((m.start(), m.end()))
    if not spans:
        for m in _ATTACHED_NUMBER_RE.finditer(text):
            base = _to_decimal(m.group(1))
            if base is not None and base == target:
                spans.append((m.start(1), m.end(1)))
    return spans


def price_supported(message: str, value: Any) -> bool:
    """True when a span of the message supports ``value`` AS A PRICE: a currency next to it, a "per <unit>" / "/kg"
    after it, or a price word (price, rate, budget, pay ...) in front of it. A bare number, or a number that is
    followed by an order unit ("5 ton", "10 pcs"), is a quantity or a spec, never a price."""
    text = normalise_text(message)
    spans = number_spans(message, value)
    if not spans:
        # spelled-out numbers ("twelve rupees"): only a currency or price word anywhere supports it
        return bool(re.search(_CURRENCY_WORD_RE, text) or _PRICE_CUE_RE.search(text))
    unit_after = re.compile(
        r"^\s*(?:[a-z]+\.?)\b", re.IGNORECASE
    )
    for start, end in spans:
        before, after = text[max(0, start - 22):start], text[end:end + 14]
        if re.search(rf"(?:{_CURRENCY_WORD_RE})\s*$", before.rstrip() + " ", re.IGNORECASE) and re.search(rf"(?:{_CURRENCY_WORD_RE})\s*$", before, re.IGNORECASE):
            return True
        if re.match(rf"\s*(?:{_CURRENCY_WORD_RE})", after, re.IGNORECASE):
            return True
        if _PER_AFTER_RE.match(after):
            return True
        if re.search(r"\b(?:at|for)\s+(?:of\s+)?$", before) and re.match(r"\s*(?:[,.;!?]|$)", after) \
                and len(_ANY_NUMBER_RE.findall(text)) >= 2:
            return True  # "10 ton at 15": the second figure after at/for is the rate
        if _PRICE_CUE_RE.search(before[-18:]):
            # "price 15", "budget 1.5 crore": but a unit right after ("rate 5 ton") makes it a quantity again
            m_unit = unit_after.match(after)
            word = (m_unit.group(0).strip(" .") if m_unit else "")
            from services import unit_converter

            if not (word and (unit_converter.base_unit(word) is not None or unit_converter.is_packaging_unit(word))):
                return True
    return False


def states_total_price(message: str) -> bool:
    """The message calls its amount a total ("total budget", "overall", "kul")."""
    return bool(_TOTAL_CUE_RE.search(message or ""))


_ATTR_SKIP_KEYS = frozenset({
    "quantity", "quantity_unit", "unit", "price", "price_currency", "currency", "price_per_unit", "amount", "location",
    "city", "state", "country", "deadline", "role", "name", "product", "category", "min_quantity", "max_quantity",
    "moq", "hsn", "hsn_code", "capacity_unit",
})


def attribute_supported(value: Any, message: str) -> bool:
    """A property value the message supports: its words (or digits) appear in the text, in any script."""
    if has_indic_script(message):
        return True
    lowered = normalise_text(message)
    if isinstance(value, bool):
        return True  # a flag the model derived from a phrase; the key was checked separately
    if isinstance(value, (int, float)):
        return _number_grounded(value, message)
    items = value if isinstance(value, list) else [value]
    for item in items:
        text = normalise_text(str(item))
        if not text:
            return False
        if text in lowered:
            continue
        tokens = [t for t in re.findall(r"[^\W_]+", text) if len(t) > 1]
        if not tokens:
            return False
        stems = {singularise(t) for t in re.findall(r"[^\W_]+", lowered)}
        if not all(singularise(t) in stems or t in lowered for t in tokens):
            return False
    return True


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


# No real order is above a billion base units (kg, pieces, litres, metres): "99999999999999 kg" is a typo or a probe.
MAX_SANE_BASE_QUANTITY = Decimal("1e9")


def quantity_is_absurd(value: Any, unit: Optional[str] = None) -> bool:
    """True when ``value`` of ``unit`` is above ``MAX_SANE_BASE_QUANTITY`` in base units."""
    number = _to_decimal(value)
    if number is None:
        return False
    base = unit_converter.base_unit(unit)
    if base is not None:
        number = number * base[0]
    return number > MAX_SANE_BASE_QUANTITY


def has_indic_script(text: str) -> bool:
    """True when ``text`` has letters of an Indic script (Devanagari ... Malayalam, U+0900-U+0DFF).

    Only then is the model's English name a translation that cannot be found literally in the message. A rupee
    sign, an emoji or a curly quote is non-ASCII too, but says nothing about the language."""
    return any(0x0900 <= ord(ch) <= 0x0DFF and ch.isalpha() for ch in text or "")


def _tokens(text: str) -> set[str]:
    return {singularise(t) for t in re.findall(r"\w+", normalise_text(text)) if len(t) > 1}


def _product_grounded(product: str, as_written: Optional[str], message: str) -> bool:
    if has_indic_script(message):
        return True  # another script: the English name is a translation
    lowered = normalise_text(message)
    written = normalise_text(as_written or product)
    if written and written in lowered:
        return True
    msg_tokens = _tokens(message)
    if (_tokens(as_written or "") | _tokens(product)) & msg_tokens:
        return True
    # A known local word for the product ("haldi" for "turmeric") grounds it too.
    from services import product_synonyms

    canon_tokens = _tokens(product_synonyms.canonicalise_text(message))
    product_tokens = _tokens(product)
    if product_tokens and product_tokens <= canon_tokens:
        return True
    return bool({v for t in [product, *product.split()] for v in product_synonyms.variants(t)} & (msg_tokens | canon_tokens))


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


_BRACKETED_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def _sane_product(product: Optional[str]) -> Optional[str]:
    """Drop bracketed notes and reject what is not a product name. Models sometimes
    explain themselves inside the field ("ulli (likely a typo for ...)")."""
    if not product:
        return None
    product = " ".join(_BRACKETED_RE.sub(" ", product).split())
    if not product or len(product) > 60 or len(product.split()) > 6:
        return None
    return product


_EVIDENCE_FUNCTION_WORDS = frozenset({
    "for", "about", "of", "on", "in", "at", "and", "or", "the", "a", "an", "to", "with", "what", "how", "is", "are", "its",
    "it", "this", "that", "these", "those", "tell", "me", "please", "also", "then", "so", "any", "some",
})


def _evidence_is_not_a_wish(evidence: str, raw: dict[str, Any]) -> bool:
    """Role evidence must show the user wants to buy or sell. A question mark, or words that are only the product
    ("for paracetamol tablets?"), show a question about goods: no role."""
    text = normalise_text(evidence)
    if text.endswith("?"):
        return True
    words = [w for w in re.findall(r"[^\W\d_]+", text) if w not in _EVIDENCE_FUNCTION_WORDS]
    if not words:
        return True
    product_words = {singularise(w) for field in ("product", "product_as_written")
                     for w in re.findall(r"[^\W\d_]+", normalise_text(str(raw.get(field) or "")))}
    return bool(product_words) and all(singularise(w) in product_words for w in words)


def ground(raw: Any, message: str) -> AIParse:
    """Validate a raw model dict against ``message`` and keep only supported fields."""
    parse = AIParse()
    if not isinstance(raw, dict):
        return parse
    lowered = normalise_text(message)

    role = _clean_str(raw.get("role"))
    evidence = _clean_str(raw.get("role_evidence"))
    if role and role.lower() in ("buyer", "seller") and evidence and _evidence_grounded(evidence, lowered) \
            and not _evidence_is_not_a_wish(evidence, raw):
        parse.role = RFQRole.BUYER if role.lower() == "buyer" else RFQRole.SELLER
        parse.role_evidence = normalise_text(evidence)

    parse.intent = _clean_str(raw.get("intent"))

    language = _clean_str(raw.get("language"))
    if language and len(language) <= 20:
        parse.language = language

    product = _sane_product(_clean_str(raw.get("product")))
    as_written = _clean_str(raw.get("product_as_written"))
    if product and _product_grounded(product, as_written, message):
        parse.product = product
        parse.product_as_written = as_written
        if isinstance(raw.get("product_is_generic"), bool):
            parse.product_is_generic = raw["product_is_generic"]

    category = _clean_str(raw.get("category"))
    if category:
        parse.category = categories.normalize_category(category)

    attributes: dict[str, Any] = {}
    raw_attrs = raw.get("attributes")
    if isinstance(raw_attrs, dict):
        for key, value in raw_attrs.items():
            norm_key = _attribute_key(key)
            if not norm_key or norm_key in _ATTR_SKIP_KEYS:
                continue
            if not isinstance(value, bool) and not attribute_supported(value, message):
                continue  # a property the message does not say is the model's guess
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
            n for n in (_attribute_key(x) for x in negated)
            if n and all(t in lowered or singularise(t) in _tokens(message) for t in n.split("_") if len(t) > 1)
        ]

    qty = _to_decimal(raw.get("quantity_value"))
    if qty is not None and _number_grounded(qty, message) and not quantity_is_absurd(qty, _clean_str(raw.get("quantity_unit"))):
        parse.quantity_value = qty
        unit = _clean_str(raw.get("quantity_unit"))
        if unit:
            parse.quantity_unit = unit_converter.clean_unit(unit) or unit.lower()

    price = _to_decimal(raw.get("price_amount"))
    if price is not None and _number_grounded(price, message) and price_supported(message, price):
        parse.price_amount = price
        # the model reads whether the amount is a lump sum (any phrasing); a stated "per <unit>" settles it the other way
        parse.price_is_total = bool(raw.get("price_is_total")) and not _clean_str(raw.get("price_per_unit"))
        parse.price_currency = valid_currency(raw.get("price_currency"))
        per_unit = _clean_str(raw.get("price_per_unit"))
        if per_unit:
            parse.price_per_unit = unit_converter.clean_unit(per_unit) or per_unit.lower()

    location = _clean_str(raw.get("location"))
    if location and (normalise_text(location) in lowered or has_indic_script(message)
                     or find_location(location)):
        parse.location = location

    days = _as_float(raw.get("deadline_days"))
    if days is not None and 0 < days <= 3650 and (
        _number_grounded(days, message) or re.search(r"\b(tomorrow|today|week|month|asap|urgent|kal|parso|hafte|hafta|mahin[ae]|turant|jaldi)", lowered)
        or has_indic_script(message)
    ):
        parse.deadline_days = int(days)

    items = raw.get("products")
    if isinstance(items, list):
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            name = _sane_product(_clean_str(item.get("product")))
            if not name or not _product_grounded(name, None, message):
                continue
            qty = _to_decimal(item.get("quantity_value"))
            if qty is not None and not _number_grounded(qty, message):
                qty = None
            unit = _clean_str(item.get("quantity_unit"))
            parse.products.append({
                "product": name, "quantity_value": str(qty) if qty is not None else None,
                "quantity_unit": (unit_converter.clean_unit(unit) or unit.lower()) if unit else None,
            })

    terms = raw.get("unknown_terms")
    if isinstance(terms, list):
        parse.unknown_terms = [
            t for t in (_clean_str(x) for x in terms) if t and normalise_text(t) in lowered
        ][:20]
    return parse

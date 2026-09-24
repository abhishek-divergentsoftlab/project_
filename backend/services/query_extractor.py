"""Turn a natural-language search message into structured requirements.

This is deliberately a parser rather than a model call. Local models were probed
first (llama3.1:8b, gpt-oss:20b, qwen3:32b) and all three missed the price in
"at price 2 dollar per unit", two of them mis-parsed quantity, and one read
"i can supply" as a buy intent -- while taking 7-21 seconds. Numbers, units,
currencies, dates and known city names are exactly what a parser does with
perfect precision and no latency.

``services/llm_extractor.py`` layers a model on top to fill what this cannot
reach, and is opt-in for that reason.
"""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from pydantic import BaseModel, Field

from models.enums import RFQRole
from services import currency as currency_service, unit_converter
from services.locations import City, Location, find_city, find_location
from services.match_scoring import singularise

# --- vocabulary --------------------------------------------------------------

_SELL_CUES = (
    "i can supply", "i can sell", "i sell", "i am selling", "i'm selling",
    "we supply", "we sell", "we are selling", "i have stock", "i have",
    "supplying", "offering", "for sale", "i can provide", "i can offer",
)
_BUY_CUES = (
    "i want", "i need", "looking for", "i am looking", "i'm looking",
    "need to buy", "want to buy", "i buy", "requirement of", "show me", "find",
)

_CURRENCIES: dict[str, str] = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR",
    "rupee": "INR", "rupees": "INR", "ruppes": "INR", "rupes": "INR",
    "rupess": "INR", "ruppees": "INR", "rupe": "INR",
    "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD",
    "buck": "USD", "bucks": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
}

_UNITS = (
    "pcs", "pieces", "piece", "units", "unit", "nos", "no",
    "kg", "kgs", "kilogram", "kilograms", "tonne", "tonnes", "ton", "tons", "mt",
    "quintal", "quintals", "qtl",
    "meter", "meters", "metre", "metres", "m",
    "box", "boxes", "carton", "cartons", "roll", "rolls", "set", "sets",
    # Trade packaging units. Without "pallet", "20 pallets with 1000 kg
    # capacity" was read as an order for 1,000 kg.
    "pallet", "pallets", "bag", "bags", "drum", "drums", "bale", "bales",
    "container", "containers", "reel", "reels", "pack", "packs", "crate",
    "crates", "sack", "sacks", "spool", "spools", "sheet", "sheets",
    "bora", "boras", "bori", "boris", "katta", "kattas",
    "litre", "litres", "liter", "liters", "l",
    "lb", "lbs", "pound", "pounds",
)

# Units that only ever count things. A leftover "pcs"/"pcses"/"kg" is noise in a
# product name, whereas "box", "carton" and "roll" are products in their own
# right and must survive.
_COUNT_UNITS = frozenset(
    """pc pcs piece pieces unit units no nos each kg kgs kilogram kilograms
    tonne tonnes ton tons mt m meter meters metre metres l litre litres liter
    liters quintal quintals qtl lb lbs bora boras bori boris katta kattas""".split()
)

_COLORS = (
    "white", "black", "red", "blue", "green", "grey", "gray", "brown", "navy",
    "indigo", "silver", "golden", "gold", "yellow", "orange", "pink", "purple",
    "transparent", "beige", "maroon", "walnut",
)

# Words that describe the product and belong in attributes rather than its name.
_QUALITIES = (
    "organic", "printed", "unprinted", "braided", "polished", "stretch",
    "waterproof", "uv treated", "food grade", "refurbished", "reusable",
)
_MATERIALS = (
    "cotton", "denim", "leather", "leatherette", "mesh", "wood", "wooden",
    "sheesham", "mango wood", "steel", "aluminium", "plastic", "glass",
    "silicone", "rubber", "paper", "jute",
)

# Keyword -> category, so the category dimension can score even when the user
# never names a category.
_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "Electronics": (
        "cable", "cables", "hdmi", "charger", "chargers", "usb", "type-c", "type c",
        "adapter", "adapters", "power bank", "electronic", "electronics", "gan",
        "battery", "batteries", "led", "cell", "cells", "lithium", "wire", "wires",
        "laptop", "phone", "circuit", "display",
    ),
    "Packaging": (
        "box", "boxes", "carton", "cartons", "corrugated", "fibc", "bulk bag",
        "packaging", "bubble wrap", "pouch", "pouches", "packing", "tape", "container",
        "mailer", "bag", "bags", "sack", "sacks",
    ),
    "Furniture": (
        "table", "tables", "chair", "chairs", "sofa", "sofas", "desk", "desks",
        "furniture", "cabinet", "cabinets", "wardrobe", "bed", "beds", "shelf",
        "shelves", "seating", "stool", "bench",
    ),
    "Agriculture": (
        "apple", "apples", "rice", "wheat", "grain", "grains", "fruit", "fruits",
        "vegetable", "vegetables", "vagitable", "vagitables", "vegitable", "vegitables",
        "veggie", "veggies", "agro", "basmati", "onion", "onions", "potato", "potatoes",
        "tomato", "tomatos", "tomatoes", "chili", "chilies", "spice", "spices", "crop",
        "crops", "produce", "corn", "maize", "pulses", "pulse", "beans", "lentils",
        "garlic", "ginger", "mango", "mangoes", "banana", "sugar", "tea", "coffee",
    ),
    "Textiles": (
        "shirt", "shirts", "fabric", "fabrics", "denim", "cotton", "textile", "textiles",
        "garment", "garments", "t-shirt", "t-shirts", "trouser", "trousers", "saree",
        "sarees", "yarn", "cloth", "apparel", "linen", "silk", "wool", "polyester",
    ),
    "Industrial": (
        "steel", "sheet", "sheets", "pipe", "pipes", "tube", "tubes", "valve", "valves",
        "pump", "pumps", "metal", "metals", "industrial", "bearing", "bearings",
        "fastener", "fasteners", "copper", "iron", "aluminium", "aluminum",
    ),
}

_BROAD_PRODUCTS: frozenset[str] = frozenset(
    """vegetable vegetables vagitable vagitables vegitable vegitables veggie veggies
    fruit fruits grain grains crop crops produce agro agriculture spice spices
    electronic electronics cable cables wire wires device devices gadget gadgets
    furniture seating packaging box boxes bag bags container containers
    textile textiles fabric fabrics cloth clothes clothing garment garments
    metal metals industrial tool tools""".split()
)

_CATEGORY_EXAMPLES: dict[str, str] = {
    "Agriculture": "tomatoes, onions, potatoes, apples, or basmati rice",
    "Electronics": "USB Type-C cables, HDMI cables, power banks, or GaN chargers",
    "Packaging": "corrugated boxes, bubble mailers, or FIBC bulk bags",
    "Furniture": "office desks, ergonomic chairs, or wooden tables",
    "Textiles": "cotton denim fabrics, cotton t-shirts, or yarn",
    "Industrial": "stainless steel sheets, industrial valves, or copper pipes",
}


def is_broad_product(product: Optional[str]) -> bool:
    if not product:
        return False
    words = product.lower().strip().split()
    return len(words) <= 2 and any(w in _BROAD_PRODUCTS for w in words)

_ORDINALS = frozenset(
    """1st 2nd 3rd 4th 5th 6th 7th 8th 9th 10th 11th 12th 13th 14th 15th
    16th 17th 18th 19th 20th 21st 22nd 23rd 24th 25th 26th 27th 28th 29th 30th 31st
    1'st 2'nd 3'rd 4'th 5'th 6'th 7'th 8'th 9'th 10'th 11'th 12'th 13'th 14'th 15'th
    16'th 17'th 18'th 19'th 20'th 21'st 22'nd 23'rd 24'th 25'th 26'th 27'th 28'th 29'th 30'th 31'st
    st nd rd th""".split()
)

# Stripped from the residual product phrase.
_FILLER = frozenset(
    """i we want need looking for buy sell selling buying supply supplying show me
    find get me some any please pls of the a an at in on to from with and or for is
    are be will would can could should must my our your it its that this these those now next
    also plus per each price prices priced pricing cost costs costing rate rates
    pay pays paying paid payment budget budgets target targets spend spending spent
    afford offer offering quote quoted quoting charge charges charged
    within before by until due deadline days
    day week weeks month months year years quantity qty around about approx nearly
    roughly upto up maximum minimum min max least most only just very really
    prefer preferably instead rather change switch convert set update make it them one give requirement
    almost exactly total overall
    actually actully actualy basically literally sorry sry apologies excuse
    ok okay okey then well alright fine search check rfq yes yeah yep sure k definitely absolutely
    dont don't not didnt didn't wont won't never no nope
    cancel remove drop delete forgetting forget
    just simple plain basic standard regular ordinary
    date dates calendar schedule
    jan jan feb mar apr jun jul aug sep sept oct nov dec
    january february march april may june july august september october november december
    st nd rd th
    rupee rupees ruppes rupes rupess ruppees rupe dollar dollars buck bucks euro euros pound pounds
    currency currencies inr usd eur gbp rs
    unit units pcs piece pieces
    color colour colours shade size under below above over between
    good best cheap cheapest quality
    contain contains cantain cantains containing containig packed packaging packing""".split()
    + list(_ORDINALS)
)

# A message made only of these is conversation, not a product. Without this,
# "hello" becomes the product and the user is told no sellers stock it.
_CHITCHAT = frozenset(
    """hi hello hey yo hiya greetings thanks thank you ok okay okey then well alright fine k sure yes yeah
    yep no nope cool nice great good morning afternoon evening bye goodbye
    test testing help what how who when where why please pls sorry sry
    actually actully nah nevermind""".split()
)

_NUMBER = r"\d[\d,]*(?:\.\d+)?"

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

# Filler words long enough that a one-character typo is worth forgiving.
_FUZZY_FILLER = frozenset(word for word in _FILLER if len(word) >= 5)


def _is_near_filler(word: str) -> bool:
    """True for a one-edit typo of a filler word, e.g. "withing" for "within".

    People type quickly in a chat box. Without this, a typo becomes a permanent
    term in the product phrase and drags down every relevance score.
    """
    if len(word) < 5:
        return False
    for filler in _FUZZY_FILLER:
        if abs(len(filler) - len(word)) > 1:
            continue
        if _edit_distance_within_one(word, filler):
            return True
    return False


def _edit_distance_within_one(left: str, right: str) -> bool:
    if left == right:
        return True
    if len(left) == len(right):
        diffs = sum(1 for a, b in zip(left, right) if a != b)
        return diffs == 1
    # One insertion or deletion: walk both, allowing a single skip.
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    i = j = 0
    skipped = False
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        j += 1
    return True
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-\.]*")


class Requirements(BaseModel):
    """Everything understood about a direct search so far.

    Persisted on the conversation and merged forward, so each message refines
    the picture rather than restarting it.
    """

    role: RFQRole = RFQRole.BUYER
    product: Optional[str] = None
    category: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    quantity_value: Optional[Decimal] = None
    quantity_unit: Optional[str] = None

    price_amount: Optional[Decimal] = None
    price_currency: Optional[str] = None
    price_per_unit: Optional[str] = None

    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    currency_hint: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None

    deadline_days: Optional[int] = None

    # Fields the user declined to give, so they are not asked again.
    skipped: list[str] = Field(default_factory=list)

    # Attributes explicitly negated or rejected by the user in refinements.
    negated_attributes: set[str] = Field(default_factory=set)

    def known(self, field: str) -> bool:
        if field == "quantity":
            return self.quantity_value is not None
        if field == "price":
            return self.price_amount is not None
        if field == "location":
            return self.city is not None
        if field == "deadline":
            return self.deadline_days is not None
        return getattr(self, field, None) is not None


def _to_decimal(raw: str) -> Optional[Decimal]:
    try:
        return Decimal(raw.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _normalise_currency(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    cleaned = token.strip().lower().rstrip(".")
    norm = currency_service.normalize_currency(cleaned)
    if norm:
        return norm
    if cleaned in _CURRENCIES:
        return _CURRENCIES[cleaned]
    return None


_CURRENCY_SYMBOLS = (
    r"₹|\$|€|£|¥|₩|c\$|a\$|rs\.?|inr|usd|eur|gbp|jpy|cny|rmb|cad|aud|aed|chf|sek|nok|dkk|krw|sgd|"
    r"(?:india[n]?\s+)?rup+[e|p]*s*|(?:u\.?s\.?\s+)?(?:dollars?|bucks?)|"
    r"euros?|pounds?|(?:swedish\s+)?krona|(?:norwegian\s+)?krone|kronor|kroner|kr|"
    r"dirhams?|yen|yuan|renminbi|won|francs?"
)



def _extract_price(text: str) -> tuple[Optional[Decimal], Optional[str], Optional[str], str]:
    """Price, currency and the unit it is quoted per; plus the text with it removed.

    Removing what it consumes is what stops "2 dollar per unit" from also being
    read as a quantity of 2.
    """
    symbols = _CURRENCY_SYMBOLS
    per = rf"(?:\s*(?:per|/|a)\s*(?P<unit>[a-z]+))?"

    patterns = (
        # "₹200/kg", "rs 200 per kg", "$2 per unit", "inr 210/unit"
        rf"(?P<cur>{symbols})\s*(?P<amt>{_NUMBER}){per}",
        # "200 rupees per kg", "2 dollar per unit", "210 ruppes per unit"
        rf"(?P<amt>{_NUMBER})\s*(?P<cur>{symbols}){per}",
        # "210/- per unit" (Indian currency notation)
        rf"(?P<amt>{_NUMBER})\s*(?P<cur>/-){per}",
        # "at price 200 per kg", "can pay 210 per unit", "budget 200/kg", "pay 210"
        rf"(?:at|for|price|rate|budget|pay|paying|target|upto|up\s+to|max|around)\s+(?:of\s+)?(?P<amt>{_NUMBER}){per}",
    )

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        amount = _to_decimal(match.group("amt"))
        if amount is None:
            continue
        cur_raw = match.groupdict().get("cur")
        currency = "INR" if cur_raw == "/-" else _normalise_currency(cur_raw)
        unit = match.groupdict().get("unit")
        if unit in ("unit", "units", "piece", "pieces", "each"):
            unit = "pcs"
        cleaned = text[: match.start()] + " " + text[match.end() :]
        return amount, currency, unit, cleaned

    return None, None, None, text


def _extract_standalone_currency(text: str) -> tuple[Optional[str], str]:
    """Find an explicit currency specification when no price amount was mentioned.

    e.g. "change currency to inr", "in rupees", "in ruppes", "use inr", "currency: inr"
    """
    pattern = (
        rf"\b(?:(?:change|switch|convert|set|use|show|target)?\s*(?:the\s+)?(?:currency\s*(?:to|in|is|:)?|in|to)\s+)?"
        rf"(?P<cur>{_CURRENCY_SYMBOLS})(?:\s+instead(?:\s+of\s+(?:{_CURRENCY_SYMBOLS}))?)?\b"
    )
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        cur_token = match.group("cur")
        normalized = _normalise_currency(cur_token)
        if normalized:
            cleaned = text[: match.start()] + " " + text[match.end() :]
            return normalized, cleaned
    return None, text


def _parse_calendar_date(
    text: str, today: Optional[date] = None
) -> tuple[Optional[date], Optional[tuple[int, int]]]:
    """An absolute date written the way people actually type one.

    Handles "30 sep", "sep 30", "12'th of november", "before november 2026",
    "2026-09-30" and "30/09/2026". A day-and-month with no year that has already
    passed is read as next year, because nobody asks for delivery in the past.
    Returns (parsed_date, (start_index, end_index)).
    """
    today = today or datetime.now().date()
    lowered = text.lower()

    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lowered)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))), iso.span()
        except ValueError:
            pass

    numeric = re.search(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b", lowered)
    if numeric:
        day, month = int(numeric.group(1)), int(numeric.group(2))
        year = int(numeric.group(3) or today.year)
        if year < 100:
            year += 2000
        try:
            parsed = date(year, month, day)
            if numeric.group(3) is None and parsed < today:
                parsed = parsed.replace(year=year + 1)
            return parsed, numeric.span()
        except ValueError:
            pass

    # Day-first: "12'th of november", "12th november 2026", "12 nov", "12 of nov"
    pat_day_first = re.search(
        rf"\b(?P<day>\d{{1,2}})\s*(?:'?(?:st|nd|rd|th))?\s*(?:of\s+)?(?P<mon>{_MONTH_ALT})(?:\s+(?P<year>20\d{{2}}))?\b",
        lowered,
    )
    if pat_day_first:
        month = _MONTHS[pat_day_first.group("mon")]
        day = int(pat_day_first.group("day"))
        year = int(pat_day_first.group("year")) if pat_day_first.group("year") else today.year
        try:
            parsed = date(year, month, day)
            if not pat_day_first.group("year") and parsed < today:
                parsed = parsed.replace(year=year + 1)
            return parsed, pat_day_first.span()
        except ValueError:
            pass

    # Month-first: "november 12", "nov 12th", "november the 12'th 2026"
    pat_mon_first = re.search(
        rf"\b(?P<mon>{_MONTH_ALT})\s+(?:the\s+)?(?P<day>\d{{1,2}})\s*(?:'?(?:st|nd|rd|th))?(?:\s+(?P<year>20\d{{2}}))?\b",
        lowered,
    )
    if pat_mon_first:
        month = _MONTHS[pat_mon_first.group("mon")]
        day = int(pat_mon_first.group("day"))
        year = int(pat_mon_first.group("year")) if pat_mon_first.group("year") else today.year
        try:
            parsed = date(year, month, day)
            if not pat_mon_first.group("year") and parsed < today:
                parsed = parsed.replace(year=year + 1)
            return parsed, pat_mon_first.span()
        except ValueError:
            pass

    # Month + Year (no day): "november 2026", "nov 2026"
    pat_mon_year = re.search(
        rf"\b(?P<mon>{_MONTH_ALT})\s+(?P<year>20\d{{2}})\b",
        lowered,
    )
    if pat_mon_year:
        month = _MONTHS[pat_mon_year.group("mon")]
        year = int(pat_mon_year.group("year"))
        try:
            parsed = date(year, month, 1)
            return parsed, pat_mon_year.span()
        except ValueError:
            pass

    # Preposition + Bare Month: "before november", "by november", "in november"
    pat_prep_mon = re.search(
        rf"\b(?:before|by|in|until)\s+(?P<mon>{_MONTH_ALT})\b",
        lowered,
    )
    if pat_prep_mon:
        month = _MONTHS[pat_prep_mon.group("mon")]
        year = today.year
        try:
            parsed = date(year, month, 1)
            if parsed < today:
                parsed = parsed.replace(year=year + 1)
            return parsed, pat_prep_mon.span()
        except ValueError:
            pass

    return None, None


def _extract_deadline(text: str) -> tuple[Optional[int], str]:
    patterns = (
        rf"(?:within|in|after|next)\s+(?P<n>\d+)\s*(?P<unit>day|days|week|weeks|month|months)",
        rf"(?P<n>\d+)\s*(?P<unit>day|days|week|weeks|month|months)\s*(?:deadline|delivery)?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        count = int(match.group("n"))
        unit = match.group("unit").lower()
        days = count * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)
        cleaned = text[: match.start()] + " " + text[match.end() :]
        return days, cleaned

    for phrase, days in (("tomorrow", 1), ("today", 0), ("next week", 7), ("urgent", 3),
                         ("asap", 3), ("immediately", 1)):
        if phrase in text.lower():
            return days, re.sub(rf"\b{re.escape(phrase)}\b", " ", text, flags=re.IGNORECASE)

    # "before 30 sep", "it can be before 12'th of november", "before november 2026"
    calendar, span = _parse_calendar_date(text)
    if calendar is not None and span is not None:
        days = (calendar - datetime.now().date()).days
        start, end = span
        prefix = text[:start]
        m_prep = re.search(
            r"\b(?:it\s+can\s+be\s+|can\s+be\s+|deliver\s+by\s+|delivery\s+(?:by|before)\s+)?(?:before|by|until|on|around|within|in)\s*$",
            prefix,
            re.IGNORECASE,
        )
        if m_prep:
            start = m_prep.start()
        cleaned = text[:start] + " " + text[end:]
        return max(days, 0), cleaned

    return None, text


# Units that make a number a specification rather than an order size. Note that
# kg, m, tonne and the rest are absent on purpose: those are real order units
# and are matched by the branch above this one.
_SPEC_SUFFIX = re.compile(
    r"\s*(?:mah|wh|kwh|kw|w|watts?|v|volts?|a|amps?|mm|cm|nm|hz|khz|ghz|gsm|"
    r"micron|denier|gauge|ply|ports?|cycles?|seater|inch|inches|\"|bar|psi|"
    r"rpm|°c|c\b)",
    flags=re.IGNORECASE,
)

_PACKAGING_UNITS_SET = frozenset({
    "bora", "boras", "bori", "boris", "katta", "kattas",
    "bag", "bags", "sack", "sacks", "gunny bag", "gunny bags",
    "bale", "bales", "carton", "cartons", "box", "boxes",
    "crate", "crates", "drum", "drums", "pallet", "pallets",
    "pack", "packs",
})

_CANONICAL_PACKAGING: dict[str, str] = {
    "bora": "bora", "boras": "bora", "bori": "bora", "boris": "bora",
    "katta": "bora", "kattas": "bora",
    "bag": "bag", "bags": "bag", "sack": "bag", "sacks": "bag",
    "gunny bag": "bag", "gunny bags": "bag",
    "box": "box", "boxes": "box", "carton": "carton", "cartons": "carton",
    "crate": "crate", "crates": "crate", "drum": "drum", "drums": "drum",
    "pallet": "pallet", "pallets": "pallet", "bale": "bale", "bales": "bale",
    "pack": "pack", "packs": "pack",
}

# e.g. "each bora should cantain 55kg", "each sack 50 kg", "every bag must hold 25kg"
_PACK_WEIGHT_RE = re.compile(
    r"\b(?:each|every|per|one)\s+(?:(?P<p_unit_w>[a-z]+)\s+)?(?:should\s+|must\s+|to\s+)?(?:contain|cantain|contains|cantains|holds?|weighs?|weight|having|with|capacity)?\s*(?:of\s+)?(?P<wt>\d[\d,]*(?:\.\d+)?)\s*(?P<wt_unit>kg|kgs|kilo|kilos|kilogram|kilograms|g|gm|gms|gram|grams|quintal|quintals|qtl|tonne|tonnes|ton|tons|mt|lb|lbs)\b",
    re.IGNORECASE,
)

# e.g. "55kg each", "50 kg per bag", "25 kg in each sack"
_PACK_WEIGHT_RE_ALT = re.compile(
    r"\b(?P<wt>\d[\d,]*(?:\.\d+)?)\s*(?P<wt_unit>kg|kgs|kilo|kilos|kilogram|kilograms|g|gm|gms|gram|grams|quintal|quintals|qtl|tonne|tonnes|ton|tons|mt|lb|lbs)\s*(?:each|per\s+(?P<p_unit_w>[a-z]+)|in\s+each\s+(?P<p_unit_w2>[a-z]+))\b",
    re.IGNORECASE,
)

# e.g. "bora of 55kg", "bags with 50 kg", "carton of 10kg"
_PACK_WEIGHT_RE_DIRECT = re.compile(
    r"\b(?P<p_unit_w>bora|boras|bori|boris|katta|kattas|bag|bags|sack|sacks|bale|bales|carton|cartons|box|boxes|crate|crates|drum|drums|pallet|pallets|pack|packs)\s+(?:of\s+|with\s+)?(?P<wt>\d[\d,]*(?:\.\d+)?)\s*(?P<wt_unit>kg|kgs|kilo|kilos|kilogram|kilograms|g|gm|gms|gram|grams|quintal|quintals|qtl|tonne|tonnes|ton|tons|mt|lb|lbs)\b",
    re.IGNORECASE,
)

# e.g. "1000 boras", "500 bags pcs", "200 cartons", "1000 bora pcs"
_PACK_COUNT_RE = re.compile(
    r"\b(?P<count>\d[\d,]*(?:\.\d+)?)\s*(?:of\s+)?(?P<p_unit_c>bora|boras|bori|boris|katta|kattas|gunny\s+bags?|bag|bags|sack|sacks|bale|bales|carton|cartons|box|boxes|crate|crates|drum|drums|pallet|pallets|pack|packs)(?:\s+pcs)?\b",
    re.IGNORECASE,
)

# e.g. "in bora pcs", "packed in bags", "packaging in sacks"
_PACKAGING_PREF_RE = re.compile(
    r"\b(?:in|packed\s+in|packaging\s*(?:in|:)?)\s+(?P<pack_pref>bora|boras|bori|boris|katta|kattas|gunny\s+bags?|bag|bags|sack|sacks|bale|bales|carton|cartons|box|boxes|crate|crates|drum|drums|pallet|pallets|pack|packs)(?:\s+pcs)?\b",
    re.IGNORECASE,
)


def _extract_compound_packaging(
    text: str,
) -> tuple[Optional[Decimal], Optional[str], dict[str, Any], str]:
    working = text
    count: Optional[Decimal] = None
    pack_unit: Optional[str] = None
    pack_wt: Optional[Decimal] = None
    pack_wt_unit: Optional[str] = None

    # 1. Look for pack count: e.g. "1000 boras", "500 bags"
    m_count = _PACK_COUNT_RE.search(working)
    if m_count:
        count = _to_decimal(m_count.group("count"))
        raw_u = m_count.group("p_unit_c").lower()
        pack_unit = _CANONICAL_PACKAGING.get(raw_u, raw_u)
        working = working[: m_count.start()] + " " + working[m_count.end() :]

    # 2. Look for single pack capacity/weight: e.g. "each bora should cantain 55kg"
    m_wt = _PACK_WEIGHT_RE.search(working)
    if not m_wt:
        m_wt = _PACK_WEIGHT_RE_ALT.search(working)
    if not m_wt:
        m_wt = _PACK_WEIGHT_RE_DIRECT.search(working)

    if m_wt:
        pack_wt = _to_decimal(m_wt.group("wt"))
        pack_wt_unit = m_wt.group("wt_unit").lower()
        pw_u = m_wt.groupdict().get("p_unit_w") or m_wt.groupdict().get("p_unit_w2")
        if pw_u and pw_u.lower() in _PACKAGING_UNITS_SET:
            if not pack_unit:
                pack_unit = _CANONICAL_PACKAGING.get(pw_u.lower(), pw_u.lower())
        working = working[: m_wt.start()] + " " + working[m_wt.end() :]

    # 3. Look for explicit packaging preference: e.g. "in bora pcs"
    m_pref = _PACKAGING_PREF_RE.search(working)
    if m_pref:
        raw_pref = m_pref.group("pack_pref").lower()
        if not pack_unit:
            pack_unit = _CANONICAL_PACKAGING.get(raw_pref, raw_pref)
        working = working[: m_pref.start()] + " " + working[m_pref.end() :]

    # If pack_unit is known, clean leftover packaging references like "bora pcs" or "in bora"
    if pack_unit:
        working = re.sub(
            rf"\b(?:in\s+)?{re.escape(pack_unit)}(?:s)?(?:\s+pcs)?\b",
            " ",
            working,
            flags=re.IGNORECASE,
        )

    attrs: dict[str, Any] = {}
    if pack_unit:
        attrs["packaging"] = pack_unit
    if count is not None:
        attrs["pack_count"] = int(count) if count == int(count) else float(count)
    if pack_wt is not None and pack_wt_unit:
        attrs["pack_weight"] = {"value": float(pack_wt), "unit": pack_wt_unit}
        single_kg = unit_converter.to_kg(pack_wt, pack_wt_unit)
        if single_kg is not None:
            attrs["pack_weight_kg"] = float(single_kg)
            if count is not None:
                tot_kg = count * single_kg
                attrs["total_weight_kg"] = float(tot_kg)
                attrs["total_weight_tons"] = float(tot_kg / Decimal("1000"))

    return count, pack_unit, attrs, working


def _extract_quantity(text: str) -> tuple[Optional[Decimal], Optional[str], str]:
    unit_alt = "|".join(sorted(_UNITS, key=len, reverse=True))
    match = re.search(rf"(?P<amt>{_NUMBER})\s*(?P<unit>{unit_alt})\b", text, flags=re.IGNORECASE)
    if match:
        value = _to_decimal(match.group("amt"))
        unit = match.group("unit").lower()
        cleaned = text[: match.start()] + " " + text[match.end() :]
        return value, unit, cleaned

    # A bare number, once price and deadline have already been consumed --
    # skipping any that is really a specification.
    #
    # "i want a 20000 mah power bank" used to set the order quantity to 20,000
    # as well as the capacity, so the assistant never asked how many the user
    # actually wanted and searched with a quantity nobody had given it.
    for match in re.finditer(rf"\b(?P<amt>{_NUMBER})\b", text):
        if _SPEC_SUFFIX.match(text, match.end()):
            continue
        value = _to_decimal(match.group("amt"))
        if value is not None and value >= 10:
            cleaned = text[: match.start()] + " " + text[match.end() :]
            return value, None, cleaned

    return None, None, text


def _extract_role(text: str) -> Optional[RFQRole]:
    lowered = text.lower()
    sell_at = min((lowered.find(cue) for cue in _SELL_CUES if cue in lowered), default=-1)
    buy_at = min((lowered.find(cue) for cue in _BUY_CUES if cue in lowered), default=-1)

    if sell_at == -1 and buy_at == -1:
        return None
    if sell_at == -1:
        return RFQRole.BUYER
    if buy_at == -1:
        return RFQRole.SELLER
    # Whichever the sentence leads with.
    return RFQRole.SELLER if sell_at < buy_at else RFQRole.BUYER


# Measurements and codes people type into a search box, mapped onto the
# attribute names the catalogue uses. Without these, "20000 mah 65w" carries no
# weight at all and a 10,000 mAh unit outranks the one that was asked for.
_MEASUREMENTS: tuple[tuple[str, str, str], ...] = (
    (r"(\d[\d,]*)\s*mah\b", "capacity", "mAh"),
    (r"(\d[\d,]*(?:\.\d+)?)\s*w(?:att)?s?\b", "output_power", "W"),
    (r"(\d+(?:\.\d+)?)\s*v(?:olt)?s?\b", "output_voltage", "V"),
    (r"(\d+(?:\.\d+)?)\s*a(?:mp|mps)?\b", "current_rating", "A"),
    (r"(\d+(?:\.\d+)?)\s*mm\b", "thickness", "mm"),
    (r"(\d+(?:\.\d+)?)\s*kg\b", "capacity", "kg"),
    (r"(\d+)\s*gsm\b", "gsm", ""),
    (r"(\d+)\s*ports?\b", "ports", ""),
    (r"(\d+)\s*cycles?\b", "cycle_life", ""),
    (r"(\d+)\s*seater\b", "seats", ""),
)

# Quality marks are written as bare acronyms and stored as lists on a listing.
_CERTIFICATIONS = (
    "FSC", "CE", "RoHS", "UL", "FCC", "BIS", "PSE", "UN38.3", "IEC62133",
    "GlobalGAP", "HACCP", "OEKO-TEX", "GOTS", "BCI", "BIFMA", "SGS", "APEDA",
    "ISO 9001", "ISO 14001", "ISO 22000", "USB-IF", "ASTM A240", "EN 10088",
)

_PROTOCOLS = {
    "usb pd 3.1": "USB PD 3.1", "pd 3.1": "USB PD 3.1", "pd3.1": "USB PD 3.1",
    "usb pd 3.0": "USB PD 3.0", "pd 3.0": "USB PD 3.0", "pd3.0": "USB PD 3.0",
    "quick charge 5": "Quick Charge 5", "quick charge 4": "Quick Charge 4+",
    "qc 4": "Quick Charge 4+", "pps": "PPS",
    # Unversioned, and last: the specific entries above win when a version was
    # typed. "USB PD" shares all its tokens with "USB PD 3.1", so it scores full
    # marks against any PD listing and zero against Quick Charge -- which is
    # what somebody who did not name a version meant.
    "usb pd": "USB PD", "usb-pd": "USB PD",
    "quick charge": "Quick Charge",
}


# Alloy, commodity and agricultural grades ("ss 316l", "grade 304", "grade a", "class i", "extra class").
_SS_GRADE_RE = re.compile(r"\b(?:ss|grade)\s*(\d{3}[a-z]?)\b|\b(\d{3}l)\b", re.IGNORECASE)
_COMMODITY_GRADE_RE = re.compile(r"\b(?:grade\s+([a-c]|1|2|3)|class\s+(i{1,3}|iv|v|[a-c]|\d+)|extra\s+class)\b", re.IGNORECASE)
_INDUSTRY_GRADE_RE = re.compile(r"\b(food|industrial|pharma|feed|commercial)\s+grade\b", re.IGNORECASE)


def _find_grade(lowered: str) -> Optional[str]:
    m_ss = _SS_GRADE_RE.search(lowered)
    if m_ss:
        return f"SS {(m_ss.group(1) or m_ss.group(2)).upper()}"
    m_ind = _INDUSTRY_GRADE_RE.search(lowered)
    if m_ind:
        return f"{m_ind.group(1).capitalize()} Grade"
    m_comm = _COMMODITY_GRADE_RE.search(lowered)
    if m_comm:
        matched = m_comm.group(0).lower()
        if "extra class" in matched:
            return "Extra Class"
        if m_comm.group(1):
            return f"Grade {m_comm.group(1).upper()}"
        if m_comm.group(2):
            return f"Class {m_comm.group(2).upper()}"
    return None


def _strip_grades(text: str) -> str:
    """Blank out grade codes before quantities are read off the text."""
    t = _SS_GRADE_RE.sub(" ", text)
    t = _COMMODITY_GRADE_RE.sub(" ", t)
    t = _INDUSTRY_GRADE_RE.sub(" ", t)
    return t


def _extract_measurements(lowered: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for pattern, key, unit in _MEASUREMENTS:
        match = re.search(pattern, lowered)
        if not match or key in found:
            continue
        raw = match.group(1).replace(",", "")
        number: Any = float(raw) if "." in raw else int(raw)
        found[key] = {"value": number, "unit": unit} if unit else number
    return found


def _extract_attributes(text: str) -> dict[str, Any]:
    lowered = text.lower()
    attributes: dict[str, Any] = {}

    for color in _COLORS:
        if re.search(rf"\b{re.escape(color)}\b", lowered):
            attributes["color"] = "grey" if color == "gray" else color
            break

    for material in _MATERIALS:
        if re.search(rf"\b{re.escape(material)}\b", lowered):
            attributes["material"] = material
            break

    for quality in _QUALITIES:
        if re.search(rf"\b{re.escape(quality)}\b", lowered):
            attributes[quality.replace(" ", "_")] = True

    ply = re.search(r"(\d+)\s*-?\s*ply", lowered)
    if ply:
        attributes["ply"] = f"{ply.group(1)}-ply"

    if re.search(r"\btype\s*-?\s*c\b", lowered):
        attributes["type"] = "C"
    elif re.search(r"\btype\s*-?\s*a\b", lowered):
        attributes["type"] = "A"
    elif re.search(r"\btype\s*-?\s*b\b", lowered):
        attributes["type"] = "B"

    # "c to c", "c-c", "usb c to usb c"
    # Only c_to_c, not a "connector" string: c_to_c exists on cables and power
    # banks alike, whereas "connector" is cable-only and would cost every other
    # product half a mark for not having the field.
    if re.search(r"\bc\s*(?:to|-|\u2013)\s*c\b", lowered):
        attributes["c_to_c"] = True

    grade = _find_grade(lowered)
    if grade:
        attributes["grade"] = grade

    for phrase, canonical in _PROTOCOLS.items():
        if phrase in lowered:
            attributes["fast_charge_protocol"] = canonical
            break

    marks = [
        mark for mark in _CERTIFICATIONS
        if re.search(rf"\b{re.escape(mark.lower())}\b", lowered)
    ]
    if marks:
        attributes["certification"] = marks

    # Measurements are read from the text with the order quantity removed, so
    # "500 kg of basmati rice" is an order for 500kg and not also a product with
    # a 500kg capacity -- which would have penalised every seller who did not
    # declare a field the buyer never meant to ask about.
    _, _, without_quantity = _extract_quantity(_strip_grades(text))
    attributes.update(_extract_measurements(without_quantity.lower()))
    return attributes


def _infer_category(text: str) -> Optional[str]:
    lowered = text.lower()
    lowered = re.sub(r"\bvagitable[s]?\b", "vegetable", lowered)
    lowered = re.sub(r"\bvegitable[s]?\b", "vegetable", lowered)
    lowered = re.sub(r"\btomatos\b", "tomatoes", lowered)
    lowered = re.sub(r"\bpotatos\b", "potatoes", lowered)
    best: Optional[str] = None
    best_hits = 0
    for category, hints in _CATEGORY_HINTS.items():
        hits = sum(
            1
            for hint in hints
            if re.search(rf"\b{re.escape(hint)}(?:s|es)?\b", lowered)
        )
        if hits > best_hits:
            best, best_hits = category, hits
    return best


def _extract_product(
    text: str,
    attributes: dict[str, Any],
    location: Optional[Location] = None,
    city: Optional[City] = None,
) -> Optional[str]:
    lowered = text.lower()

    if location:
        lowered = re.sub(rf"\b{re.escape(location.name.lower())}\b", " ", lowered)
        if location.matched_key:
            lowered = re.sub(rf"\b{re.escape(location.matched_key.lower())}\b", " ", lowered)
        if location.city:
            lowered = re.sub(rf"\b{re.escape(location.city.lower())}\b", " ", lowered)
        if location.state:
            lowered = re.sub(rf"\b{re.escape(location.state.lower())}\b", " ", lowered)
        if location.country:
            lowered = re.sub(rf"\b{re.escape(location.country.lower())}\b", " ", lowered)
    elif city:
        lowered = re.sub(rf"\b{re.escape(city.name.lower())}\b", " ", lowered)

    # Strip common states, regions and country markers so they never become a product name
    _GEO_WORDS = (
        r"haryana|hariyana|haryanvi|punjab|panjab|maharashtra|gujarat|gujrat|rajasthan|"
        r"karnataka|kerala|bihar|assam|odisha|orissa|delhi|chandigarh|kolkata|"
        r"mumbai|pune|bhopal|indore|bangalore|bengaluru|chennai|hyderabad|"
        r"india|china|germany"
    )
    lowered = re.sub(rf"\b(?:{_GEO_WORDS})\b", " ", lowered)

    # Normalize apostrophe ordinals like 12'th -> 12th so they match _ORDINALS / _FILLER
    lowered = re.sub(r"(\d+)'(th|st|nd|rd)\b", r"\1\2", lowered)
    lowered = lowered.replace("'", " ")

    for key, value in attributes.items():
        if isinstance(value, str):
            if len(value) >= 3:
                lowered = re.sub(rf"\b{re.escape(value.lower())}\b", " ", lowered)
        elif value is True:
            lowered = re.sub(rf"\b{re.escape(key.lower().replace('_', ' '))}\b", " ", lowered)
            for part in key.lower().split("_"):
                if len(part) >= 3:
                    lowered = re.sub(rf"\b{re.escape(part)}\b", " ", lowered)

    # Strip grades and attribute labels so qualities never form a product name
    lowered = _strip_grades(lowered)
    lowered = re.sub(
        r"\b(?:grade|quality|class|spec|specification|color|colour|size|material|ply)\b",
        " ",
        lowered,
    )

    for symbol in ("₹", "$", "€", "£"):
        lowered = lowered.replace(symbol, " ")

    # Strip currency tokens, names and typos so they never form a product name
    lowered = re.sub(
        rf"\b(?:{_CURRENCY_SYMBOLS}|currency|currencies)\b",
        " ",
        lowered,
        flags=re.IGNORECASE,
    )

    # Unit words are NOT stripped here. The quantity match already removed the
    # span it consumed, and "box", "set" and "roll" are product names as often
    # as they are units.
    words = [
        word.strip(".-")
        for word in _TOKEN_RE.findall(lowered)
        if word not in _FILLER
        and word not in _CHITCHAT
        and not word.replace(".", "").isdigit()
        # "3000 pcses" leaves "pcses" behind; singularising catches it while
        # leaving "boxes" alone, since "box" is not a counting unit.
        and word not in _COUNT_UNITS
        and singularise(word) not in _COUNT_UNITS
        and word not in _ORDINALS
        and word not in _MONTHS
    ]
    words = [
        w for w in words
        if w and w not in _FILLER and w not in _CHITCHAT and len(w) > 1 and not _is_near_filler(w)
    ]
    if not words:
        return None
    if all(word in _CHITCHAT for word in words):
        return None
    # Keep the order the user used; it reads back naturally in the reply.
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
    return " ".join(seen[:6])


_NEGATION_PATTERNS = (
    # e.g. "i dont need type c cables", "dont need type c", "do not want white"
    r"\b(?:i\s+)?(?:don't|dont|do\s+not|didnt|didn't|wont|won't)\s+(?:need|want|require|ask\s+for)\s+(?P<neg>[^,;.\n]+?)(?=(?:i\s+(?:need|want|require|prefer)|but|instead|rather|make\s+it|,|;|\.|$))",
    # e.g. "not type c", "not white", "no type c", "without type c", "free from type c"
    r"\b(?:not|no|without|free\s+from|except|excluding)\s+(?P<neg>[a-z0-9\s\-]+?)(?=(?:i\s+(?:need|want|require|prefer)|but|instead|rather|make\s+it|,|;|\.|$))",
    # e.g. "remove type c", "drop type c", "cancel type c"
    r"\b(?:remove|drop|cancel|delete|forget)\s+(?:the\s+)?(?P<neg>[a-z0-9\s\-]+?)(?=(?:i\s+(?:need|want|require|prefer)|but|instead|rather|make\s+it|,|;|\.|$))",
)

# Product-specific technical attributes that should be pruned when a product changes
_PRODUCT_SPECIFIC_ATTRS = frozenset({
    "type", "c_to_c", "fast_charge_protocol", "ply", "gsm", "seats",
    "output_power", "output_voltage", "current_rating", "cycle_life",
})


def _detect_negations(text: str) -> tuple[set[str], str]:
    """Identify negated attributes (e.g. 'dont need type c', 'not white') and remove them from text.

    Returns (negated_keys, cleaned_text).
    """
    negated_keys: set[str] = set()
    cleaned = text

    for pat in _NEGATION_PATTERNS:
        for match in re.finditer(pat, text, flags=re.IGNORECASE):
            neg_span = match.group("neg").strip()
            attrs = _extract_attributes(neg_span)
            for k in attrs.keys():
                negated_keys.add(k)
            lowered_span = neg_span.lower()
            if re.search(r"\btype\s*-?\s*c\b", lowered_span):
                negated_keys.add("type")
            if re.search(r"\b(?:color|colour)\b", lowered_span):
                negated_keys.add("color")
            if re.search(r"\bgrade\b", lowered_span):
                negated_keys.add("grade")
            if re.search(r"\bmaterial\b", lowered_span):
                negated_keys.add("material")

            start, end = match.span()
            cleaned = cleaned[:start] + " " * (end - start) + cleaned[end:]

    return negated_keys, cleaned


def extract(message: str) -> Requirements:
    """Parse one message in isolation. Merging with history happens elsewhere."""
    requirements = Requirements()

    role = _extract_role(message)
    if role is not None:
        requirements.role = role

    # 1. Negation detection: extract negated attributes and blank out negated clauses
    negated_attrs, positive_text = _detect_negations(message)
    requirements.negated_attributes = negated_attrs
    working = positive_text

    price, currency, per_unit, working = _extract_price(working)
    if price is None:
        standalone_currency, working = _extract_standalone_currency(working)
        if standalone_currency is not None:
            currency = standalone_currency
    deadline_days, working = _extract_deadline(working)

    # Packaging & compound weight extraction
    pack_count, pack_unit, pack_attrs, working = _extract_compound_packaging(working)

    quantity, unit, working = _extract_quantity(_strip_grades(working))

    if price is not None:
        requirements.price_amount = price
        requirements.price_currency = currency
        requirements.price_per_unit = per_unit or unit
    elif currency is not None:
        requirements.price_currency = currency
    if deadline_days is not None:
        requirements.deadline_days = deadline_days

    if pack_count is not None:
        requirements.quantity_value = pack_count
        requirements.quantity_unit = pack_unit or unit or "pcs"
    elif quantity is not None:
        requirements.quantity_value = quantity
        requirements.quantity_unit = pack_unit or unit

    loc = find_location(message)
    if loc:
        requirements.city = loc.city
        requirements.state = loc.state
        requirements.country = loc.country
        requirements.currency_hint = loc.currency
        requirements.latitude = Decimal(str(loc.latitude))
        requirements.longitude = Decimal(str(loc.longitude))

    # Attributes extracted from positive_text (with negated clauses removed, but specs intact)
    extracted_attrs = _extract_attributes(positive_text)
    for k in negated_attrs:
        extracted_attrs.pop(k, None)
    requirements.attributes = extracted_attrs
    if pack_attrs:
        requirements.attributes.update(pack_attrs)
    requirements.category = _infer_category(working)
    requirements.product = _extract_product(working, requirements.attributes, location=loc)

    return requirements


def parse_answer(field: str, message: str) -> Optional[Requirements]:
    """Read a reply as an answer to one specific question, and nothing else.

    This exists because the general parser is the wrong tool for a reply. Asked
    "by when do you need it?", someone answers "i need it before 30 sep" -- and
    a general parse of that sentence sees the residual word "sep" as a product
    name and the number 30 as a quantity, silently destroying the search. When a
    question is on the table, only the field it asked about may change.

    Returns None when the reply contains no answer to that field, so the caller
    can fall back to treating it as a general statement.
    """
    answer = Requirements()

    if field == "quantity":
        _, without_deadline = _extract_deadline(message)

        pack_count, pack_unit, pack_attrs, _ = _extract_compound_packaging(without_deadline)
        if pack_count is not None:
            answer.quantity_value = pack_count
            answer.quantity_unit = pack_unit or "pcs"
            if pack_attrs:
                answer.attributes.update(pack_attrs)
            return answer

        value, unit, _ = _extract_quantity(without_deadline)
        if value is None:
            # A bare number is a perfectly good answer to "how many?", however
            # small -- the general parser ignores anything under ten.
            bare = re.search(rf"\b({_NUMBER})\b", without_deadline)
            if bare:
                end_pos = bare.end()
                if not re.match(r"\s*(?:'?(?:st|nd|rd|th))\b", without_deadline[end_pos:], re.IGNORECASE):
                    value = _to_decimal(bare.group(1))
        if value is None:
            return None
        answer.quantity_value = value
        answer.quantity_unit = unit or _bare_unit(without_deadline)
        if pack_attrs:
            answer.attributes.update(pack_attrs)
        return answer

    if field == "price":
        amount, currency, per_unit, _ = _extract_price(message)
        if amount is None:
            bare = re.search(rf"\b({_NUMBER})\b", message)
            amount = _to_decimal(bare.group(1)) if bare else None
            currency = _currency_in(message)
        if amount is None:
            return None
        answer.price_amount = amount
        answer.price_currency = currency
        answer.price_per_unit = per_unit
        return answer

    if field == "location":
        loc = find_location(message)
        if loc is None:
            return None
        answer.city = loc.city
        answer.state = loc.state
        answer.country = loc.country
        answer.currency_hint = loc.currency
        answer.latitude = Decimal(str(loc.latitude))
        answer.longitude = Decimal(str(loc.longitude))
        return answer

    if field == "deadline":
        days, _ = _extract_deadline(message)
        if days is not None:
            answer.deadline_days = days
            return answer
        bare = re.search(r"^\s*(\d{1,3})\s*$", message)
        if bare:
            answer.deadline_days = int(bare.group(1))
            return answer
        return None

    return None


def _bare_unit(message: str) -> Optional[str]:
    """A unit mentioned without a number, e.g. answering "pcs"."""
    unit_alt = "|".join(sorted(_UNITS, key=len, reverse=True))
    match = re.search(rf"\b({unit_alt})\b", message, flags=re.IGNORECASE)
    return match.group(1).lower() if match else None


def _currency_in(message: str) -> Optional[str]:
    for token, code in _CURRENCIES.items():
        if token.isalpha():
            if re.search(rf"\b{re.escape(token)}\b", message, flags=re.IGNORECASE):
                return code
        elif token in message:
            return code
    return None


def merge_answer(previous: Requirements, answer: Requirements) -> Requirements:
    """Fold a field-scoped answer in. Product is never touched; packaging attributes merge."""
    merged = previous.model_copy(deep=True)
    for field in (
        "quantity_value", "quantity_unit", "price_amount", "price_currency",
        "price_per_unit", "deadline_days",
    ):
        value = getattr(answer, field)
        if value is not None:
            setattr(merged, field, value)

    # Location handling in answer
    if answer.state is not None and answer.city is None:
        merged.city = None
        merged.state = answer.state
        merged.country = answer.country or merged.country
        merged.latitude = answer.latitude
        merged.longitude = answer.longitude
        if answer.currency_hint:
            merged.currency_hint = answer.currency_hint
    elif answer.country is not None and answer.state is None and answer.city is None:
        merged.city = None
        merged.state = None
        merged.country = answer.country
        merged.latitude = answer.latitude
        merged.longitude = answer.longitude
        if answer.currency_hint:
            merged.currency_hint = answer.currency_hint
    else:
        for field in ("city", "state", "country", "currency_hint", "latitude", "longitude"):
            value = getattr(answer, field)
            if value is not None:
                setattr(merged, field, value)

    if answer.attributes:
        merged.attributes = {**merged.attributes, **answer.attributes}
    for field in ("quantity", "price", "location", "deadline"):
        if merged.known(field) and field in merged.skipped:
            merged.skipped.remove(field)
    return merged


def merge(previous: Requirements, update: Requirements, message: str) -> Requirements:
    """Fold a new message into what is already known.

    Later messages win on the fields they mention. A refinement such as
    "show me black instead" must not wipe the product, the city or the price
    that earlier turns established.
    """
    merged = previous.model_copy(deep=True)

    if _extract_role(message) is not None:
        merged.role = update.role

    for field in (
        "quantity_value", "quantity_unit", "price_amount", "price_currency",
        "price_per_unit", "deadline_days",
    ):
        value = getattr(update, field)
        if value is not None:
            setattr(merged, field, value)

    # Location pivot: handle city / state / country updates properly
    if update.state is not None and update.city is None:
        # User specified a state/region (e.g. "in hariyana", "find in punjab").
        # Clear previous city so search is not artificially restricted to an old city in a different state!
        merged.city = None
        merged.state = update.state
        merged.country = update.country or merged.country
        merged.latitude = update.latitude
        merged.longitude = update.longitude
        if update.currency_hint:
            merged.currency_hint = update.currency_hint
    elif update.country is not None and update.state is None and update.city is None:
        # User specified nationwide / pan-India
        merged.city = None
        merged.state = None
        merged.country = update.country
        merged.latitude = update.latitude
        merged.longitude = update.longitude
        if update.currency_hint:
            merged.currency_hint = update.currency_hint
    else:
        for field in ("city", "state", "country", "currency_hint", "latitude", "longitude"):
            value = getattr(update, field)
            if value is not None:
                setattr(merged, field, value)

    # 1. Remove negated attributes
    if update.negated_attributes:
        for neg_key in update.negated_attributes:
            merged.attributes.pop(neg_key, None)

    # 2. Add positive attributes
    if update.attributes:
        merged.attributes = {**merged.attributes, **update.attributes}

    # 3. Product update & incompatible attribute pruning
    if update.product and not _is_refinement_only(update, message):
        product_changed = bool(previous.product and update.product != previous.product)
        if product_changed:
            target_category = update.category or _infer_category(update.product)
            category_changed = bool(
                target_category and previous.category and target_category != previous.category
            )
            if category_changed:
                # Complete category switch (e.g. Electronics -> Agriculture).
                # Wipe old attributes clean: cable color/specs have nothing to do with vegetables.
                merged.attributes = dict(update.attributes)
                merged.category = target_category
            else:
                for spec_key in _PRODUCT_SPECIFIC_ATTRS:
                    if spec_key not in update.attributes:
                        merged.attributes.pop(spec_key, None)
                merged.category = target_category or update.category or merged.category
        else:
            merged.category = update.category or merged.category

        merged.product = update.product
    elif update.category and not merged.category:
        merged.category = update.category

    # Answering a question un-skips whatever it was about.
    for field in ("quantity", "price", "location", "deadline"):
        if merged.known(field) and field in merged.skipped:
            merged.skipped.remove(field)

    return merged


def _is_refinement_only(update: Requirements, message: str) -> bool:
    """True when the message only adjusts attributes or specs already being tracked.

    "show me black color" leaves "black color" as the residual product phrase,
    which would otherwise replace a perfectly good "usb type c cable".
    """
    if not update.product:
        return True
    product_words = set(update.product.split())
    attribute_words = {
        str(value).lower()
        for value in update.attributes.values()
        if isinstance(value, str)
    }
    # Add boolean attributes and attribute keys/words
    for k, v in update.attributes.items():
        attribute_words.add(k.lower())
        attribute_words |= set(k.lower().replace("_", " ").split())
        if isinstance(v, str):
            attribute_words |= set(v.lower().split())

    attribute_words |= {
        "color", "colour", "size", "material", "type", "ply", "grade", "quality", "class",
        "organic", "fresh", "standard", "spec", "specification", "variety", "pure", "raw",
        "price", "cost", "rate", "budget", "pay", "currency", "target",
        "rupee", "rupees", "ruppes", "rupes", "dollar", "dollars", "inr", "usd", "eur", "rs",
        "unit", "units", "pcs", "piece", "pieces", "day", "days", "week", "weeks", "month", "months",
        "year", "years", "date", "dates", "calendar", "time", "th", "st", "nd", "rd",
        "delhi", "mumbai", "indore", "bangalore", "bengaluru", "chennai", "kolkata", "hyderabad",
        "haryana", "hariyana", "haryanvi", "punjab", "panjab", "maharashtra", "gujarat", "gujrat",
        "rajasthan", "karnataka", "kerala", "bihar", "assam", "odisha", "orissa", "chandigarh",
        "india", "nationwide",
    }
    attribute_words |= set(_MONTHS.keys())
    attribute_words |= _ORDINALS
    attribute_words |= _FILLER
    attribute_words |= _CHITCHAT
    return product_words.issubset(attribute_words)

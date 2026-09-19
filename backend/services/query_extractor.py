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
from services.locations import City, find_city
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
    "meter", "meters", "metre", "metres", "m",
    "box", "boxes", "carton", "cartons", "roll", "rolls", "set", "sets",
    # Trade packaging units. Without "pallet", "20 pallets with 1000 kg
    # capacity" was read as an order for 1,000 kg.
    "pallet", "pallets", "bag", "bags", "drum", "drums", "bale", "bales",
    "container", "containers", "reel", "reels", "pack", "packs", "crate",
    "crates", "sack", "sacks", "spool", "spools", "sheet", "sheets",
    "litre", "litres", "liter", "liters", "l",
)

# Units that only ever count things. A leftover "pcs"/"pcses"/"kg" is noise in a
# product name, whereas "box", "carton" and "roll" are products in their own
# right and must survive.
_COUNT_UNITS = frozenset(
    """pc pcs piece pieces unit units no nos each kg kgs kilogram kilograms
    tonne tonnes ton tons mt m meter meters metre metres l litre litres liter
    liters""".split()
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
    "Electronics": ("cable", "charger", "usb", "type-c", "type c", "adapter",
                    "power bank", "electronic", "gan", "battery", "led"),
    "Packaging": ("box", "carton", "corrugated", "fibc", "bulk bag", "packaging",
                  "bubble wrap", "pouch", "packing"),
    "Furniture": ("table", "chair", "sofa", "desk", "furniture", "cabinet",
                  "wardrobe", "bed"),
    "Agriculture": ("apple", "rice", "wheat", "grain", "fruit", "vegetable",
                    "agro", "basmati", "onion", "potato", "spice"),
    "Textiles": ("shirt", "fabric", "denim", "cotton", "textile", "garment",
                 "t-shirt", "trouser", "saree", "yarn"),
}

# Stripped from the residual product phrase.
_FILLER = frozenset(
    """i we want need looking for buy sell selling buying supply supplying show me
    find get me some any please of the a an at in on to from with and or for is
    are be will would can could my our your it its that this these those now next
    also plus per each price prices priced pricing cost costs costing rate rates
    pay pays paying paid payment budget budgets target targets spend spending spent
    afford offer offering quote quoted quoting charge charges charged
    within before by until due deadline days
    day week weeks month months year years quantity qty around about approx nearly
    roughly upto up maximum minimum min max least most only just very really
    prefer preferably instead rather change switch convert set update make it them one give requirement
    almost exactly total overall
    rupee rupees ruppes rupes rupess ruppees rupe dollar dollars buck bucks euro euros pound pounds
    currency currencies inr usd eur gbp rs
    unit units pcs piece pieces
    color colour colours shade size under below above over between
    good best cheap cheapest quality""".split()
)

# A message made only of these is conversation, not a product. Without this,
# "hello" becomes the product and the user is told no sellers stock it.
_CHITCHAT = frozenset(
    """hi hello hey yo hiya greetings thanks thank you ok okay k sure yes yeah
    yep no nope cool nice great good morning afternoon evening bye goodbye
    test testing help what how who when where why please sorry""".split()
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
    if cleaned in _CURRENCIES:
        return _CURRENCIES[cleaned]
    if re.match(r"^rup+[e|p]*s*$", cleaned):
        return "INR"
    if re.match(r"^dollars?$", cleaned) or re.match(r"^bucks?$", cleaned):
        return "USD"
    if re.match(r"^euros?$", cleaned):
        return "EUR"
    if re.match(r"^pounds?$", cleaned):
        return "GBP"
    return None


_CURRENCY_SYMBOLS = (
    r"₹|\$|€|£|rs\.?|inr|usd|eur|gbp|dollars?|bucks?|"
    r"rup+[e|p]*s*|rupe+s?|euros?|pounds?"
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


def _parse_calendar_date(text: str, today: Optional[date] = None) -> Optional[date]:
    """An absolute date written the way people actually type one.

    Handles "30 sep", "sep 30", "30 september 2026", "2026-09-30" and
    "30/09/2026". A day-and-month with no year that has already passed is read
    as next year, because nobody asks for delivery in the past.
    """
    today = today or datetime.now().date()
    lowered = text.lower()

    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lowered)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    numeric = re.search(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b", lowered)
    if numeric:
        day, month = int(numeric.group(1)), int(numeric.group(2))
        year = int(numeric.group(3) or today.year)
        if year < 100:
            year += 2000
        try:
            parsed = date(year, month, day)
        except ValueError:
            return None
        if numeric.group(3) is None and parsed < today:
            parsed = parsed.replace(year=year + 1)
        return parsed

    patterns = (
        rf"\b(?P<day>\d{{1,2}})\s*(?:st|nd|rd|th)?\s+(?P<mon>{_MONTH_ALT})\b",
        rf"\b(?P<mon>{_MONTH_ALT})\s+(?P<day>\d{{1,2}})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        month = _MONTHS[match.group("mon")]
        day = int(match.group("day"))
        year_match = re.search(r"\b(20\d{2})\b", lowered)
        year = int(year_match.group(1)) if year_match else today.year
        try:
            parsed = date(year, month, day)
        except ValueError:
            return None
        if year_match is None and parsed < today:
            parsed = parsed.replace(year=year + 1)
        return parsed

    return None


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
            return days, text.lower().replace(phrase, " ")

    # "before 30 sep" -- an absolute date, converted to days from today so the
    # rest of the pipeline keeps a single representation.
    calendar = _parse_calendar_date(text)
    if calendar is not None:
        days = (calendar - datetime.now().date()).days
        return max(days, 0), text

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


# Alloy and material grades: "ss 316l", "316L", "grade 304".
_GRADE_RE = re.compile(r"\b(?:ss|grade)\s*(\d{3}[a-z]?)\b|\b(\d{3}l)\b", re.IGNORECASE)


def _find_grade(lowered: str) -> Optional[str]:
    match = _GRADE_RE.search(lowered)
    if not match:
        return None
    return f"SS {(match.group(1) or match.group(2)).upper()}"


def _strip_grades(text: str) -> str:
    """Blank out grade codes before quantities are read off the text.

    "l" is a real order unit -- litres -- so "3mm SS 316L sheet" was parsed as
    an order for 316 litres.
    """
    return _GRADE_RE.sub(" ", text)


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
    best: Optional[str] = None
    best_hits = 0
    for category, hints in _CATEGORY_HINTS.items():
        # Word boundaries, not substrings: "gan" otherwise matches "organic"
        # and files a crate of apples under Electronics.
        hits = sum(
            1
            for hint in hints
            if re.search(rf"\b{re.escape(hint)}(?:s|es)?\b", lowered)
        )
        if hits > best_hits:
            best, best_hits = category, hits
    return best


def _extract_product(text: str, attributes: dict[str, Any], city: Optional[City]) -> Optional[str]:
    lowered = text.lower()

    if city:
        lowered = re.sub(rf"\b{re.escape(city.name.lower())}\b", " ", lowered)

    for value in attributes.values():
        # Whole words only, and never a value this short: replacing the "C" of
        # a Type-C cable as a substring turned "cable" into "able".
        if isinstance(value, str) and len(value) >= 3:
            lowered = re.sub(rf"\b{re.escape(value.lower())}\b", " ", lowered)

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
        and not word.replace(".", "").isdigit()
        # "3000 pcses" leaves "pcses" behind; singularising catches it while
        # leaving "boxes" alone, since "box" is not a counting unit.
        and word not in _COUNT_UNITS
        and singularise(word) not in _COUNT_UNITS
    ]
    words = [
        w for w in words
        if w and w not in _FILLER and len(w) > 1 and not _is_near_filler(w)
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


def extract(message: str) -> Requirements:
    """Parse one message in isolation. Merging with history happens elsewhere."""
    requirements = Requirements()

    role = _extract_role(message)
    if role is not None:
        requirements.role = role

    working = message
    price, currency, per_unit, working = _extract_price(working)
    if price is None:
        standalone_currency, working = _extract_standalone_currency(working)
        if standalone_currency is not None:
            currency = standalone_currency
    deadline_days, working = _extract_deadline(working)
    quantity, unit, working = _extract_quantity(_strip_grades(working))

    if price is not None:
        requirements.price_amount = price
        # No FX conversion exists, so an unstated currency stays unstated rather
        # than being guessed at.
        requirements.price_currency = currency
        requirements.price_per_unit = per_unit or unit
    elif currency is not None:
        requirements.price_currency = currency
    if deadline_days is not None:
        requirements.deadline_days = deadline_days
    if quantity is not None:
        requirements.quantity_value = quantity
        requirements.quantity_unit = unit

    city = find_city(message)
    if city:
        requirements.city = city.name
        requirements.state = city.region
        requirements.country = city.country
        requirements.currency_hint = city.currency
        requirements.latitude = Decimal(str(city.latitude))
        requirements.longitude = Decimal(str(city.longitude))

    requirements.attributes = _extract_attributes(message)
    requirements.category = _infer_category(message)
    requirements.product = _extract_product(working, requirements.attributes, city)

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
        value, unit, _ = _extract_quantity(message)
        if value is None:
            # A bare number is a perfectly good answer to "how many?", however
            # small -- the general parser ignores anything under ten.
            bare = re.search(rf"\b({_NUMBER})\b", message)
            value = _to_decimal(bare.group(1)) if bare else None
        if value is None:
            return None
        answer.quantity_value = value
        answer.quantity_unit = unit or _bare_unit(message)
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
        city = find_city(message)
        if city is None:
            return None
        answer.city = city.name
        answer.state = city.region
        answer.country = city.country
        answer.currency_hint = city.currency
        answer.latitude = Decimal(str(city.latitude))
        answer.longitude = Decimal(str(city.longitude))
        return answer

    if field == "deadline":
        days, _ = _extract_deadline(message)
        if days is None:
            return None
        answer.deadline_days = days
        return answer

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
    """Fold a field-scoped answer in. Product and attributes are never touched."""
    merged = previous.model_copy(deep=True)
    for field in (
        "quantity_value", "quantity_unit", "price_amount", "price_currency",
        "price_per_unit", "deadline_days", "city", "state", "country",
        "currency_hint", "latitude", "longitude",
    ):
        value = getattr(answer, field)
        if value is not None:
            setattr(merged, field, value)
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
        "price_per_unit", "deadline_days", "city", "state", "country",
        "currency_hint", "latitude", "longitude",
    ):
        value = getattr(update, field)
        if value is not None:
            setattr(merged, field, value)

    if update.attributes:
        merged.attributes = {**merged.attributes, **update.attributes}

    # The product only changes when the new message names one and is not merely
    # an adjustment to an attribute already understood.
    if update.product and not _is_refinement_only(update, message):
        merged.product = update.product
        merged.category = update.category or merged.category
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
    attribute_words |= {
        "color", "colour", "size", "material", "type", "ply", "grade",
        "price", "cost", "rate", "budget", "pay", "currency", "target",
        "rupee", "rupees", "ruppes", "rupes", "dollar", "dollars", "inr", "usd", "eur", "rs",
        "unit", "units", "pcs", "piece", "pieces", "day", "days", "week", "weeks", "month", "months",
    }
    return product_words.issubset(attribute_words)

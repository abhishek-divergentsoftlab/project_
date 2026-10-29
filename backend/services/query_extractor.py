"""Turn a natural-language search message into structured requirements.

Understanding is AI-first and open-world: ``extract_async`` asks a local model
(``services/ai_query_parser.py``), validates every field against the message
(``services/query_grounding.py``) and learns what it saw
(``services/learned_vocabulary.py``). The rule parser below remains the offline
fallback and the mechanical helper for numbers, prices and dates, and its word
lists are only seeds. Sync ``extract`` never touches the network.

History: this was originally a parser rather than a model call. Local models were probed
first (llama3.1:8b, gpt-oss:20b, qwen3:32b) and all three missed the price in
"at price 2 dollar per unit", two of them mis-parsed quantity, and one read
"i can supply" as a buy intent -- while taking 7-21 seconds. Numbers, units,
currencies, dates and known city names are exactly what a parser does with
perfect precision and no latency.

"""

import asyncio
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from pydantic import BaseModel, Field

import logging

from models.enums import RFQRole
from services import ai_lexicon, ai_query_parser, learned_vocabulary, query_grounding
from services import currency as currency_service, product_synonyms, unit_converter
from services import locations
from services.locations import City, Location, find_city, find_location
from services.match_scoring import singularise

logger = logging.getLogger(__name__)

# --- seed vocabulary ---------------------------------------------------------
# Everything below is a SEED, not the source of truth: it keeps the offline rule
# parser useful when no model is reachable. The model proposes anything outside
# these lists and ``learned_vocabulary`` extends them at run time.

_SELL_CUES = (
    "i can supply", "i can sell", "i sell", "i am selling", "i'm selling",
    "we supply", "we sell", "we are selling", "i have stock", "i have",
    "we are manufacturers of", "we are manufacturer of", "manufacturer of", "manufacturers of",
    "we manufacture", "we produce", "ready stock", "in stock", "ex-factory", "ex factory",
    "ready to supply", "ready to dispatch",
    "supplying", "offering", "for sale", "i can provide", "i can offer",
    "बेचना", "बेचने", "बेचना चाहता", "बेचना चाहती", "बेचना चाहते", "बेचना है",
    "बिक्री", "सप्लाई", "vender", "venta",
)
_BUY_CUES = (
    "i want", "i need", "looking for", "i am looking", "i'm looking",
    "need to buy", "want to buy", "i buy", "requirement of", "show me", "find",
    "खरीदना", "खरीदने", "खरीदना चाहता", "खरीदना चाहती", "खरीदना चाहते", "खरीदना है",
    "खरीद", "चाहिए", "मंगवाना", "comprar", "compra",
)

# Whole-word cues, matched with word boundaries because short words ("need", "bech")
# would otherwise fire inside other words. Used only by the rule fallback, i.e. when
# the model is unavailable; with a model the role comes from its parse.
_SELL_WORD_CUES = re.compile(
    r"\b(?:selling|we\s+sell|for\s+sale|bech|bechna|bechni|bechenge|bech\s+raha|bech\s+rahe|sell\s+karna|"
    r"supply\s+karenge|maal\s+hai|stock\s+hai)\b"
)
_BUY_WORD_CUES = re.compile(
    r"\b(?:chahiye|kharidna|kharidni|lena\s+hai|required|wanted|need)\b"
)
# "koi jeera bech raha hai?" asks whether ANYONE sells: a buyer's question, not an offer.
_ASKING_ANYONE = re.compile(r"\b(?:koi|kya|kisi|anyone|anybody|who)\b")

_CURRENCIES: dict[str, str] = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR",
    "rupee": "INR", "rupees": "INR", "ruppes": "INR", "rupes": "INR",
    "rupess": "INR", "ruppees": "INR", "rupe": "INR",
    "rupaye": "INR", "rupay": "INR", "rupya": "INR", "rupiya": "INR", "rupaiye": "INR",
    "रुपये": "INR", "रुपया": "INR",
    "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD",
    "buck": "USD", "bucks": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
}

_UNITS = (
    "pcs", "pieces", "piece", "units", "unit", "nos", "no",
    "metric tonnes", "metric tonne", "metric tons", "metric ton",
    "kg", "kgs", "kilogram", "kilograms", "kilo", "kilos",
    "tonne", "tonnes", "ton", "tons", "mt",
    "quintal", "quintals", "qtl",
    "meter", "meters", "metre", "metres", "m",
    "box", "boxes", "carton", "cartons", "roll", "rolls", "set", "sets",
    "tin", "tins", "can", "cans", "bottle", "bottles", "packet", "packets",
    "sqft", "sqm", "cum",
    # Trade packaging units. Without "pallet", "20 pallets with 1000 kg
    # capacity" was read as an order for 1,000 kg.
    "pallet", "pallets", "bag", "bags", "drum", "drums", "bale", "bales",
    "container", "containers", "reel", "reels", "pack", "packs", "crate",
    "crates", "sack", "sacks", "spool", "spools", "sheet", "sheets",
    "bora", "boras", "bori", "boris", "katta", "kattas",
    "litre", "litres", "liter", "liters", "l",
    "lb", "lbs", "pound", "pounds",
)

# Spellings reported under one canonical unit, so "20 metric tons" is 20 tonne
# and "500 kilo" is 500 kg. Other units are returned as typed.
_UNIT_CANONICAL: dict[str, str] = {
    "metric tonnes": "tonne", "metric tonne": "tonne",
    "metric tons": "tonne", "metric ton": "tonne",
    "kilo": "kg", "kilos": "kg",
}

# Units that only ever count things. A leftover "pcs"/"pcses"/"kg" is noise in a
# product name, whereas "box", "carton" and "roll" are products in their own
# right and must survive.
_COUNT_UNITS = frozenset(
    """pc pcs piece pieces unit units no nos each kg kgs kilogram kilograms
    tonne tonnes ton tons mt m meter meters metre metres l litre litres liter
    liters quintal quintals qtl lb lbs bora boras bori boris katta kattas
    kilo kilos metric dozen dozens tin tins can cans bottle bottles packet packets sqft sqm cum""".split()
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
        # Canonical forms of the local names in services/product_synonyms, so
        # "pyaz", "haldi" or "mirchi" are recognised once translated.
        "chilli", "chillies", "chilly", "turmeric", "cumin", "cardamom", "clove",
        "pepper", "coriander", "groundnut", "peanut", "soybean", "mustard", "millet",
        "jaggery", "cashew", "almond", "okra", "brinjal", "cauliflower", "cabbage",
        "pea", "chickpea", "grape", "lemon", "sesame", "lentil", "flour", "apple",
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
    if len(words) <= 2 and any(w in _BROAD_PRODUCTS for w in words):
        return True
    # A family the model flagged ("vegetables", "masala") that the static list lacks.
    return ai_lexicon.is_generic(product) is True

_ORDINALS = frozenset(
    """1st 2nd 3rd 4th 5th 6th 7th 8th 9th 10th 11th 12th 13th 14th 15th
    16th 17th 18th 19th 20th 21st 22nd 23rd 24th 25th 26th 27th 28th 29th 30th 31st
    1'st 2'nd 3'rd 4'th 5'th 6'th 7'th 8'th 9'th 10'th 11'th 12'th 13'th 14'th 15'th
    16'th 17'th 18'th 19'th 20'th 21'st 22'nd 23'rd 24'th 25'th 26'th 27'th 28'th 29'th 30'th 31'st
    st nd rd th""".split()
)

# Stripped from the residual product phrase.
_FILLER = frozenset(
    """metric i we want need looking for buy sell selling buying supply supplying show me
    find get me some any please pls of the a an at in on to from with and or but for is
    are am was were been be will would can could should must my our your it its that this these those now next
    also plus per each price prices priced pricing cost costs costing rate rates
    pay pays paying paid payment budget budgets target targets spend spending spent
    afford offer offering quote quoted quoting charge charges charged
    within before by until due deadline days
    day week weeks month months year years quantity qty around about approx nearly
    roughly upto up maximum minimum min max least most only just very really
    prefer preferably instead rather change switch convert set update make it them one give requirement
    almost exactly total overall
    actually actully actualy basically literally sorry sry apologies excuse
    mistake mistakes mistaken said say saying meant mean meaning typo typos wrong scratch product
    ok okay okey then well alright fine search check rfq yes yeah yep sure k definitely absolutely
    dont don't not didnt didn't wont won't never no nope
    wait waitt hold
    cancel remove drop delete forgetting forget
    just simple plain basic standard regular ordinary
    date dates calendar schedule yesterday today tomorrow tonight
    whole lot lots lump sum altogether entire
    jan jan feb mar apr jun jul aug sep sept oct nov dec
    january february march april may june july august september october november december
    st nd rd th
    rupee rupees ruppes rupes rupess ruppees rupe dollar dollars buck bucks euro euros pound pounds
    currency currencies inr usd eur gbp rs
    unit units pcs piece pieces
    color colour colours shade size under below above over between
    good best cheap cheapest quality
    contain contains cantain cantains containing containig packed packaging packing
    confused decide deciding decision analyze analyse analysis analyzing
    suitable there many how what which where tell telling know market
    seller sellers buyer buyers supplier suppliers vendor vendors listing listings
    detail details info information contact contacts aslo alos too
    mail email emails id ids suit suits suited top two three four five compare comparison
    role draft drafting create creating created raise rais raised suppy suply supplly suplly udpate fron frm
    deliver delivered delivering delevered delivred dispatch dispatched shipped
    delivery deliveries shipping transport logistics list lists
    plan plans planning planned wish wishing aim aiming ready
    bhai mujhe chahiye karna krna hoga karo mera meri hum hume ka ki ke ko se me mein
    main mai bechna kharidna khareedna chahta chahti chahte hoon hu hun hai hain tha the thi
    quiero comprar vender de para un una unos unas por con el la los las
    procurement procure procuring category categories typical condition conditions viable viability fair high cheap expensive quote quotes quoting does""".split()
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
_PRICE_NUMBER = r"\d[\d,]*(?:\.\d+)?(?:k|m)?"

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
    # Enhancement #3: Quantity range support ("500-1000 kg")
    min_quantity: Optional[Decimal] = None
    max_quantity: Optional[Decimal] = None

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

    # The price is the WHOLE order's value ("total budget 1.5 crore"), not a per-unit price.
    price_is_total: bool = False
    # Things the message said that cannot be stored as they are (a negative quantity, an impossible date, a second
    # product ...): the chat explains them and asks, it never stores a guess. See ``_sanity_flags``.
    flags: dict[str, Any] = Field(default_factory=dict)

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
        s = raw.replace(",", "").strip().lower()
        if s.endswith("k"):
            return Decimal(s[:-1]) * Decimal("1000")
        if s.endswith("m"):
            return Decimal(s[:-1]) * Decimal("1000000")
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _normalise_currency(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    cleaned = token.strip().lower().rstrip(".")
    if cleaned in ("रुपये", "रुपया", "rupee", "rupees", "ruppes", "rupess", "rupe"):
        return "INR"
    if cleaned in ("dollar", "dollars", "dolar", "dolars"):
        return "USD"
    norm = currency_service.normalize_currency(cleaned)
    if norm:
        return norm
    if cleaned in _CURRENCIES:
        return _CURRENCIES[cleaned]
    return None


_CURRENCY_SYMBOLS = (
    r"₹|\$|€|£|¥|₩|c\$|a\$|rs\.?|inr|usd|eur|gbp|jpy|cny|rmb|cad|aud|aed|chf|sek|nok|dkk|krw|sgd|"
    r"(?:india[n]?\s+)?(?:rupaye?|rup+[e|p]*s*)|रुपये|रुपया|(?:u\.?s\.?\s+)?(?:dollars?|dol+ars?|bucks?)|"
    r"euros?|pounds?|(?:swedish\s+)?krona|(?:norwegian\s+)?krone|kronor|kroner|kr|"
    r"dirhams?|yen|yuan|renminbi|won|francs?"
)



def _extract_price(text: str) -> tuple[Optional[Decimal], Optional[str], Optional[str], str]:
    """Price, currency and the unit it is quoted per; plus the text with it removed.

    Removing what it consumes is what stops "2 dollar per unit" from also being
    read as a quantity of 2.
    """
    symbols = _CURRENCY_SYMBOLS
    # "per kg", "/kg", "a kilo" and, right after a currency, a bare unit word: "12 rs kilo".
    per = (
        rf"(?:(?:\s*(?:per\b|par\b|/|\ba\b|प्रति)\s*|\s+(?=(?:kg|kgs|kilo|kilos|kilogram|quintal|quintals|qtl|ton|tons|tonne|tonnes|"
        rf"pcs|pc|piece|pieces|meter|meters|litre|litres|liter|liters)\b))(?P<unit>[a-z\u0900-\u097f]+))?"
    )
    known_units = (
        r"kg|kgs|kilo|kilos|kilogram|kilograms|quintal|quintals|qtl|ton|tons|tonne|tonnes|mt|g|gm|gram|grams|"
        r"pcs|pc|piece|pieces|unit|units|dozen|bora|boras|katta|bag|bags|meter|meters|m|l|liter|liters|litre|litres|"
        r"क्विंटल|किलो|टन|ग्राम|मीटर|लीटर|दर्जन|पीस|बोरी|बोरा|कट्टा"
    )

    patterns = (
        # "₹200/kg", "rs 200 per kg", "$2 per unit", "inr 210/unit"
        rf"(?<![a-z])(?P<cur>{symbols})\s*(?P<amt>{_PRICE_NUMBER}){per}",
        # "200 rupees per kg", "2 dollar per unit", "210 ruppes per unit"
        rf"(?P<amt>{_PRICE_NUMBER})\s*(?P<cur>{symbols}){per}",
        # "210/- per unit" (Indian currency notation)
        rf"(?P<amt>{_PRICE_NUMBER})\s*(?P<cur>/-){per}",
        # "at price 200 per kg", "can pay 210 per unit", "budget 200/kg", "pay 210"
        rf"\b(?:at|for|price|rate|budget|pay|paying|target|upto|up\s+to|max|around)\s+(?:of\s+)?(?P<amt>{_PRICE_NUMBER}){per}",
        # Bare price per unit: "1000/quintal", "15/kg", "1.5/kg", "1000 per quintal", "1.5k/quintal"
        rf"(?:^|\s)(?P<amt>{_PRICE_NUMBER})\s*(?:/|per\b|par\b|प्रति)\s*(?P<unit>{known_units})\b",
    )

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        amount = _to_decimal(match.group("amt"))
        if amount is None or amount <= 0:
            continue
        cur_raw = match.groupdict().get("cur")
        if cur_raw == "/-":
            currency = "INR"
        elif cur_raw:
            currency = _normalise_currency(cur_raw)
        else:
            currency = "INR"
        raw_unit = match.groupdict().get("unit")
        unit = unit_converter.clean_unit(raw_unit) if raw_unit else None
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
        rf"(?:within|in|after|next)\s+(?:\d+\s*(?:-|to)\s*)?(?P<n>\d+)\s*(?P<unit>day|days|week|weeks|month|months)",
        rf"(?:\d+\s*(?:-|to)\s*)?(?P<n>\d+)\s*(?P<unit>day|days|week|weeks|month|months)\s*(?:deadline|delivery)?",
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
    if calendar is not None and span is not None and re.match(rf"\s*(?:{_unit_alternation()})\b", text[span[1]:], re.IGNORECASE):
        calendar = None  # "1.5 tonnes" is a size, not the 1st of May
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

# e.g. "each box is 5 dozen", "each carton has 24 pieces", "12 pcs per box": how MANY units one pack holds
_PACK_CONTENT_RE = re.compile(
    r"\b(?:(?:each|every|per)\s+(?P<p1>[a-z]+)\s+(?:is|has|have|contains?|holds?|of|with|=|:)?\s*(?P<n1>\d[\d,]*(?:\.\d+)?)\s*(?P<u1>dozens?|doz|pcs|pieces?|units?|nos|bottles?|packets?)"
    r"|(?P<n2>\d[\d,]*(?:\.\d+)?)\s*(?P<u2>dozens?|doz|pcs|pieces?|units?|nos|bottles?|packets?)\s*(?:in\s+each|per|in\s+every|each)\s+(?P<p2>[a-z]+))\b",
    re.IGNORECASE,
)

# e.g. "in bora pcs", "packed in bags", "packaging in sacks"
_PACKAGING_PREF_RE = re.compile(
    r"\b(?:in|packed\s+in|packaging\s*(?:in|:)?)\s+(?P<pack_pref>bora|boras|bori|boris|katta|kattas|gunny\s+bags?|bag|bags|sack|sacks|bale|bales|carton|cartons|box|boxes|crate|crates|drum|drums|pallet|pallets|pack|packs)(?:\s+pcs)?\b",
    re.IGNORECASE,
)


def _extract_pack_contents(text: str) -> tuple[Optional[str], Optional[str], str]:
    """"each box is 5 dozen" -> ("5 dozen", "box", text without it): what one pack holds, never the order quantity."""
    m = _PACK_CONTENT_RE.search(text)
    if not m:
        return None, None, text
    holder = (m.group("p1") or m.group("p2") or "").lower()
    if holder not in _PACKAGING_UNITS_SET and holder not in _CANONICAL_PACKAGING:
        return None, None, text
    n = (m.group("n1") or m.group("n2")).replace(",", "")
    u = (m.group("u1") or m.group("u2") or "").lower()
    u = "dozen" if u.startswith("doz") else u
    return f"{n} {u}", _CANONICAL_PACKAGING.get(holder, holder), text[: m.start()] + " " + text[m.end():]


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


def _all_units() -> tuple[str, ...]:
    """Seed units plus units the model taught us."""
    learned = [u for u in learned_vocabulary.known_units() if u not in _UNITS]
    return (*_UNITS, *learned)


def _unit_alternation() -> str:
    return "|".join(
        re.escape(u).replace(r"\ ", r"\s+") for u in sorted(_all_units(), key=len, reverse=True)
    )


def _canonical_unit(raw: str) -> str:
    unit = " ".join(raw.lower().split())
    return _UNIT_CANONICAL.get(unit, unit)


# Indian and shorthand multipliers. "5 lakh" is 500,000 and "2 crore" is
# 20,000,000; without this the number was read as 5 and "lakh" became part of
# the product name.
_MULTIPLIERS: dict[str, Decimal] = {
    "lakh": Decimal(100000), "lakhs": Decimal(100000), "lac": Decimal(100000),
    "lacs": Decimal(100000), "crore": Decimal(10000000), "crores": Decimal(10000000),
    "cr": Decimal(10000000), "thousand": Decimal(1000), "million": Decimal(1000000),
}
_MULTIPLIER_RE = re.compile(
    rf"(?P<n>{_NUMBER})\s*(?P<m>lakhs?|lacs?|crores?|cr|thousand|million)\b",
    re.IGNORECASE,
)
_DOZEN_RE = re.compile(rf"(?P<n>{_NUMBER})\s*(?:dozens?|doz)\b", re.IGNORECASE)
# "half a tonne", "quarter tonne", Hindi "dedh quintal" (1.5), "dhai tonne" (2.5): only before a unit, so
# "half sleeve" or "dedh" in a product name is left alone.
_FRACTIONS = {"half": "0.5", "quarter": "0.25", "dedh": "1.5", "derh": "1.5", "dhai": "2.5", "dhaai": "2.5", "adha": "0.5", "aadha": "0.5"}
_FRACTION_RE = re.compile(
    r"\b(?P<f>half|quarter|dedh|derh|dhai|dhaai|adha|aadha)\s+(?:(?:a|an|of\s+a)\s+)?(?=(?:tonnes?|tons?|mt|quintals?|qtl|kgs?|kilos?|kilograms?|litres?|liters?|dozens?)\b)",
    re.IGNORECASE,
)
_CURRENCY_BEFORE_K = r"(?:₹|\$|€|£|rs\.?|inr|usd|eur|gbp)"


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def _expand_multipliers(text: str) -> str:
    """Rewrite multiplier shorthand as plain numbers before anything is parsed.

    "5 lakh pcs" -> "500000 pcs", "2 crore" -> "20000000", "1.5k pcs" ->
    "1500 pcs", "₹2k" -> "₹2000", "2 dozen chairs" -> "24 pcs chairs".
    A bare "k" is only expanded before a unit or after a currency, so "8k"
    in "hdmi 8k cable" stays a resolution.
    """
    if not text:
        return text

    def mult(match: re.Match) -> str:
        number = _to_decimal(match.group("n"))
        factor = _MULTIPLIERS.get(match.group("m").lower())
        if number is None or factor is None:
            return match.group(0)
        return _plain(number * factor)

    def dozen(match: re.Match) -> str:
        number = _to_decimal(match.group("n"))
        if number is None:
            return match.group(0)
        return _plain(number * 12)

    def fraction(match: re.Match) -> str:
        return _FRACTIONS[match.group("f").lower()] + " "

    def thousands(match: re.Match) -> str:
        number = _to_decimal(match.group("n"))
        if number is None:
            return match.group(0)
        return f"{match.group('pre') or ''}{_plain(number * 1000)}"

    out = _FRACTION_RE.sub(fraction, text)
    out = _MULTIPLIER_RE.sub(mult, out)
    out = _DOZEN_RE.sub(dozen, out)
    out = re.sub(
        rf"(?P<pre>)(?P<n>\d+(?:\.\d+)?)\s?k\b(?=\s*(?:{_unit_alternation()})\b)",
        thousands, out, flags=re.IGNORECASE,
    )
    out = re.sub(
        rf"(?P<pre>{_CURRENCY_BEFORE_K}\s*)(?P<n>\d+(?:\.\d+)?)\s?k\b",
        thousands, out, flags=re.IGNORECASE,
    )
    return out


# Enhancement #3: Quantity range parsing ("500-1000 kg", "between 5 and 10 tonnes")
_RANGE_PATTERNS = (
    # "500-1000 kg", "500 - 1000 kg", "500~1000 kg"
    rf"(?P<min>{_NUMBER})\s*(?:-|–|~|to)\s*(?P<max>{_NUMBER})\s*(?P<unit>{{unit_alt}})\b",
    # "between 5 and 10 tonnes"
    rf"between\s+(?P<min>{_NUMBER})\s+and\s+(?P<max>{_NUMBER})\s*(?P<unit>{{unit_alt}})\b",
    # "500 to 1000 kg"
    rf"(?P<min>{_NUMBER})\s+to\s+(?P<max>{_NUMBER})\s*(?P<unit>{{unit_alt}})\b",
)


def _extract_quantity_range(
    text: str,
) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[str], str]:
    """Extract quantity range (min, max) from text.

    Returns (quantity_value, min_quantity, max_quantity, unit, cleaned_text).
    quantity_value is set to min_quantity for backward compatibility.
    """
    unit_alt = _unit_alternation()
    for pattern_template in _RANGE_PATTERNS:
        pattern = pattern_template.format(unit_alt=unit_alt)
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            min_val = _to_decimal(match.group("min"))
            max_val = _to_decimal(match.group("max"))
            unit = _canonical_unit(match.group("unit"))
            cleaned = text[: match.start()] + " " + text[match.end() :]
            if min_val is not None and max_val is not None:
                if min_val > max_val:
                    min_val, max_val = max_val, min_val
                return min_val, min_val, max_val, unit, cleaned
    return None, None, None, None, text


def _extend_compound(
    text: str, match: "re.Match[str]", value: Optional[Decimal], unit: str
) -> tuple[Optional[Decimal], int]:
    """"5 ton 200 kg": a smaller unit of the same kind right after the quantity adds to it (5.2 ton).

    Returns the summed value (in the first unit) and the end of the text consumed."""
    if value is None:
        return value, match.end()
    base = unit_converter.base_unit(unit)
    if base is None:
        return value, match.end()
    unit_alt = _unit_alternation()
    end, total = match.end(), value
    while True:
        nxt = re.match(
            rf"\s*(?:and\s+|&\s*|\+\s*|,\s*)?(?P<amt>{_NUMBER})\s*(?P<unit>{unit_alt})\b", text[end:], flags=re.IGNORECASE
        )
        if not nxt:
            break
        small = unit_converter.base_unit(_canonical_unit(nxt.group("unit")))
        amount = _to_decimal(nxt.group("amt"))
        if small is None or amount is None or small[1] != base[1] or small[0] >= base[0]:
            break
        total += amount * small[0] / base[0]
        end += nxt.end()
    return total.quantize(Decimal("0.000001")).normalize() if total != value else value, end


def _extract_quantity(text: str) -> tuple[Optional[Decimal], Optional[str], str]:
    value, unit, cleaned, _compound = _extract_quantity_ex(text)
    return value, unit, cleaned


def _extract_quantity_ex(text: str) -> tuple[Optional[Decimal], Optional[str], str, bool]:
    """Like ``_extract_quantity``, plus whether a compound ("5 ton 200 kg") was summed."""
    unit_alt = _unit_alternation()
    match = re.search(rf"(?P<amt>{_NUMBER})\s*(?P<unit>{unit_alt})\b", text, flags=re.IGNORECASE)
    if match:
        value = _to_decimal(match.group("amt"))
        unit = _canonical_unit(match.group("unit"))
        summed, end = _extend_compound(text, match, value, unit)
        cleaned = text[: match.start()] + " " + text[end:]
        return summed, unit, cleaned, summed != value

    # A bare number, once price and deadline have already been consumed --
    # skipping any that is really a specification.
    #
    # "i want a 20000 mah power bank" used to set the order quantity to 20,000
    # as well as the capacity, so the assistant never asked how many the user
    # actually wanted and searched with a quantity nobody had given it.
    for match in re.finditer(rf"\b(?P<amt>{_NUMBER})\b", text):
        if _SPEC_SUFFIX.match(text, match.end()):
            continue
        amt_str = match.group("amt")
        # Ignore 4-digit calendar years (e.g. 2024, 2025, 2026)
        if re.match(r"^(?:19|20)\d{2}$", amt_str):
            continue
        # Ignore numbers followed by duration/deadline ranges (e.g. "within 10-15 days", "10 days")
        if re.match(r"^\s*(?:-\s*\d+)?\s*(?:days?|weeks?|months?|years?|hours?)\b", text[match.end():], re.IGNORECASE):
            continue
        value = _to_decimal(amt_str)
        if value is not None and value >= 10:
            cleaned = text[: match.start()] + " " + text[match.end() :]
            return value, None, cleaned, False

    return None, None, text, False


# "supply" as people actually type it in a chat box.
_SUPPLY_VERB = r"(?:supply|suppy|suply|supplly|suplly|sypply|supplie)"


def _extract_role(text: str) -> Optional[RFQRole]:
    lowered = f" {text.lower()} "

    # 0. Explicit role switches: "update the rfq from buyer to seller", "actually the seller not buyer".
    switch = re.search(
        r"\b(?:from|fron|frm)\s+(?:a\s+)?(buyer|seller|supplier)\s+to\s+(?:a\s+)?(buyer|seller|supplier)\b"
        r"|\b(?:change|switch|convert|make|set|udpate|update)\b[^.?!\n]{0,30}\bto\s+(?:a\s+)?(buyer|seller|supplier|selling|buying)\b"
        r"|\b(?:make|mark|set)\s+it\s+(?:an?\s+)?(buyer|seller|supplier|sell|buy)\w*\s+(?:rfq|listing|post)\b"
        r"|\b(?:i\s+am|im|i'm)?\s*(?:actually\s+)?(?:a\s+|the\s+)?(seller|buyer|supplier)\s+(?:not|not\s+a)\s+(buyer|seller)\b",
        lowered,
    )
    if switch:
        target = switch.group(2) or switch.group(3) or switch.group(4) or switch.group(5)
        target = {"sell": "seller", "selling": "seller", "buy": "buyer", "buying": "buyer"}.get(target, target)
        return RFQRole.BUYER if target == "buyer" else RFQRole.SELLER

    # 1. High-confidence compound phrases first (overrides standalone "i want" or "looking")
    compound_sell = [
        # "buy the basmati rice from us" is a seller pitching to a buyer.
        r"\bbuy\b[^.?!\n]{0,60}\bfrom\s+(?:us|me|our)\b",
        rf"\b(?:for|to)\s+(?:the\s+)?{_SUPPLY_VERB}\b",
        rf"\b(?:i\s+)?(?:want|looking|plan|planning|ready|wish|wishing|aim|aiming)\s+to\s+(?:{_SUPPLY_VERB}|sell|offer|provide)\b",
        rf"\b(?:i\s+)?(?:can|could|will|would)\s+(?:{_SUPPLY_VERB}|sell|offer|provide)\b",
        r"\b(?:i\s+)?(?:am|'m|we\s+are|we're)\s+(?:selling|supplying|offering|providing)\b",
        r"\b(?:i\s+)?(?:am|'m|we\s+are|we're)\s+(?:actually\s+|now\s+)?(?:a\s+|the\s+)?(?:supplier|seller|vendor|exporter|manufacturer|wholesaler|producer)s?\b",
        r"\b(?:as\s+a\s+)?(?:supplier|seller|vendor|exporter|manufacturer|wholesaler)s?\b[^.?!\n]{0,50}\b(?:searching|looking)\s+for\s+buyers?\b",
        r"\b(?:have|got|with)\s+(?:ready\s+)?(?:stock|inventory|supplies|availability)\b",
        r"\bready\s+stock\b",
        r"\bex[- ]factory\b",
        r"\bwe\s+(?:manufacture|produce|fabricate)\b",
        r"\b(?:for\s+sale|to\s+sell|to\s+supply)\b",
        r"\b(?:supply|selling)\s+of\b",
        r"\b(?:bechna|bechne|vikreta)\b",
        r"(?:बेचना|बेचने|विक्रेता|बिक्री|सप्लाई)",
        r"\b(?:vender|venta|suministrar)\b",
    ]
    compound_buy = [
        r"\b(?:i\s+)?(?:want|looking|plan|planning|ready|wish|wishing|aim|aiming)\s+to\s+(?:buy|purchase|procure|order)\b",
        r"\b(?:i\s+)?(?:can|could|will|would)\s+(?:buy|purchase|procure)\b",
        r"\b(?:i\s+)?(?:am|'m|we\s+are|we're)\s+(?:buying|purchasing|procuring)\b",
        r"\b(?:i\s+)?(?:am|'m|we\s+are|we're)\s+(?:actually\s+|now\s+)?(?:a\s+|the\s+)?(?:buyer|purchaser)s?\b",
        r"\b(?:need|require|seeking|looking\s+for|in\s+search\s+of)\b",
        r"\b(?:requirement\s+of|quote\s+for|rfq\s+for)\b",
        r"\b(?:khareedna|khareedne|chahiye|mangwana)\b",
        r"(?:खरीदना|खरीदने|खरीद|चाहिए|मंगवाना)",
        r"\b(?:comprar|compra|adquirir)\b",
    ]

    for pat in compound_sell:
        if re.search(pat, lowered):
            return RFQRole.SELLER
    for pat in compound_buy:
        if re.search(pat, lowered):
            return RFQRole.BUYER

    # 1b. Phrases the model taught us ("we've got ... lying in our godown").
    learned_role = learned_vocabulary.role_for(text)
    if learned_role is not None:
        return learned_role

    # 2. General cues fallback
    sell_hits = [lowered.find(cue) for cue in _SELL_CUES if cue in lowered]
    buy_hits = [lowered.find(cue) for cue in _BUY_CUES if cue in lowered]
    asking = "?" in lowered and _ASKING_ANYONE.search(lowered) is not None
    sell_word = _SELL_WORD_CUES.search(lowered)
    if sell_word and not asking:
        sell_hits.append(sell_word.start())
    buy_word = _BUY_WORD_CUES.search(lowered)
    if buy_word:
        buy_hits.append(buy_word.start())
    # The earliest cue of either kind decides.
    sell_at = min(sell_hits, default=-1)
    buy_at = min(buy_hits, default=-1)

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

    # Values the model has met before are HINTS for the model (see ``extract_async``), never rules that write a
    # field here: "5" once learned as a ply count must not turn up on wheat.

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
    _, min_q, max_q, _, without_quantity = _extract_quantity_range(_strip_grades(text))
    if min_q is None:
        _, _, without_quantity = _extract_quantity(_strip_grades(text))
    attributes.update(_extract_measurements(without_quantity.lower()))
    return attributes


def _infer_category(text: str) -> Optional[str]:
    learned = learned_vocabulary.category_for(text)
    if learned:
        return learned
    # Local names first: "pyaz" is an onion, "haldi" turmeric.
    lowered = product_synonyms.canonicalise_text(text)
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
    raw_text = text

    # If the user put the title/product in quotes, e.g. "Procurement of 50,000 pcs Appy Fizz in Indore"
    m_quoted = re.search(r'["\']([^"\']{3,120})["\']', raw_text)
    if m_quoted:
        candidate_quote = m_quoted.group(1).strip()
        # Ensure the quote is not just a currency or category name
        if not re.search(r"^(?:inr|rs|usd|eur|food\s*&\s*beverages|agriculture|chemicals|construction|textiles|plastics)$", candidate_quote, re.IGNORECASE):
            raw_text = candidate_quote

    lowered = raw_text.lower()
    lowered = re.sub(r"\b(?:pan|all)[\s-]+india\b", " ", lowered)

    # Strip leading analytical/prompt scaffolding
    lowered = re.sub(
        r"^(?:\s*(?:analyze|analyse|evaluate|check|assess|compare)\s+(?:this\s+)?(?:buyer|seller\s+)?(?:listing|quote|rfq|rate|price|offer)\s+(?:for\s+)?)+",
        " ",
        lowered,
        flags=re.IGNORECASE,
    )
    lowered = re.sub(
        r"^(?:\s*(?:evaluate|check|assess)\s+commercial\s+viability\s+(?:of\s+)?)+",
        " ",
        lowered,
        flags=re.IGNORECASE,
    )

    # 1. Strip secondary conversational/analytical/question clauses so doubt questions
    # do not pollute the core commodity noun phrase (e.g. "but i am confused what price...")
    clause_splits = re.split(
        r"\b(?:but|however|although|though|can you|could you|please tell|how many|what is|what are|what price|what should|tell me|analyze|analyse|help me decide|am confused|confused about|how does this compare|how does it compare|is this price fair|is this fair)\b",
        lowered,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    if clause_splits and clause_splits[0].strip():
        if len(clause_splits) > 1 and re.search(r"\b(?:mistake|typo|wrong|said|thought|meant|earlier)\b", clause_splits[0], re.IGNORECASE):
            lowered = clause_splits[1]
        else:
            lowered = clause_splits[0]

    # Strip prompt & transactional scaffolding words
    _SCAFFOLDING = (
        r"\b(?:procurement|procure|procuring|buyer|seller|listing|listings|rfq|rfqs|"
        r"analyze|analyse|analysis|evaluating|evaluate|quote|quotes|quoting|"
        r"typical|condition|conditions|viable|viability|commercial|commercials|"
        r"fair|high|cheap|expensive|compare|comparison|budget|budgets|"
        r"category|categories|specification|specifications|draft|drafting|"
        r"manufacturers?|producer|producers|factory|factories|available|stock|ready|"
        r"bhav|daam|dar|rate|rates|ke\s+paas|paas|ke\s+andar|andar|chahiye|mujhe|hamen|humein|"
        r"around|near|within|under|below|approx|approximately)\b"
    )
    lowered = re.sub(_SCAFFOLDING, " ", lowered, flags=re.IGNORECASE)

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

    # Dynamically strip all known cities from gazetteer so location names never form product names
    from services.locations import CITIES
    city_regex = r"\b(?:" + "|".join(re.escape(k) for k in CITIES.keys()) + r")\b"
    lowered = re.sub(city_regex, " ", lowered)
    lowered = re.sub(r"\b(?:pan|all)[\s-]+india\b", " ", lowered, flags=re.IGNORECASE)

    # Strip common states, regions and country markers so they never become a product name
    _GEO_WORDS = (
        r"haryana|hariyana|haryanvi|punjab|panjab|maharashtra|gujarat|gujrat|rajasthan|"
        r"karnataka|kerala|bihar|assam|odisha|orissa|delhi|chandigarh|kolkata|"
        r"mumbai|pune|bhopal|indore|bangalore|bengaluru|chennai|hyderabad|"
        r"india|china|germany"
    )
    lowered = re.sub(rf"\b(?:{_GEO_WORDS})\b", " ", lowered)

    # Dynamic prepositional location expressions: e.g. "in Bhiwandi", "at Surat", "near Thane"
    def _strip_prep_loc(m: re.Match) -> str:
        word = m.group(1).lower()
        if word in {"bulk", "stock", "boxes", "box", "bags", "bag", "pieces", "pcs", "roll", "rolls", "transit", "advance", "cash", "credit", "full", "part", "good", "new", "fresh", "sealed", "loose"}:
            return m.group(0)
        return " "

    lowered = re.sub(r"\b(?:in|at|near|around)\s+([a-zA-Z]+)\b", _strip_prep_loc, lowered)
    lowered = re.sub(r"\btype\s+c\b", "type-c", lowered)

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
    r"\b(?:i\s+)?(?:don't|dont|do\s+not|didnt|didn't|wont|won't)\s+(?:need|want|require|ask\s+for)\s+(?P<neg>[^,;.\n]+?)(?=(?:i\s+(?:need|want|require|prefer)|but|instead|rather|make\s+it|it\'?s|its|is|actually|meant|mean|now|chahiye|mujhe|,|;|\.|$))",
    # e.g. "not type c", "not white", "no type c", "without type c", "free from type c"
    r"\b(?:not|no|without|free\s+from|except|excluding)\s+(?P<neg>[a-z0-9\s\-]+?)(?=(?:i\s+(?:need|want|require|prefer)|but|instead|rather|make\s+it|it\'?s|its|is|actually|meant|mean|now|chahiye|mujhe|,|;|\.|$))",
    # e.g. "remove type c", "drop type c", "cancel type c"
    r"\b(?:remove|drop|cancel|delete|forget)\s+(?:the\s+)?(?P<neg>[a-z0-9\s\-]+?)(?=(?:i\s+(?:need|want|require|prefer)|but|instead|rather|make\s+it|it\'?s|its|is|actually|meant|mean|now|chahiye|mujhe|,|;|\.|$))",
)
# Verbs of the 3rd pattern are commands; their object is negated only when it names a product property we can
# recognise ("remove type c"), never an arbitrary word ("cancel everything", "drop table rfqs").
_COMMAND_NEGATION_VERBS = frozenset({"remove", "drop", "cancel", "delete", "forget"})
# Words that can follow "no / not / without" without being a property of the goods.
_NOT_A_PROPERTY = learned_vocabulary._STOPWORDS | frozenset({
    "need", "wait", "problem", "worry", "doubt", "idea", "time", "way", "one", "more", "less", "other", "else", "such",
    "questions", "question", "details", "detail", "table", "rfq", "rfqs", "draft", "listing", "order", "orders",
})

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
            is_command = match.group(0).split()[0].lower() in _COMMAND_NEGATION_VERBS
            attrs = _extract_attributes(neg_span)
            for k, v in attrs.items():
                negated_keys.add(k)
                if isinstance(v, str):
                    negated_keys.add(v.lower())
            lowered_span = neg_span.lower()
            if re.search(r"\btype\s*-?\s*c\b", lowered_span):
                negated_keys.add("type")
            if re.search(r"\b(?:color|colour)\b", lowered_span):
                negated_keys.add("color")
            if re.search(r"\bgrade\b", lowered_span):
                negated_keys.add("grade")
            if re.search(r"\bmaterial\b", lowered_span):
                negated_keys.add("material")
            _neg_exclude = {
                "the", "any", "and", "with", "from", "for", "buy", "sell", "buyer",
                "seller", "buying", "selling", "purchaser", "vendor", "supplier",
                "purchase", "it", "this", "that", "them", "want", "need", "have",
                "order", "rfq", "change", "make", "wait", "not", "dont",
                "used", "needed", "wanted", "required", "added", "given", "allowed", "applied",
            }
            for word in lowered_span.split():
                clean_w = word.strip(".,;:!?\"'")
                if is_command and not attrs:
                    continue  # "cancel everything" is a command, not a rejected property
                if (
                    len(clean_w) > 2 and clean_w not in _neg_exclude and clean_w not in _NOT_A_PROPERTY
                    and not clean_w.endswith("ing") and clean_w.isalpha() and len(lowered_span.split()) <= 3
                ):
                    negated_keys.add(clean_w)

            start, end = match.span()
            cleaned = cleaned[:start] + " " * (end - start) + cleaned[end:]

    return negated_keys, cleaned


# "10 tonnes, no wait make it 12 tonnes": the user's last stated quantity is the one that counts.
_CORRECTION_RE = re.compile(
    r"\b(?:no[,.]?\s+wait|wait[,.]?\s+no|actually|change\s+(?:it\s+|that\s+|the\s+quantity\s+)?to|make\s+(?:it|that)|"
    r"i\s+mean|scratch\s+that|sorry[,.]?|correction|nahi[,.]?\s+(?=\d)|nahin[,.]?\s+(?=\d))",
    re.IGNORECASE,
)


def _corrected_quantity(text: str) -> tuple[Optional[Decimal], Optional[str]]:
    """The quantity stated after the last correction word, if the text has one."""
    last = None
    for last in _CORRECTION_RE.finditer(text):
        pass
    if last is None:
        return None, None
    value, unit, _ = _extract_quantity(_strip_grades(_expand_multipliers(text[last.end():])))
    return value, unit


_RETRACTION_RE = re.compile(
    r"\b(?:by\s+mistake|mistakenly|sorry|typo|wrongly)?\s*(?:i\s+)?(?:said|wrote|typed|told|mentioned|put|asked\s+for)\s+([a-zA-Z0-9\s-]+?)(?=\s*(?:but|however|instead|now|its|it'?s|is|actually|,|;|\.|$))",
    re.IGNORECASE,
)


def _rule_extract(message: str) -> Requirements:
    """Rule-based parse of one message: the offline fallback and mechanical helper."""
    requirements = Requirements()

    role = _extract_role(message)
    if role is not None:
        requirements.role = role

    # 0. Blank out apologies and retractions ("by mistake i said apple but ...")
    clean_msg = message
    m_retract = _RETRACTION_RE.search(message)
    if m_retract:
        clean_msg = message[: m_retract.start()] + " " * (m_retract.end() - m_retract.start()) + message[m_retract.end() :]

    # 1. Negation detection: extract negated attributes and blank out negated clauses
    negated_attrs, positive_text = _detect_negations(clean_msg)
    requirements.negated_attributes = negated_attrs
    pack_contents, contents_holder, positive_text = _extract_pack_contents(positive_text)
    working = _expand_multipliers(positive_text)

    # Explicit category extraction from message (e.g. in category "Food & Beverages")
    cat_match = re.search(r'\b(?:in\s+)?category\s*[:"]?\s*["\']?([^"\'\n,;]+)["\']?', working, re.IGNORECASE)
    if cat_match:
        cat_candidate = cat_match.group(1).strip()
        working = working[: cat_match.start()] + " " + working[cat_match.end() :]
        for standard_cat in learned_vocabulary.known_categories():
            if cat_candidate.lower() in standard_cat.lower() or standard_cat.lower() in cat_candidate.lower():
                requirements.category = standard_cat
                break

    price, currency, per_unit, working = _extract_price(working)
    if price is None:
        standalone_currency, working = _extract_standalone_currency(working)
        if standalone_currency is not None:
            currency = standalone_currency
    deadline_days, working = _extract_deadline(working)

    # Packaging & compound weight extraction
    pack_count, pack_unit, pack_attrs, working = _extract_compound_packaging(working)

    # Enhancement #3: Quantity range parsing ("500-1000 kg", "between 5 and 10 tonnes")
    range_qty, min_q, max_q, range_unit, working_range = _extract_quantity_range(_strip_grades(working))
    if min_q is not None and max_q is not None:
        quantity = range_qty
        unit = range_unit
        working = working_range
        requirements.min_quantity = min_q
        requirements.max_quantity = max_q
    else:
        quantity, unit, working, compound = _extract_quantity_ex(_strip_grades(working))
        if compound:
            requirements.flags["quantity_compound"] = True

    fixed_qty, fixed_unit = _corrected_quantity(positive_text)
    if fixed_qty is not None:
        quantity, unit, pack_count = fixed_qty, fixed_unit or unit, None
        requirements.min_quantity = requirements.max_quantity = None
    # An absurd size is not accepted silently: with no quantity the conversation asks how much is wanted.
    if quantity is not None and query_grounding.quantity_is_absurd(quantity, pack_unit or unit):
        quantity = None
    if pack_count is not None and query_grounding.quantity_is_absurd(pack_count, pack_unit or unit):
        pack_count = None

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
    # (the price is cut out first: "rs 15 kg" is a rate, not a 15 kg capacity)
    extracted_attrs = _extract_attributes(_extract_price(_expand_multipliers(positive_text))[3])
    for k in negated_attrs:
        extracted_attrs.pop(k, None)
    requirements.attributes = extracted_attrs
    if pack_attrs:
        requirements.attributes.update(pack_attrs)
    if pack_contents:
        requirements.attributes["pack_contents"] = pack_contents
        requirements.attributes.setdefault("packaging", contents_holder)
    if not requirements.category:
        requirements.category = _infer_category(working)
    prod = _extract_product(working, requirements.attributes, location=loc)
    if prod:
        # Enhancement #1: Phonetic + fuzzy typo recovery for product name
        # If the word is already a known English plural or noun, preserve user phrasing
        from services.match_scoring import singularise
        is_known = (
            singularise(prod) in product_synonyms._GROUPS
            or prod.rstrip("s") in product_synonyms._GROUPS
            or prod.rstrip("es") in product_synonyms._GROUPS
        )
        if not is_known:
            norm = product_synonyms.canonical_token(prod) or product_synonyms.fuzzy_match(prod)
            if norm:
                prod = norm
            else:
                tokens = prod.split()
                corrected = []
                for t in tokens:
                    t_known = (
                        singularise(t) in product_synonyms._GROUPS
                        or t.rstrip("s") in product_synonyms._GROUPS
                        or t.rstrip("es") in product_synonyms._GROUPS
                    )
                    c = None if t_known else (product_synonyms.canonical_token(t) or product_synonyms.fuzzy_match(t))
                    corrected.append(c if c else t)
                prod = " ".join(corrected)
    requirements.product = prod

    return requirements




# --- sanity of what was read -------------------------------------------------------------------------------------
# Nothing below guesses a replacement: a value that cannot be real is removed and named in ``Requirements.flags`` so
# the chat can say why and ask again.

_NEGATIVE_QTY_RE = re.compile(r"(?<![\w.\-])(?:-(?=\d)|minus\s+)(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?P<u>{unit_alt})\b", re.IGNORECASE)
_ZERO_QTY_RE = re.compile(r"(?<![\d.,])(?P<n>0+(?:\.0*)?)\s*(?P<u>{unit_alt})\b", re.IGNORECASE)
_ANY_QTY_RE = re.compile(r"(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?P<u>{unit_alt})\b", re.IGNORECASE)
_PAST_WORDS_RE = re.compile(
    r"\b(?:yesterday|day\s+before\s+yesterday|last\s+(?:week|month|year|night)|(?:\d+|a|an|one|two|three)\s+(?:days?|weeks?|months?|years?)\s+ago)\b",
    re.IGNORECASE,
)
# Price bounds per BASE unit (kg, piece, litre, metre), in any currency: no real trade is below one hundredth or above
# a hundred million of a currency unit per kg or per piece.
_MIN_PRICE_PER_BASE = Decimal("0.01")
_MAX_PRICE_PER_BASE = Decimal("100000000")


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


def date_issues(text: str, today: Optional[date] = None) -> list[tuple[str, str]]:
    """Dates in ``text`` that cannot be a delivery date: ("invalid", "31 february") or ("past", "yesterday")."""
    today = today or datetime.now().date()
    lowered = (text or "").lower()
    found: list[tuple[str, str]] = []
    for m in re.finditer(
        rf"\b(?P<day>\d{{1,2}})\s*(?:'?(?:st|nd|rd|th))?\s*(?:of\s+)?(?P<mon>{_MONTH_ALT})\b(?:\s+(?P<year>20\d{{2}}))?", lowered
    ):
        day, month = int(m.group("day")), _MONTHS[m.group("mon")]
        year = int(m.group("year")) if m.group("year") else today.year
        if day < 1 or day > _days_in_month(year, month) if 1 <= month <= 12 else True:
            found.append(("invalid", m.group(0).strip()))
        elif m.group("year") and date(year, month, day) < today:
            found.append(("past", m.group(0).strip()))
    for m in re.finditer(
        rf"\b(?P<mon>{_MONTH_ALT})\s+(?:the\s+)?(?P<day>\d{{1,2}})\b(?!\s*(?:{_unit_alternation()})\b)(?:\s*(?:'?(?:st|nd|rd|th)))?(?:\s+(?P<year>20\d{{2}}))?", lowered
    ):
        day, month = int(m.group("day")), _MONTHS[m.group("mon")]
        if m.group("mon") == "may" and not re.search(r"\bmay\s+\d", lowered):
            continue
        year = int(m.group("year")) if m.group("year") else today.year
        if day < 1 or day > _days_in_month(year, month):
            if day <= 31:
                found.append(("invalid", m.group(0).strip()))
        elif m.group("year") and date(year, month, day) < today:
            found.append(("past", m.group(0).strip()))
    for m in re.finditer(r"\b(?P<d>\d{1,2})[/.](?P<m>\d{1,2})[/.](?P<y>\d{2,4})\b", lowered):
        day, month, year = int(m.group("d")), int(m.group("m")), int(m.group("y"))
        year += 2000 if year < 100 else 0
        if not 1 <= month <= 12 or day < 1 or day > _days_in_month(year, month):
            found.append(("invalid", m.group(0)))
        elif date(year, month, day) < today:
            found.append(("past", m.group(0)))
    for m in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lowered):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not 1 <= month <= 12 or day < 1 or day > _days_in_month(year, month):
            found.append(("invalid", m.group(0)))
        elif date(year, month, day) < today:
            found.append(("past", m.group(0)))
    for m in _PAST_WORDS_RE.finditer(lowered):
        found.append(("past", m.group(0)))
    return found


def _segments(message: str) -> list[dict[str, Any]]:
    """Every "<number> <unit> <product words>" group of a message ("10 ton onion and 5 ton potato")."""
    unit_alt = _unit_alternation()
    text = _expand_multipliers(message)
    out: list[dict[str, Any]] = []
    for m in re.finditer(
        rf"(?P<n>{_NUMBER})\s*(?P<u>{unit_alt})\s+(?:of\s+)?(?P<p>[a-zA-Z][a-zA-Z\-]*(?:\s+[a-zA-Z][a-zA-Z\-]*){{0,2}})", text, flags=re.IGNORECASE
    ):
        words = []
        for w in m.group("p").lower().split():
            if w in _FILLER or w in _CHITCHAT or w in _COUNT_UNITS or singularise(w) in _COUNT_UNITS or w in _MONTHS:
                break
            if find_location(w) is not None or w in {"each", "per", "box", "boxes"}:
                break
            words.append(w)
        phrase = " ".join(words)
        if phrase and learned_vocabulary.valid_product_term(phrase):
            out.append({"product": phrase, "quantity_value": _to_decimal(m.group("n")), "quantity_unit": _canonical_unit(m.group("u"))})
    return out


def _extra_items(message: str, req: Requirements) -> list[dict[str, Any]]:
    """The other products when one message orders several ("10 ton onion and 5 ton potato"); [] for a single one."""
    segs = _segments(message)
    distinct: list[dict[str, Any]] = []
    for seg in segs:
        key = singularise(seg["product"].split()[-1])
        if all(singularise(d["product"].split()[-1]) != key for d in distinct):
            distinct.append(seg)
    if len(distinct) < 2:
        return []
    main = singularise((req.product or "").split()[-1]) if req.product else None
    if main and all(singularise(d["product"].split()[-1]) != main for d in distinct):
        return []  # the segments do not describe the product we understood: not a multi-product order
    return [d for d in distinct if not main or singularise(d["product"].split()[-1]) != main]


def _finalise(req: Requirements, message: str) -> Requirements:
    """Remove what cannot be real and say so in ``req.flags``; derive nothing from a guess."""
    unit_alt = _unit_alternation()
    flags = req.flags
    if req.product and all(_looks_like_gibberish(w) for w in re.findall(r"[^\W\d_]+", req.product)):
        req.product = None  # keyboard mash is not a product
    if req.product and len(req.product.split()) > 3:
        # a product is a short noun phrase: cut a long leftover to its known-commodity ending, else do not guess one
        req.product = _head_product(req.product)
        if req.product and len(req.product.split()) > 4:
            req.product = None
    neg = re.search(_NEGATIVE_QTY_RE.pattern.replace("{unit_alt}", unit_alt), message, re.IGNORECASE)
    zero = re.search(_ZERO_QTY_RE.pattern.replace("{unit_alt}", unit_alt), message, re.IGNORECASE)
    bad = neg or zero
    if bad or (req.quantity_value is not None and req.quantity_value <= 0):
        flags["quantity_nonpositive"] = bad.group(0).strip() if bad else str(req.quantity_value)
        req.quantity_value = req.quantity_unit = req.min_quantity = req.max_quantity = None
    for m in re.finditer(_ANY_QTY_RE.pattern.replace("{unit_alt}", unit_alt), _expand_multipliers(message), re.IGNORECASE):
        value = _to_decimal(m.group("n"))
        if value is not None and query_grounding.quantity_is_absurd(value, _canonical_unit(m.group("u"))):
            flags["quantity_absurd"] = f"{m.group('n')} {m.group('u')}"
            req.quantity_value = req.quantity_unit = req.min_quantity = req.max_quantity = None
            break
    if req.quantity_value is not None and not req.quantity_unit and "quantity_nonpositive" not in flags:
        flags["quantity_unit_missing"] = str(req.quantity_value)
    if req.price_amount is not None:
        if req.price_is_total or (
            query_grounding.states_total_price(message) and not _price_unit_stated(message)
        ):
            req.price_is_total = True
            flags["price_total"] = {"total": str(req.price_amount), "currency": req.price_currency}
            if req.quantity_value is not None and req.quantity_value > 0 and req.quantity_unit:
                per = (req.price_amount / req.quantity_value).quantize(Decimal("0.0001")).normalize()
                flags["price_total"].update(per_unit_amount=str(per), qty=str(req.quantity_value), unit=req.quantity_unit)
        elif req.price_per_unit:
            base = unit_converter.base_unit(req.price_per_unit)
            if base is not None:
                per_base = req.price_amount / base[0]
                if per_base < _MIN_PRICE_PER_BASE or per_base > _MAX_PRICE_PER_BASE:
                    flags["price_absurd"] = f"{req.price_amount.normalize():f} {req.price_currency or ''} per {req.price_per_unit}".replace("  ", " ")
                    req.price_amount = req.price_currency = req.price_per_unit = None
    for kind, text in date_issues(message):
        flags.setdefault(f"date_{kind}", text)
    if "date_invalid" in flags or "date_past" in flags:
        req.deadline_days = None
    extras = _extra_items(message, req)
    ai_items = flags.pop("ai_products", None)
    if ai_items:
        # the model's list of the message's goods (any joining words): everything but the main product is queued
        main = singularise((req.product or ai_items[0]["product"]).split()[-1])
        extras = [
            {"product": i["product"], "quantity_value": _to_decimal(str(i["quantity_value"])) if i.get("quantity_value") else None,
             "quantity_unit": i.get("quantity_unit")}
            for i in ai_items if singularise(i["product"].split()[-1]) != main
        ]
    if extras:
        flags["extra_items"] = [
            {"product": e["product"], "quantity_value": str(e["quantity_value"]) if e.get("quantity_value") is not None else None,
             "quantity_unit": e.get("quantity_unit")} for e in extras
        ]
    return req


def _price_unit_stated(message: str) -> bool:
    """The price names its unit ("per kg", "/kg", "rs kilo"), so it is a unit price whatever else the text says."""
    return _extract_price(_expand_multipliers(message))[2] is not None


def _is_pack_contents(value: Any, pack_contents: Any) -> bool:
    """True when ``value`` is the number a pack was said to hold ("5 dozen" -> 5 or 60)."""
    m = re.match(r"([\d.]+)\s*(dozen)?", str(pack_contents or ""))
    if not m:
        return False
    n = _to_decimal(m.group(1))
    if n is None:
        return False
    return Decimal(str(value)) in {n, n * 12 if m.group(2) else n}


def _looks_like_gibberish(word: str) -> bool:
    """Keyboard mash ("asdfghjkl"): almost no vowels, or a run of six consonants no real word has."""
    w = word.lower()
    if len(w) < 5 or not w.isascii():
        return False
    vowels = sum(ch in "aeiouy" for ch in w)
    return vowels / len(w) < 0.15 or bool(re.search(r"[^aeiouy\W\d_]{6,}", w))


def _head_product(product: Optional[str]) -> Optional[str]:
    """A product is a short noun phrase. A long one ("sir small family business doing groundnut") is cut to its
    longest known-commodity ending; with none the phrase is kept only up to four words."""
    if not product:
        return product
    words = product.split()
    if len(words) <= 3:
        return product
    for n in (3, 2, 1):
        tail = " ".join(words[-n:])
        if _known_commodity(tail):
            return tail
    return " ".join(words[-3:]) if len(words) > 4 else product


def _known_commodity(name: str) -> bool:
    """A product name the verified seed, the AI lexicon or learned vocabulary already knows."""
    key = " ".join(name.lower().split())
    return bool(
        key in product_synonyms._GROUPS
        or product_synonyms.canonical_token(key)
        or ai_lexicon.term(key)
        or key in learned_vocabulary._load()["product_category"]
    )


def _requirements_from_ai(
    ai: query_grounding.AIParse, rules: Requirements, message: str, loc: Optional[Location] = None
) -> Requirements:
    """AI understanding first; the rule parse only fills what the model left empty.

    ``loc`` is the AI location already resolved (possibly by the model, for a town no
    table knows); without it the static tables are consulted.
    """
    merged = rules.model_copy(deep=True)
    stated_role = ai.role or None
    if stated_role is None:
        stated_role = learned_vocabulary.role_for(message) or _extract_role(message)
    merged.role = stated_role or RFQRole.BUYER

    if not ai.product and merged.product and not _known_commodity(merged.product):
        # The model read the message and found no goods ("15 din mein", "write me a
        # poem about love"); a rule-parser leftover ("din", "write poem clove") is noise.
        merged.product = None
    if ai.product:
        cleaned = clean_product_phrase(ai.product)
        words = ai.product.lower().split()
        # The last word heads the model's product name ("PET bottles", "hair colour"):
        # when the rule cleaner strips it as a unit or a colour, the name is the model's.
        if (
            cleaned and len(words) > 1
            and singularise(words[-1]) not in {singularise(w) for w in cleaned.split()}
        ):
            cleaned = " ".join(words)
        # A product the user wrote (almost) literally keeps its own spelling: "t-shirt" is not the rule cleaner's "shirt".
        literal = ai.product.strip().lower()
        squash = lambda t: re.sub(r"[\s\-]+", "", t.lower())  # noqa: E731
        if literal and 0 < len(literal.split()) <= 3 and squash(singularise(literal)) in squash(message):
            cleaned = singularise(literal) if len(literal.split()) == 1 else literal
        if len(literal.split()) == 1:
            # the model cut a glued word ("shirt" from "tshirts"): the user's own word is the product
            for word in re.findall(r"[^\W\d_]+", message.lower()):
                sing = singularise(word)
                if sing != singularise(literal) and sing.endswith(singularise(literal)) and 0 < len(sing) - len(singularise(literal)) <= 2:
                    cleaned = sing
                    break
        merged.product = _head_product(cleaned or rules.product)
    if ai.category:
        merged.category = ai.category
    elif not merged.category and merged.product:
        merged.category = _infer_category(merged.product)

    if ai.attributes:
        merged.attributes = {**rules.attributes, **ai.attributes}
    # The model reads negations in context ("no seamless"); the rule parser's (already filtered) guess is kept only
    # when the model found none.
    negated = {k for k in ai.negated_attributes} or set(rules.negated_attributes)
    merged.negated_attributes = negated
    for key in ai.negated_attributes:
        merged.attributes.pop(key, None)

    if ai.quantity_value is not None and rules.flags.get("quantity_compound") and rules.quantity_value is not None:
        pass  # "5 ton 200 kg": the model read the first part; the rules summed both (5.2 ton)
    elif ai.quantity_value is not None and _is_pack_contents(ai.quantity_value, rules.attributes.get("pack_contents")):
        merged.quantity_value = rules.quantity_value  # "each box is 5 dozen": that is what a box holds, not the order
        merged.quantity_unit = rules.quantity_unit
    elif ai.quantity_value is not None:
        merged.quantity_value = ai.quantity_value
        merged.quantity_unit = ai.quantity_unit or rules.quantity_unit
    elif merged.quantity_value is not None and not merged.quantity_unit:
        # A bare number the model did not read as a quantity is a day count, a
        # grade or a size ("15 din mein" overwrote 80 quintal with 15 pcs).
        merged.quantity_value = None
    if ai.price_amount is not None:
        merged.price_amount = ai.price_amount
        merged.price_currency = ai.price_currency or rules.price_currency
        merged.price_per_unit = ai.price_per_unit or rules.price_per_unit
        merged.price_is_total = ai.price_is_total or rules.price_is_total
    if len(ai.products) > 1:
        merged.flags["ai_products"] = ai.products
    elif merged.price_amount is not None and merged.price_amount == merged.quantity_value:
        # The model read the number as a quantity ("looking for 100 kg"); the rule
        # parser's "for 100" price pattern must not reuse it as a price.
        merged.price_amount = merged.price_currency = merged.price_per_unit = None
    if ai.deadline_days is not None:
        merged.deadline_days = ai.deadline_days

    if ai.location:
        # The model read the whole message, so its place beats a rule-parser city that may
        # be a fuzzy false positive.
        if loc is None and not rules.city:
            loc = find_location(ai.location)
        if loc:
            merged.city, merged.state, merged.country = loc.city, loc.state, loc.country
            merged.currency_hint = loc.currency
            merged.latitude = Decimal(str(loc.latitude))
            merged.longitude = Decimal(str(loc.longitude))
        elif not rules.city:
            # A place no gazetteer, cached place or model lookup knows is asked about, never stored as a guess.
            merged.flags["location_unresolved"] = ai.location
            merged.city = merged.state = merged.country = None
    return merged


def extract(message: str) -> Requirements:
    """Parse one message in isolation. Merging with history happens elsewhere.

    Sync callers run inside the event loop, so this never calls the network: it
    reuses a model parse made earlier for the same message (see ``extract_async``)
    and otherwise uses the rules.
    """
    rules = _rule_extract(message)
    ai = ai_query_parser.cached(message)
    if ai is None:
        return _finalise(rules, message)
    return _finalise(_requirements_from_ai(ai, rules, message), message)


async def extract_async(message: str, user_id: Any = None) -> Requirements:
    """AI-first parse with grounding, falling back to the rules, then learn from it.

    ``user_id`` (who wrote the message) lets the parse count as one vote for the vocabulary it contains; without it
    nothing is learned, because a single user must not redefine words for everyone."""
    rules = _rule_extract(message)
    # Values remembered for this product family are hints for the model, never rules of their own.
    family = rules.category or learned_vocabulary.category_for(message)
    ai = await ai_query_parser.understand(
        message,
        known_categories=learned_vocabulary.known_categories(),
        hints=learned_vocabulary.attribute_hints(family),
    )
    if ai is None:
        return _finalise(rules, message)
    learned_vocabulary.learn_from(ai, message, user_id)
    loc = await locations.resolve_location(ai.location) if ai.location else None
    if ai.product:
        ai_lexicon.note_parse(
            ai.product, ai.product_as_written, ai.category, getattr(ai, "product_is_generic", None), user_id
        )
        _schedule_term_lookup([ai.product, ai.product_as_written])
    return _finalise(_requirements_from_ai(ai, rules, message, loc), message)


# Strong references to fire-and-forget lexicon lookups, so the loop does not drop them mid-flight.
_background_tasks: set[asyncio.Task] = set()


def _schedule_term_lookup(terms: list[Optional[str]]) -> None:
    """Learn the product's names in the background; never delays the reply."""
    wanted = [t for t in terms if t and t.strip()]
    if not wanted:
        return
    try:
        task = asyncio.get_running_loop().create_task(ai_lexicon.resolve_terms(wanted))
    except RuntimeError:
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def detect_role(message: str) -> Optional[RFQRole]:
    """The role the user actually stated in this message, or None.

    ``extract(...).role`` falls back to BUYER for the direct search flow, so it
    cannot tell "I am buying" from "no role mentioned". Chat code that updates a
    draft must use this instead, or every message resets the draft to BUYER.
    """
    if not message or not message.strip():
        return None
    ai = ai_query_parser.cached(message)
    if ai is not None and ai.role is not None:
        return ai.role
    learned = learned_vocabulary.role_for(message)
    if learned is not None:
        return learned
    return _extract_role(message)


async def detect_role_async(message: str) -> Optional[RFQRole]:
    """``detect_role`` after giving the model a chance to read the message."""
    if not message or not message.strip():
        return None
    await extract_async(message)
    return detect_role(message)


def clean_product_phrase(phrase: Optional[str]) -> Optional[str]:
    """Strip filler from a product phrase proposed elsewhere (e.g. by the LLM).

    "buyer details" and "details aslo" reduce to nothing, so they are never
    queried as a commodity.
    """
    if not phrase or not phrase.strip():
        return None
    return _extract_product(phrase.lower(), {})


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
        if value is None or value <= 0:
            return None
        answer.quantity_value = value
        answer.quantity_unit = unit or _bare_unit(without_deadline)
        if pack_attrs:
            answer.attributes.update(pack_attrs)
        return answer

    if field == "price":
        amount, currency, per_unit, _ = _extract_price(message)
        if amount is None:
            amount = _bare_price_answer(message)
            currency = _currency_in(message)
        if amount is None or amount <= 0:
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


# Words that may surround a bare number that answers "what price?" ("ok 15", "around 15 only").
_ANSWER_FILLER = frozenset({
    "ok", "okay", "about", "around", "approx", "approximately", "roughly", "only", "just", "maybe", "say", "like", "is",
    "it", "its", "the", "a", "price", "rate", "of", "to", "be", "should", "make", "keep", "then", "yes", "haan", "bas",
    "lagbhag", "hai", "per", "pls", "please", "kindly", "so", "i", "can", "pay", "offer", "target",
}) | learned_vocabulary._COMMAND_WORDS


def _bare_price_answer(message: str) -> Optional[Decimal]:
    """The number of a reply that answers the price question without naming a currency or unit ("15", "ok 15 only").

    A number that belongs to another statement is not a price: one followed by an order unit ("0 kg", "5 ton"), or in
    a sentence of its own words ("I want to sell 10 pistols", "each box is 5 dozen")."""
    numbers = list(re.finditer(rf"(?<![\w.]){_NUMBER}\b", message))
    if len(numbers) != 1:
        return None
    m = numbers[0]
    after = message[m.end():].strip().lower()
    word = re.match(r"[^\W\d_]+", after)
    if word and (unit_converter.base_unit(word.group(0)) is not None or unit_converter.is_packaging_unit(word.group(0))
                 or _canonical_unit(word.group(0)) in _COUNT_UNITS or word.group(0) in {"dozen", "dozens", "doz"}):
        return None
    leftover = [w for w in re.findall(r"[^\W\d_]+", (message[:m.start()] + " " + message[m.end():]).lower())
                if w not in _ANSWER_FILLER and not _normalise_currency(w)]
    if len(leftover) > 1:
        return None
    return _to_decimal(m.group(0))


def parse_unit_answer(message: str) -> Optional[str]:
    """The unit of a reply that only names one ("kg", "tonnes please", "in pieces"), else None."""
    words = re.findall(r"[^\W\d_]+", (message or "").lower())
    if not words or len(words) > 4:
        return None
    unit = _bare_unit(message)
    if not unit:
        return None
    rest = [w for w in words if w not in _ANSWER_FIELD_FILLER and _canonical_unit(w) != _canonical_unit(unit)]
    return _canonical_unit(unit) if not rest else None


_ANSWER_FIELD_FILLER = frozenset({"in", "ok", "okay", "please", "pls", "only", "it", "is", "are", "the", "of", "sorry", "yes", "its", "kindly"})


def _bare_unit(message: str) -> Optional[str]:
    """A unit mentioned without a number, e.g. answering "pcs"."""
    unit_alt = _unit_alternation()
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

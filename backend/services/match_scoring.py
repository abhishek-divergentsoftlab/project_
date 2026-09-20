"""Business-rule scoring for candidate matches.

Vector similarity alone is not a match. It will happily rank a seller who has
4,000 units against a buyer who needs 6,000, because the two descriptions read
almost identically. These functions apply the compatibility rules that decide
whether a semantically similar RFQ is actually a viable counterparty.

Every function here is pure and takes plain values, so the rules can be reasoned
about and tested without a database.
"""

import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from services import currency, unit_converter

# How much each dimension contributes. Dimensions that cannot be evaluated
# (a missing price, say) are dropped and the remainder is renormalised, so an
# RFQ is never punished for a field its counterparty left blank.
WEIGHTS: dict[str, float] = {
    "relevance": 0.28,
    # Attributes the requester actually named carry real weight. Leaving them to
    # the text score meant that asking for "black" was outvoted by quantity and
    # distance; at 0.15 a named alloy grade still lost to a nearer supplier of
    # the wrong grade. In a spec-driven market the spec is the point.
    "attributes": 0.22,
    "category": 0.10,
    "price": 0.16,
    "quantity": 0.12,
    "location": 0.08,
    "deadline": 0.04,
}

# Keys that name the product rather than describe it.
_PRODUCT_NAME_KEYS = frozenset({"name", "product", "product_name", "title"})

# Keys representing derived quantity measurements rather than intrinsic specifications.
_DERIVED_QUANTITY_KEYS = frozenset({
    "total_weight_kg", "total_weight_tons", "pack_weight_kg", "pack_count",
})

# Units that mean the same thing, so "500 pcs" and "500 pieces" compare.
_UNIT_SYNONYMS: dict[str, str] = {
    "pc": "pcs", "pcs": "pcs", "piece": "pcs", "pieces": "pcs",
    "unit": "pcs", "units": "pcs", "no": "pcs", "nos": "pcs", "each": "pcs",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg",
    "kilograms": "kg",
    "tonne": "tonne", "tonnes": "tonne", "ton": "tonne", "tons": "tonne",
    "mt": "tonne", "metric ton": "tonne", "metric tons": "tonne",
    "quintal": "quintal", "quintals": "quintal", "qtl": "quintal",
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "box": "box", "boxes": "box", "carton": "carton", "cartons": "carton",
    "roll": "roll", "rolls": "roll", "set": "set", "sets": "set",
    "bora": "bora", "boras": "bora", "bori": "bora", "boris": "bora",
    "katta": "bora", "kattas": "bora",
    "bag": "bag", "bags": "bag", "sack": "bag", "sacks": "bag",
}

# ---------------------------------------------------------------------------
# Synonym graph for semantic attribute matching.
#
# Each group maps a canonical concept to a dict of variants. The value is the
# similarity to the canonical form (1.0 = exact synonym, 0.85 = related shade
# or variant, 0.80 = loosely related).
#
# This replaces dumb token overlap for attribute values: "grey" vs "gray" was
# 0.0 before, now it's 1.0. "red" vs "crimson" was 0.0, now it's 0.85.
# ---------------------------------------------------------------------------

_SYNONYM_GROUPS: dict[str, dict[str, float]] = {
    # --- Colors ---
    "grey": {
        "grey": 1.0, "gray": 1.0, "gre": 1.0,
        "charcoal": 0.85, "charcoal grey": 0.85, "charcoal gray": 0.85,
        "dark grey": 0.85, "dark gray": 0.85, "slate": 0.85, "slate grey": 0.85,
        "silver": 0.80, "silver grey": 0.85, "ash": 0.80, "ash grey": 0.85,
        "light grey": 0.85, "light gray": 0.85, "smoke": 0.80,
        "gunmetal": 0.80, "pewter": 0.80, "graphite": 0.85,
    },
    "black": {
        "black": 1.0, "blk": 1.0, "jet black": 1.0,
        "matte black": 0.90, "matt black": 0.90, "flat black": 0.90,
        "glossy black": 0.90, "gloss black": 0.90,
        "ebony": 0.85, "onyx": 0.85, "charcoal black": 0.85,
    },
    "white": {
        "white": 1.0, "wht": 1.0, "pure white": 1.0, "bright white": 1.0,
        "ivory": 0.80, "cream": 0.80, "off-white": 0.85, "off white": 0.85,
        "eggshell": 0.80, "pearl": 0.80, "pearl white": 0.85,
        "snow": 0.80, "milky": 0.80, "milky white": 0.85,
    },
    "red": {
        "red": 1.0, "scarlet": 0.90,
        "crimson": 0.85, "maroon": 0.80, "burgundy": 0.80,
        "wine": 0.80, "wine red": 0.85, "cherry": 0.85, "cherry red": 0.85,
        "ruby": 0.85, "vermilion": 0.85, "rust": 0.75, "rust red": 0.80,
        "dark red": 0.85, "deep red": 0.85, "brick red": 0.80,
    },
    "blue": {
        "blue": 1.0, "royal blue": 0.90,
        "navy": 0.85, "navy blue": 0.85, "dark blue": 0.85,
        "midnight blue": 0.85, "cobalt": 0.85, "cobalt blue": 0.85,
        "sky blue": 0.85, "light blue": 0.85, "baby blue": 0.80,
        "teal": 0.75, "azure": 0.85, "cyan": 0.75, "indigo": 0.80,
        "turquoise": 0.75, "sapphire": 0.85, "marine": 0.80,
    },
    "green": {
        "green": 1.0, "lime": 0.80, "lime green": 0.85,
        "olive": 0.80, "olive green": 0.85, "forest green": 0.85,
        "dark green": 0.85, "army green": 0.85, "sage": 0.80,
        "sage green": 0.85, "emerald": 0.85, "mint": 0.80, "mint green": 0.85,
        "moss": 0.80, "hunter green": 0.85, "neon green": 0.80,
        "chartreuse": 0.75, "jade": 0.80, "pine": 0.80,
    },
    "yellow": {
        "yellow": 1.0, "golden": 0.85, "gold": 0.80,
        "lemon": 0.85, "lemon yellow": 0.85, "mustard": 0.80,
        "amber": 0.80, "canary": 0.85, "saffron": 0.80,
        "sunflower": 0.80, "butter": 0.80,
    },
    "brown": {
        "brown": 1.0, "chocolate": 0.85, "chocolate brown": 0.85,
        "coffee": 0.85, "coffee brown": 0.85, "mocha": 0.85,
        "walnut": 0.85, "chestnut": 0.85, "mahogany": 0.80,
        "tan": 0.80, "sienna": 0.80, "umber": 0.80, "cocoa": 0.85,
        "espresso": 0.85, "teak": 0.80, "dark brown": 0.85,
    },
    "beige": {
        "beige": 1.0, "tan": 0.85, "khaki": 0.85, "sand": 0.85,
        "camel": 0.85, "buff": 0.80, "fawn": 0.80, "taupe": 0.85,
        "wheat": 0.80, "nude": 0.80, "natural": 0.75,
    },
    "orange": {
        "orange": 1.0, "tangerine": 0.85, "peach": 0.80,
        "coral": 0.80, "apricot": 0.80, "burnt orange": 0.85,
        "terracotta": 0.80, "copper": 0.80, "pumpkin": 0.85,
    },
    "pink": {
        "pink": 1.0, "rose": 0.85, "rose pink": 0.85,
        "blush": 0.85, "salmon": 0.80, "fuchsia": 0.80,
        "magenta": 0.80, "hot pink": 0.85, "baby pink": 0.85,
        "dusty pink": 0.85, "mauve": 0.80, "coral pink": 0.80,
    },
    "purple": {
        "purple": 1.0, "violet": 0.90, "lavender": 0.80,
        "plum": 0.85, "eggplant": 0.80, "mauve": 0.80,
        "lilac": 0.80, "amethyst": 0.85, "grape": 0.85,
        "orchid": 0.80, "magenta": 0.75,
    },

    # --- Materials ---
    "stainless steel": {
        "stainless steel": 1.0, "ss": 1.0, "inox": 1.0,
        "stainless": 0.95, "s steel": 0.95, "s.s.": 1.0,
    },
    "aluminum": {
        "aluminum": 1.0, "aluminium": 1.0, "al": 1.0,
        "alloy": 0.70, "alu": 0.95, "duralumin": 0.85,
    },
    "polyethylene": {
        "polyethylene": 1.0, "pe": 1.0, "polythene": 1.0,
        "hdpe": 0.85, "ldpe": 0.85, "lldpe": 0.85,
    },
    "polypropylene": {
        "polypropylene": 1.0, "pp": 1.0, "polypro": 1.0,
    },
    "polyester": {
        "polyester": 1.0, "pet": 0.90, "polyethylene terephthalate": 0.90,
    },
    "cotton": {
        "cotton": 1.0, "100% cotton": 1.0, "pure cotton": 1.0,
        "organic cotton": 0.90, "combed cotton": 0.90,
        "egyptian cotton": 0.90, "pima cotton": 0.90,
    },
    "leather": {
        "leather": 1.0, "genuine leather": 0.95, "real leather": 0.95,
        "full grain leather": 0.90, "top grain leather": 0.90,
        "buffalo leather": 0.90, "cowhide": 0.85,
        "nubuck": 0.80, "suede": 0.80,
    },
    "rubber": {
        "rubber": 1.0, "natural rubber": 0.95, "nr": 0.90,
        "synthetic rubber": 0.85, "silicone rubber": 0.85,
        "neoprene": 0.80, "epdm": 0.80, "nitrile": 0.80,
    },
    "wood": {
        "wood": 1.0, "timber": 1.0, "lumber": 1.0,
        "hardwood": 0.90, "softwood": 0.85, "plywood": 0.85,
        "mdf": 0.80, "particle board": 0.75, "particleboard": 0.75,
    },
    "glass": {
        "glass": 1.0, "tempered glass": 0.90, "toughened glass": 0.90,
        "borosilicate": 0.85, "float glass": 0.85,
        "crystal": 0.80, "gorilla glass": 0.85,
    },
    "brass": {
        "brass": 1.0, "yellow brass": 0.95,
        "bronze": 0.80, "copper alloy": 0.80,
    },
    "iron": {
        "iron": 1.0, "cast iron": 0.90, "wrought iron": 0.90,
        "pig iron": 0.85, "ductile iron": 0.85,
    },
    "mild steel": {
        "mild steel": 1.0, "ms": 1.0, "carbon steel": 0.90,
        "low carbon steel": 0.90, "plain carbon steel": 0.90,
    },
    "copper": {
        "copper": 1.0, "cu": 1.0, "electrolytic copper": 0.95,
        "oxygen free copper": 0.90, "ofc": 0.90,
    },

    # --- Finishes & Textures ---
    "matte": {
        "matte": 1.0, "matt": 1.0, "mat": 1.0, "flat": 0.90,
        "satin": 0.80, "non-glossy": 0.85, "non glossy": 0.85,
    },
    "glossy": {
        "glossy": 1.0, "gloss": 1.0, "shiny": 0.90,
        "polished": 0.85, "high gloss": 0.95, "mirror": 0.80,
        "lacquered": 0.80, "varnished": 0.80,
    },
    "brushed": {
        "brushed": 1.0, "hairline": 0.90, "satin finish": 0.85,
        "brushed finish": 1.0,
    },
    "textured": {
        "textured": 1.0, "embossed": 0.85, "hammered": 0.80,
        "rough": 0.80, "ribbed": 0.80, "knurled": 0.80,
    },
    "galvanized": {
        "galvanized": 1.0, "galvanised": 1.0, "gi": 0.95,
        "hot dip galvanized": 0.95, "hot dip galvanised": 0.95,
        "zinc coated": 0.90, "zinc plated": 0.85,
    },
    "powder coated": {
        "powder coated": 1.0, "powder coat": 1.0,
        "epoxy coated": 0.85, "painted": 0.80, "coated": 0.75,
    },
    "anodized": {
        "anodized": 1.0, "anodised": 1.0, "hard anodized": 0.95,
        "hard anodised": 0.95,
    },
    "chrome plated": {
        "chrome plated": 1.0, "chrome": 0.90, "chromium plated": 1.0,
        "nickel chrome": 0.85, "electroplated": 0.80,
    },

    # --- Trade Terms ---
    "eco-friendly": {
        "eco-friendly": 1.0, "eco friendly": 1.0, "ecofriendly": 1.0,
        "sustainable": 0.90, "green": 0.80, "environmentally friendly": 0.90,
        "biodegradable": 0.80, "recyclable": 0.80, "organic": 0.75,
    },
    "lightweight": {
        "lightweight": 1.0, "light weight": 1.0, "light": 0.85,
        "ultralight": 0.90, "ultra light": 0.90, "featherweight": 0.85,
    },
    "heavy-duty": {
        "heavy-duty": 1.0, "heavy duty": 1.0, "heavyduty": 1.0,
        "industrial grade": 0.90, "industrial": 0.85,
        "commercial grade": 0.85, "commercial": 0.80,
        "rugged": 0.85, "reinforced": 0.80, "extra strong": 0.80,
    },
    "food-grade": {
        "food-grade": 1.0, "food grade": 1.0, "foodgrade": 1.0,
        "food safe": 0.95, "food-safe": 0.95,
        "fda approved": 0.90, "fda": 0.85,
        "fssai": 0.85, "fssai approved": 0.90,
    },
    "waterproof": {
        "waterproof": 1.0, "water proof": 1.0, "water-proof": 1.0,
        "water resistant": 0.85, "water-resistant": 0.85,
        "weatherproof": 0.85, "weather resistant": 0.80,
        "splash proof": 0.80, "moisture resistant": 0.80,
    },
    "fireproof": {
        "fireproof": 1.0, "fire proof": 1.0, "fire-proof": 1.0,
        "fire resistant": 0.90, "fire-resistant": 0.90, "flame retardant": 0.85,
        "flame-retardant": 0.85, "non-flammable": 0.85, "non flammable": 0.85,
    },
    "anti-corrosion": {
        "anti-corrosion": 1.0, "anti corrosion": 1.0, "anticorrosion": 1.0,
        "corrosion resistant": 0.95, "corrosion-resistant": 0.95,
        "rust proof": 0.90, "rustproof": 0.90, "rust-proof": 0.90,
        "rust resistant": 0.85, "rust-resistant": 0.85,
    },

    # --- Shapes & Forms ---
    "round": {
        "round": 1.0, "circular": 0.95, "cylindrical": 0.85,
        "tubular": 0.80,
    },
    "square": {
        "square": 1.0, "rectangular": 0.80, "rect": 0.80,
    },
    "flat": {
        "flat": 1.0, "sheet": 0.80, "plate": 0.80,
    },
}

# Build flat lookup: token → (canonical_group, similarity)
# "gray" → ("grey", 1.0), "crimson" → ("red", 0.85)
_SYNONYM_LOOKUP: dict[str, tuple[str, float]] = {}
for _canonical, _variants in _SYNONYM_GROUPS.items():
    for _variant, _sim in _variants.items():
        _key = _variant.lower().strip()
        # If already mapped, keep the higher similarity
        if _key in _SYNONYM_LOOKUP:
            existing_sim = _SYNONYM_LOOKUP[_key][1]
            if _sim > existing_sim:
                _SYNONYM_LOOKUP[_key] = (_canonical, _sim)
        else:
            _SYNONYM_LOOKUP[_key] = (_canonical, _sim)


def _resolve_synonym(text: str) -> tuple[Optional[str], float]:
    """Resolve a text value to its canonical synonym group.

    Returns (canonical_name, similarity) or (None, 0.0) if not found.
    Tries exact match first, then the singularised/stemmed form.
    """
    cleaned = text.strip().lower()
    if cleaned in _SYNONYM_LOOKUP:
        return _SYNONYM_LOOKUP[cleaned]

    # Try singularised form
    singular = singularise(cleaned)
    if singular in _SYNONYM_LOOKUP:
        return _SYNONYM_LOOKUP[singular]

    return None, 0.0


def _semantic_text_similarity(wanted: str, offered: str) -> float:
    """Synonym-aware text comparison for attribute values.

    Resolution order:
    1. Exact string match (case-insensitive) → 1.0
    2. Both resolve to the same synonym group → min(sim_wanted, sim_offered)
       e.g. "grey" (1.0 to grey) vs "charcoal" (0.85 to grey) → 0.85
    3. Multi-token: resolve each token, compute overlap on canonical forms
    4. Fallback: original token overlap with 0.4 discount
    """
    w = wanted.strip().lower()
    o = offered.strip().lower()

    # 1. Exact string match
    if w == o:
        return 1.0

    # 2. Full-phrase synonym resolution
    w_canon, w_sim = _resolve_synonym(w)
    o_canon, o_sim = _resolve_synonym(o)

    if w_canon is not None and o_canon is not None:
        if w_canon == o_canon:
            # Both map to the same concept — return the weaker link
            return min(w_sim, o_sim)
        else:
            # Different concepts (e.g. "red" vs "blue")
            return 0.0

    # 3. Multi-token resolution: resolve each token individually and compare
    w_tokens = tokenize(w)
    o_tokens = tokenize(o)

    if w_tokens and o_tokens:
        # Resolve each token to its canonical form
        w_resolved = set()
        o_resolved = set()
        min_sim = 1.0

        for t in w_tokens:
            canon, sim = _resolve_synonym(t)
            if canon is not None:
                w_resolved.add(canon)
                min_sim = min(min_sim, sim)
            else:
                w_resolved.add(t)

        for t in o_tokens:
            canon, sim = _resolve_synonym(t)
            if canon is not None:
                o_resolved.add(canon)
                min_sim = min(min_sim, sim)
            else:
                o_resolved.add(t)

        # Compute overlap on resolved tokens
        resolved_overlap = _overlap(w_resolved, o_resolved)
        if resolved_overlap >= 0.999:
            return min_sim
        if resolved_overlap > 0:
            # Partial overlap on multi-token values (e.g. "SS 316L" vs "SS 304"
            # sharing only "stainless steel") gets the 0.4 discount — the
            # unmatched tokens (grade numbers) are the distinguishing part.
            return resolved_overlap * 0.4 * min_sim

    # 4. Fallback: raw token overlap with 0.4 penalty (original behaviour)
    if not w_tokens or not o_tokens:
        return 0.0
    raw_overlap = _overlap(w_tokens, o_tokens)
    return raw_overlap if raw_overlap >= 0.999 else raw_overlap * 0.4

# Dropped before comparing text: they carry no product signal.
#
# The second group is the vocabulary our own search_text renderer emits --
# "Role:", "Category:", "Target price:" and so on. Every RFQ contains all of
# them, so leaving them in gave any two unrelated RFQs a large shared
# vocabulary and inflated every relevance score.
_STOPWORDS = frozenset(
    (
        "a an and are as at be by for from have i in is it its of on or "
        "our that the to we with need want buy sell supply looking "
        "role category product quantity target price location deadline "
        "attributes notes buyer seller units within days bulk orders "
        "gst invoice provided ready stock yes no inr india"
    ).split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def singularise(word: str) -> str:
    """Crude but predictable stemming, so "cables" matches "cable".

    Deliberately not a real stemmer: it only has to be consistent on both sides
    of a comparison. Short words are left alone so units like "pcs" survive.
    """
    if len(word) <= 3:
        return word
    if word.endswith(("xes", "ses", "zes", "ches", "shes")):
        return word[:-2]
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ss"):
        return word
    if word.endswith("s"):
        return word[:-1]
    return word


def normalise_unit(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    cleaned = unit.strip().lower()
    return _UNIT_SYNONYMS.get(cleaned, cleaned)


def tokenize(text: Optional[str]) -> set[str]:
    """Content tokens, lowercased, singularised, stopwords removed.

    Hyphens are separators, so "usb type-c cable" and "type c cable" share
    three of their tokens instead of being two unequal strings.
    """
    if not text:
        return set()
    tokens = set()
    for raw in _TOKEN_RE.findall(text.lower()):
        # Single letters are kept -- the "C" in Type-C is the whole distinction
        # between two otherwise identical cables. Single digits are dropped as
        # noise from quantities and titles.
        if raw in _STOPWORDS or (len(raw) < 2 and not raw.isalpha()):
            continue
        word = singularise(raw)
        if word not in _STOPWORDS:
            tokens.add(word)
    return tokens


def tag_tokens(tags: list[str]) -> set[str]:
    """Flatten a tag list into one token set.

    Tags were previously compared as whole strings, which meant the tag
    "type c cable" did not match "usb type-c cable" at all -- a near-identical
    product scored zero on the dimension that matters most.
    """
    return tokenize(" ".join(tags)) if tags else set()


def _overlap(left: set[str], right: set[str]) -> float:
    """Proportion of the smaller set that the larger one covers.

    Plain Jaccard punishes a detailed RFQ for being detailed: a seller listing
    twelve attributes would score badly against a buyer who named three, even
    when all three match. Dividing by the smaller set asks the question that
    actually matters -- "is everything the requester asked for present?"
    """
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def relevance_score(
    requester_tags: list[str],
    requester_text: Optional[str],
    candidate_tags: list[str],
    candidate_text: Optional[str],
) -> float:
    """Lexical similarity between two RFQs.

    NOTE: this is term overlap, not vector similarity. It occupies the slot that
    Qdrant retrieval will fill; the weighting and the rest of the pipeline do not
    change when it is swapped.
    """
    tag_overlap = _overlap(tag_tokens(requester_tags), tag_tokens(candidate_tags))
    text_overlap = _overlap(tokenize(requester_text), tokenize(candidate_text))

    # Tags are curated and weigh more, but text carries the long tail.
    if requester_tags and candidate_tags:
        return 0.6 * tag_overlap + 0.4 * text_overlap
    return text_overlap


def category_score(requester_category: str, candidate_category: str) -> float:
    """Token overlap, not string equality.

    Categories are typed by hand, so "electronic" and "Electronics" are the same
    category to everyone except a string comparison.
    """
    return _overlap(tokenize(requester_category), tokenize(candidate_category))


def _value_similarity(wanted: Any, offered: Any) -> float:
    """How well one attribute value satisfies another, from 0 to 1.

    Graded rather than binary: asking for 20,000 mAh and being offered 27,000
    is a near miss, not a total one, while 5,000 clearly is not.

    Normalised measurements are compared numerically. They used to fall through
    to token overlap on the stringified dict, where {"value": 65, "unit": "W"}
    and {"value": 18, "unit": "W"} "matched" on the shared words value/unit/W --
    so every numeric spec scored full marks regardless of the number.
    """
    if isinstance(wanted, bool) or isinstance(offered, bool):
        return 1.0 if bool(wanted) == bool(offered) else 0.0

    if (
        isinstance(wanted, dict) and isinstance(offered, dict)
        and "value" in wanted and "value" in offered
    ):
        if str(wanted.get("unit", "")).lower() != str(offered.get("unit", "")).lower():
            return 0.0
        return _value_similarity(wanted["value"], offered["value"])

    if isinstance(wanted, (int, float)) and isinstance(offered, (int, float)):
        left, right = float(wanted), float(offered)
        if left == right:
            return 1.0
        largest = max(abs(left), abs(right))
        if largest == 0:
            return 1.0
        return max(0.0, 1.0 - abs(left - right) / largest)

    # Dimensions and other nested shapes: equal or not.
    if isinstance(wanted, dict) or isinstance(offered, dict):
        return 1.0 if wanted == offered else 0.0

    # --- Text / categorical values: synonym-aware comparison ---
    # Convert lists/tuples to space-separated strings for comparison.
    w_str = " ".join(map(str, wanted)) if isinstance(wanted, (list, tuple)) else str(wanted)
    o_str = " ".join(map(str, offered)) if isinstance(offered, (list, tuple)) else str(offered)

    return _semantic_text_similarity(w_str, o_str)


# Suffix the UI appends to flag an attribute for heavier matching weight.
_MUST_MATCH_SUFFIX = "__must_match"

# Must-match attributes count this many times more than regular ones.
_MUST_MATCH_WEIGHT = 3.0

# When a candidate does not mention a must-match attribute at all, it gets this
# score instead of the normal 0.5 — silence is more suspect when the requester
# explicitly flagged the attribute.
_MUST_MATCH_MISSING = 0.15


def attribute_score(
    wanted: dict[str, Any], offered: dict[str, Any]
) -> Optional[float]:
    """How well the candidate satisfies the attributes the requester named.

    A candidate that declares the attribute and differs scores zero for it. One
    that never mentions it scores a half: silence is not a contradiction, but it
    is not a confirmation either, so it should not outrank a stated match.

    Attributes flagged with a sibling ``<key>__must_match`` key in the
    requester's product_details carry 3× weight and a harsher missing-value
    penalty, so the weighted average shifts towards what the requester cares
    about most.
    """
    # Collect which attributes the requester flagged as critical.
    must_match_keys: set[str] = set()
    for key, value in wanted.items():
        if key.endswith(_MUST_MATCH_SUFFIX) and value:
            must_match_keys.add(key[: -len(_MUST_MATCH_SUFFIX)])

    # Filter to real, non-empty attributes (skip name keys, derived quantity keys, and metadata keys).
    requested = {
        key: value
        for key, value in wanted.items()
        if key not in _PRODUCT_NAME_KEYS
        and key not in _DERIVED_QUANTITY_KEYS
        and not key.endswith(_MUST_MATCH_SUFFIX)
        and value not in (None, "")
    }
    if not requested:
        return None

    # Also strip __must_match keys and derived quantity keys from the offered side (they are metadata).
    offered_clean = {
        key: value
        for key, value in offered.items()
        if not key.endswith(_MUST_MATCH_SUFFIX)
        and key not in _DERIVED_QUANTITY_KEYS
    }

    total = 0.0
    weight_sum = 0.0
    for key, value in requested.items():
        is_must = key in must_match_keys
        weight = _MUST_MATCH_WEIGHT if is_must else 1.0

        if key not in offered_clean or offered_clean[key] in (None, ""):
            score = _MUST_MATCH_MISSING if is_must else 0.5
        else:
            score = _value_similarity(value, offered_clean[key])

        total += weight * score
        weight_sum += weight

    return total / weight_sum if weight_sum else None


def price_score(
    buyer_target: Optional[Decimal],
    seller_ask: Optional[Decimal],
    buyer_currency: Optional[str] = None,
    seller_currency: Optional[str] = None,
) -> Optional[float]:
    """1.0 when the seller is at or under the buyer's target, decaying above it.

    Different currencies are converted to the buyer's before comparing, using
    the static table in ``services/currency``. An unknown currency still yields
    None: a fabricated comparison is worse than an absent one.
    """
    if buyer_target is None or seller_ask is None or buyer_target <= 0:
        return None

    if buyer_currency and seller_currency and buyer_currency != seller_currency:
        converted = currency.convert(seller_ask, seller_currency, buyer_currency)
        if converted is None:
            return None
        seller_ask = converted

    if seller_ask <= buyer_target:
        return 1.0

    # Linear decay: at twice the target the score reaches zero.
    overshoot = float((seller_ask - buyer_target) / buyer_target)
    return max(0.0, 1.0 - overshoot)


def quantity_score(
    needed: Optional[Decimal],
    needed_unit: Optional[str],
    available: Optional[Decimal],
    available_unit: Optional[str],
    needed_attributes: Optional[dict[str, Any]] = None,
    available_attributes: Optional[dict[str, Any]] = None,
) -> Optional[float]:
    """Full marks when the seller can cover the buyer's requirement.

    A partial fill still scores proportionally rather than zero -- two sellers at
    half the volume each is a real outcome in this market.

    Supports mass-to-mass conversions (e.g. kg to tonnes, quintals to kg)
    and packaging conversions (e.g. 1,000 boras of 55 kg to bulk tonnes).
    """
    if needed is None or available is None or needed <= 0:
        return None

    # Try resolving both sides to kilograms (kg)
    needed_kg: Optional[Decimal] = None
    if needed_attributes and "total_weight_kg" in needed_attributes:
        try:
            needed_kg = Decimal(str(needed_attributes["total_weight_kg"]))
        except (InvalidOperation, ValueError, TypeError):
            pass
    if needed_kg is None and needed_unit and unit_converter.is_mass_unit(needed_unit):
        needed_kg = unit_converter.to_kg(needed, needed_unit)
    if needed_kg is None and needed_attributes:
        pw_kg = needed_attributes.get("pack_weight_kg")
        if pw_kg is not None:
            try:
                needed_kg = needed * Decimal(str(pw_kg))
            except (InvalidOperation, ValueError, TypeError):
                pass

    available_kg: Optional[Decimal] = None
    if available_attributes and "total_weight_kg" in available_attributes:
        try:
            available_kg = Decimal(str(available_attributes["total_weight_kg"]))
        except (InvalidOperation, ValueError, TypeError):
            pass
    if available_kg is None and available_unit and unit_converter.is_mass_unit(available_unit):
        available_kg = unit_converter.to_kg(available, available_unit)
    if available_kg is None and available_attributes:
        pw_kg = available_attributes.get("pack_weight_kg")
        if pw_kg is not None:
            try:
                available_kg = available * Decimal(str(pw_kg))
            except (InvalidOperation, ValueError, TypeError):
                pass

    if needed_kg is not None and available_kg is not None:
        if needed_kg <= 0:
            return None
        if available_kg >= needed_kg:
            return 1.0
        return max(0.0, min(1.0, float(available_kg / needed_kg)))

    left, right = normalise_unit(needed_unit), normalise_unit(available_unit)
    if left and right and left != right:
        # Comparing 500 kg to 500 cartons would be nonsense.
        return None

    if available >= needed:
        return 1.0
    return float(available / needed)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


# Freight cost and lead time do not fall off in a straight line, so neither does
# this score. Inside a metro the difference is a rounding error; past that, an
# extra 400km is the difference between a day's drive and a multi-day haul.
#
# The old linear decay to 1500km read far too generously: Jaipur to Ludhiana is
# 444km -- two days by road, a different state, a different set of transporters
# -- and it scored 0.73, which on the card looks like a near-local supplier.
_SAME_METRO_KM = 50.0
# Distance over which the score falls by a factor of e once past the metro band.
_DECAY_KM = 450.0


def distance_km(
    requester: dict[str, Optional[object]], candidate: dict[str, Optional[object]]
) -> Optional[float]:
    """Great-circle distance between two locations, when both are geocoded."""
    r_lat, r_lng = requester.get("latitude"), requester.get("longitude")
    c_lat, c_lng = candidate.get("latitude"), candidate.get("longitude")
    if None in (r_lat, r_lng, c_lat, c_lng):
        return None
    return haversine_km(
        float(r_lat), float(r_lng), float(c_lat), float(c_lng)  # type: ignore[arg-type]
    )


def _administrative_score(
    requester: dict[str, Optional[object]], candidate: dict[str, Optional[object]]
) -> Optional[float]:
    """What the city/state/country fields alone say about proximity."""

    def same(field: str) -> bool:
        left, right = requester.get(field), candidate.get(field)
        return bool(left and right and str(left).strip().lower() == str(right).strip().lower())

    if same("city"):
        return 1.0
    if same("state"):
        return 0.6
    if same("country"):
        return 0.3
    # Both sides stated a location and they share no level of it: that is a
    # cross-border match, which is a real answer rather than a missing one.
    if any(requester.get(f) for f in ("city", "state", "country")) and any(
        candidate.get(f) for f in ("city", "state", "country")
    ):
        return 0.1
    return None


def location_score(
    requester: dict[str, Optional[object]], candidate: dict[str, Optional[object]]
) -> Optional[float]:
    """Proximity from 0 to 1, by distance where possible.

    The two signals are combined with ``max`` rather than distance winning
    outright: a domestic supplier 1,200km away is still domestic -- no customs,
    no currency, one set of transport rules -- so it should not score below an
    ungeocoded counterparty in the same country.
    """
    administrative = _administrative_score(requester, candidate)

    distance = distance_km(requester, candidate)
    if distance is None:
        return administrative

    if distance <= _SAME_METRO_KM:
        geographic = 1.0
    else:
        geographic = math.exp(-(distance - _SAME_METRO_KM) / _DECAY_KM)

    if administrative is None:
        return round(geographic, 4)
    return round(max(geographic, administrative), 4)


def deadline_score(
    requester_deadline: Optional[datetime], candidate_deadline: Optional[datetime]
) -> Optional[float]:
    """Whether the counterparty's window still covers the requester's date."""
    if requester_deadline is None or candidate_deadline is None:
        return None

    if candidate_deadline >= requester_deadline:
        return 1.0

    # The counterparty's window closes first; decay over a fortnight of shortfall.
    shortfall_days = (requester_deadline - candidate_deadline).total_seconds() / 86400
    return max(0.0, 1.0 - shortfall_days / 14)


def blend(scores: dict[str, Optional[float]]) -> float:
    """Weighted mean over the dimensions that could actually be evaluated."""
    total = weight_sum = 0.0
    for dimension, value in scores.items():
        if value is None:
            continue
        weight = WEIGHTS.get(dimension, 0.0)
        total += weight * value
        weight_sum += weight
    if weight_sum == 0:
        return 0.0
    return round(total / weight_sum, 4)


def estimate_logistics(
    distance_km: Optional[float], is_cross_border: bool = False
) -> dict[str, Any]:
    """Estimate transit time, freight mode, and domestic/customs status."""
    if distance_km is None:
        if is_cross_border:
            return {
                "mode": "International Maritime / Air",
                "transit_days_min": 7,
                "transit_days_max": 25,
                "label": "Cross-Border Freight (~1–3 weeks)",
                "customs_required": True,
            }
        return {
            "mode": "Regional Ground Transport",
            "transit_days_min": 2,
            "transit_days_max": 5,
            "label": "Domestic Transit (~2–5 days)",
            "customs_required": False,
        }

    if is_cross_border or distance_km > 2500:
        return {
            "mode": "Cross-Border Intermodal Freight",
            "transit_days_min": 7,
            "transit_days_max": 21,
            "label": f"International Shipping ({round(distance_km)} km · ~1–3 weeks)",
            "customs_required": True,
        }
    if distance_km <= 50:
        return {
            "mode": "Local Metro Dispatch",
            "transit_days_min": 0,
            "transit_days_max": 1,
            "label": f"Local Delivery ({round(distance_km)} km · Same day)",
            "customs_required": False,
        }
    if distance_km <= 500:
        return {
            "mode": "Direct Road Freight",
            "transit_days_min": 1,
            "transit_days_max": 2,
            "label": f"Direct Truckload ({round(distance_km)} km · 1–2 days)",
            "customs_required": False,
        }
    return {
        "mode": "Long-Haul Domestic Freight",
        "transit_days_min": 2,
        "transit_days_max": 5,
        "label": f"Domestic Transit ({round(distance_km)} km · 2–5 days)",
        "customs_required": False,
    }


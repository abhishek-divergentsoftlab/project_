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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Optional

from services import currency, embeddings, hsn_service, product_synonyms, unit_converter
from services.locations import normalise_country

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
    "mt": "tonne", "t": "tonne", "metric ton": "tonne", "metric tons": "tonne",
    "metric tonne": "tonne", "metric tonnes": "tonne",
    "quintal": "quintal", "quintals": "quintal", "qtl": "quintal",
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "box": "box", "boxes": "box", "carton": "carton", "cartons": "carton",
    "roll": "roll", "rolls": "roll", "set": "set", "sets": "set",
    "bora": "bora", "boras": "bora", "bori": "bora", "boris": "bora",
    "katta": "bora", "kattas": "bora",
    "bag": "bag", "bags": "bag", "sack": "bag", "sacks": "bag",
    "dozen": "dozen", "dozens": "dozen", "doz": "dozen", "dz": "dozen",
    "pair": "pair", "pairs": "pair",
    "l": "l", "litre": "l", "litres": "l", "liter": "l", "liters": "l",
    # Hindi / Mandi synonyms
    "क्विंटल": "quintal", "क्विंतल": "quintal",
    "किलो": "kg", "किलोग्राम": "kg", "कीलो": "kg",
    "टन": "tonne", "मीट्रिक टन": "tonne",
    "ग्राम": "g",
    "दर्जन": "dozen",
    "पीस": "pcs", "नग": "pcs",
    "बोरी": "bora", "बोरा": "bora", "कट्टा": "bora", "कट्टे": "bora",
    "बैग": "bag", "थैला": "bag",
    "कार्टन": "carton", "पेटी": "carton",
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

    # --- Out-of-the-Box Varieties & Attributes ---
    "hair spray": {
        "hair spray": 1.0, "hair sprey": 1.0, "hairspray": 1.0,
        "hair lacquer": 0.90, "finishing mist": 0.85, "styling spray": 0.90,
    },
    "hair color": {
        "hair color": 1.0, "hair colour": 1.0, "hair dye": 1.0,
        "hair tint": 0.90, "hair toner": 0.85, "permanent color": 0.90,
    },
    "shampoo": {
        "shampoo": 1.0, "hair cleanser": 0.95, "scalp cleanser": 0.90,
        "hair wash": 0.90,
    },
    "corn": {
        "corn": 1.0, "sweet corn": 0.95, "maize": 1.0, "yellow maize": 0.95,
        "baby corn": 0.85, "dent corn": 0.90, "popping corn": 0.85,
    },
    "brake pads": {
        "brake pads": 1.0, "brake pad": 1.0, "disc pads": 0.95,
        "friction pads": 0.90,
    },
    "solar panel": {
        "solar panel": 1.0, "solar panels": 1.0, "pv module": 0.95,
        "solar module": 0.95, "photovoltaic panel": 0.95,
    },
}

# Multi-mapping lookup: token → list of (canonical_group, similarity)
# Allows homonyms (e.g. "flat" as finish 0.90 vs "flat" as shape 1.0) to coexist without overwriting
_SYNONYM_LOOKUP: dict[str, list[tuple[str, float]]] = {}
for _canonical, _variants in _SYNONYM_GROUPS.items():
    for _variant, _sim in _variants.items():
        _key = _variant.lower().strip()
        if _key not in _SYNONYM_LOOKUP:
            _SYNONYM_LOOKUP[_key] = []
        _SYNONYM_LOOKUP[_key].append((_canonical, _sim))


def _resolve_all_synonyms(text: str) -> list[tuple[str, float]]:
    """Resolve a text value to all canonical synonym groups it belongs to."""
    cleaned = text.strip().lower()
    matches = list(_SYNONYM_LOOKUP.get(cleaned, []))
    singular = singularise(cleaned)
    if singular != cleaned and singular in _SYNONYM_LOOKUP:
        for canon, sim in _SYNONYM_LOOKUP[singular]:
            if not any(c == canon for c, _ in matches):
                matches.append((canon, sim))
    return matches


def _resolve_synonym(text: str) -> tuple[Optional[str], float]:
    """Backwards-compatible: returns the highest-similarity canonical match."""
    matches = _resolve_all_synonyms(text)
    if not matches:
        return None, 0.0
    return max(matches, key=lambda cs: cs[1])


def _semantic_text_similarity(wanted: str, offered: str) -> float:
    """Synonym-aware text comparison for attribute values.

    Resolution order:
    1. Exact string match (case-insensitive) → 1.0
    2. Shared synonym group match → max(min(sim_wanted, sim_offered) for shared groups)
       Handles homonyms: "matte" vs "flat" matches via shared "matte" finish group at 0.90!
    3. Multi-token: resolve each token, compute overlap on canonical forms
    4. Fallback: original token overlap with 0.4 discount
    """
    w = wanted.strip().lower()
    o = offered.strip().lower()

    # 1. Exact string match
    if w == o:
        return 1.0

    # 2. Multi-concept synonym resolution: do they share ANY canonical concept?
    w_matches = _resolve_all_synonyms(w)
    o_matches = _resolve_all_synonyms(o)

    if w_matches and o_matches:
        shared_sims = [
            min(w_sim, o_sim)
            for w_canon, w_sim in w_matches
            for o_canon, o_sim in o_matches
            if w_canon == o_canon
        ]
        if shared_sims:
            return max(shared_sims)
        # Both resolved to known concepts with zero intersection (e.g. "red" vs "blue")
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
            return resolved_overlap * 0.4 * min_sim
            # Partial overlap on multi-token values (e.g. "SS 316L" vs "SS 304"
            # sharing only "stainless steel") gets the 0.4 discount — the
            # unmatched tokens (grade numbers) are the distinguishing part.
            return resolved_overlap * 0.4 * min_sim

    # 4. Fallback: raw token overlap with 0.4 penalty (original behaviour)
    if w_tokens and o_tokens:
        raw_overlap = _overlap(w_tokens, o_tokens)
        if raw_overlap >= 0.999:
            return raw_overlap
        if raw_overlap > 0:
            return raw_overlap * 0.4

    # 5. Dynamic Open-World Semantic Fallback (HSN Codes + Dense Vector Embeddings)
    dynamic = _dynamic_semantic_similarity(w, o)
    if dynamic is not None and dynamic > 0.0:
        return dynamic

    return 0.0


@lru_cache(maxsize=20000)
def _dynamic_semantic_similarity(w: str, o: str) -> Optional[float]:
    """Zero-shot open-world semantic similarity via HSN codes & dense vector embeddings."""
    if not w or not o or len(w) < 2 or len(o) < 2:
        return None

    # 1. HSN Customs Classification
    try:
        ha = hsn_service.predict_hsn_from_text(w)
        hb = hsn_service.predict_hsn_from_text(o)
        h_sim = hsn_service.hsn_similarity(ha, hb)
        if h_sim is not None and h_sim >= 0.88:
            return h_sim
    except Exception:
        pass

    # 2. Vector Embedding Cosine Similarity
    try:
        vecs = embeddings._embed_sync([w, o])
        if vecs and len(vecs) == 2:
            dot = sum(a * b for a, b in zip(vecs[0], vecs[1]))
            m1 = math.sqrt(sum(a * a for a in vecs[0]))
            m2 = math.sqrt(sum(b * b for b in vecs[1]))
            if m1 > 0 and m2 > 0:
                cos = dot / (m1 * m2)
                if cos >= 0.85:
                    return round(cos, 4)
                return 0.0
    except Exception:
        pass

    return None


def hsn_score(buyer_hsn: Optional[str], seller_hsn: Optional[str]) -> Optional[float]:
    """Customs-grade HSN hierarchical compatibility score.

    Uses World Customs Organization 6-digit subheading and 4-digit heading logic:
    - 6-digit match: 1.0 (Identical tariff commodity)
    - 4-digit match: 0.88 (Direct commercial substitute)
    - 2-digit match: 0.55 (Same industrial chapter)
    - Chapter mismatch: 0.0 (Different industries)
    """
    return hsn_service.hsn_similarity(buyer_hsn, seller_hsn)

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
        "gst invoice provided ready stock yes no inr india "
        # Quantity words. "500 kg" is scored by the quantity dimension; left in
        # the text it made every bulk listing share vocabulary.
        "kg kgs tonne ton mt pc pcs piece quintal qtl dozen lakh crore"
    ).split()
)

_TOKEN_RE = re.compile(r"[a-z0-9\u0900-\u097f]+", re.IGNORECASE)


# Plurals the suffix rules below would get wrong: words whose singular itself
# ends in -s (bus, gas, lens) take -es, and a plain "strip -es" would also turn
# "cases" into "cas".
_IRREGULAR_PLURALS: dict[str, str] = {
    "buses": "bus", "gases": "gas", "lenses": "lens", "canvases": "canvas",
    "bonuses": "bonus", "statuses": "status", "campuses": "campus",
    "viruses": "virus", "atlases": "atlas", "cactuses": "cactus",
    "focuses": "focus", "censuses": "census", "walruses": "walrus",
    "quizzes": "quiz", "series": "series", "species": "species",
    "news": "news", "chassis": "chassis",
}
# Words ending in -oes whose singular keeps the e (shoe, canoe).
_OE_SINGULARS = ("shoes", "canoes", "oboes", "floes", "throes", "tiptoes", "sloes")


def singularise(word: str) -> str:
    """Crude but predictable stemming, so "cables" matches "cable".

    Deliberately not a real stemmer: it only has to be consistent on both sides
    of a comparison. Short words are left alone so units like "pcs" survive.

    ``tomatoes`` -> ``tomato`` (-oes), ``boxes`` -> ``box``, ``glasses`` ->
    ``glass``, ``cases`` -> ``case``, ``buses`` -> ``bus``.
    """
    if len(word) <= 3:
        return word
    irregular = _IRREGULAR_PLURALS.get(word)
    if irregular is not None:
        return irregular
    if word.endswith("oes") and len(word) >= 6 and not word.endswith(_OE_SINGULARS):
        return word[:-2]  # tomatoes, potatoes, mangoes, cargoes, echoes
    if word.endswith(("xes", "sses", "ches", "shes")):
        return word[:-2]  # boxes, glasses, inches, dishes
    if word.endswith("zzes"):
        return word[:-3]
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ss"):
        return word
    if word.endswith("s"):
        return word[:-1]  # cables, cases, sizes, shoes, onions
    return word


def normalise_unit(unit: Optional[str]) -> Optional[str]:
    cleaned = unit_converter.clean_unit(unit)
    if not cleaned:
        return None
    return _UNIT_SYNONYMS.get(cleaned, cleaned)


def product_token(raw: str) -> str:
    """One word as matching compares it: singular, and in English.

    "tomatoes" -> "tomato", "pyaz" -> "onion", "chillies" -> "chilli".
    """
    canonical = product_synonyms.canonical_token(raw)
    if canonical is not None:
        return canonical
    word = singularise(raw)
    return product_synonyms.canonical_token(word) or word


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
        # Canonical English form, so "pyaz" and "onion" are one token.
        word = product_token(raw)
        if word not in _STOPWORDS:
            tokens.update(word.split())
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


def _symmetric_overlap(left: set[str], right: set[str]) -> float:
    """Ochiai (set cosine): |L & R| / sqrt(|L| * |R|).

    Symmetric, so neither side can win by being vague. Dividing by the smaller
    set (``_overlap``) gave a one-word "cable" listing full marks against a
    detailed "hdmi braided 8k cable" request. Ochiai is the geometric mean of
    the two coverages: a detailed seller who covers everything a three-word
    request named still scores 0.5 against a 12-token listing, where Dice
    (the harmonic mean) would give it 0.4.
    """
    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


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
    tag_overlap = _symmetric_overlap(tag_tokens(requester_tags), tag_tokens(candidate_tags))
    text_overlap = _symmetric_overlap(tokenize(requester_text), tokenize(candidate_text))

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

    # Physical dimensional measurement comparison (e.g. 2cm vs 20mm -> 1.0, 1 tonne vs 1000kg -> 1.0)
    meas_sim = unit_converter.compare_measurements(wanted, None, offered, None)
    if meas_sim is not None:
        return meas_sim

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


# Synonymous attribute keys across different industry conventions (fully bidirectional).
_RAW_KEY_SYNONYMS: tuple[frozenset[str], ...] = (
    frozenset({"type", "variety", "grade", "kind", "category", "farming_type", "quality", "nature", "spec"}),
    frozenset({"grade", "type", "quality", "standard", "alloy", "steel_grade", "purity"}),
    frozenset({"material", "fabric", "composition", "alloy", "metal", "fiber", "cloth", "substance"}),
    frozenset({"port", "connector", "interface", "plug", "socket", "connection"}),
    frozenset({"dia", "diameter", "thickness", "gauge", "width", "od", "id", "size"}),
    frozenset({"thickness", "dia", "diameter", "gauge", "depth", "width"}),
    frozenset({"length", "len", "bar_len", "span", "dimension"}),
    frozenset({"finish", "coating", "treatment", "surface", "plating"}),
    frozenset({"color", "colour", "shade", "hue", "tint"}),
    frozenset({"certification", "certified", "standard", "compliance"}),
)
_KEY_SYNONYMS: dict[str, frozenset[str]] = {}
for _group in _RAW_KEY_SYNONYMS:
    for _k in _group:
        _KEY_SYNONYMS[_k] = _KEY_SYNONYMS.get(_k, frozenset()) | _group


def _find_best_attribute_match(wanted_key: str, wanted_val: Any, offered: dict[str, Any]) -> tuple[float, bool]:
    """Finds best matching attribute in offered dict, supporting:
    1. Direct key match (e.g., {"color": "black"} vs {"color": "black"})
    2. Key-to-Value Inversion (e.g., {"type": "organic"} vs {"organic": True})
    3. Key Synonyms (e.g., {"material": "cotton"} vs {"fabric": "cotton"})
    4. Cross-Measurement Match (e.g. {"dia": "20mm"} vs {"thickness": "2cm"})

    Returns (score: float, found: bool).
    """
    wk_clean = wanted_key.strip().lower()

    # 1. Direct key match
    if wanted_key in offered and offered[wanted_key] not in (None, ""):
        return _value_similarity(wanted_val, offered[wanted_key]), True
    for k in offered:
        if k.strip().lower() == wk_clean and offered[k] not in (None, ""):
            return _value_similarity(wanted_val, offered[k]), True

    # 2. Key-to-Value Inversion:
    # Case A: Requester wanted string value (e.g. type: "organic") and counterparty has boolean key (e.g. organic: True)
    if isinstance(wanted_val, str) and wanted_val.strip():
        val_clean = wanted_val.strip().lower()
        for ok, ov in offered.items():
            ok_clean = ok.strip().lower()
            if ok_clean == val_clean or val_clean in ok_clean or ok_clean.endswith(f"_{val_clean}"):
                if isinstance(ov, bool) and ov:
                    return 1.0, True
                if str(ov).strip().lower() in ("true", "yes", "1", "certified", val_clean):
                    return 1.0, True

    # Case B: Requester specified boolean (e.g. organic: True, is_certified: True) and counterparty has string value
    if isinstance(wanted_val, bool) and wanted_val:
        stem = wk_clean
        if stem.startswith("is_"):
            stem = stem[3:]
        for ok, ov in offered.items():
            ov_clean = str(ov).strip().lower()
            if ov_clean in (wk_clean, f"is_{wk_clean}", stem) or (stem and stem in ov_clean):
                return 1.0, True

    # 3. Key Synonym Resolution
    synonyms = _KEY_SYNONYMS.get(wk_clean, frozenset())
    for ok, ov in offered.items():
        ok_clean = ok.strip().lower()
        if ok_clean in synonyms:
            sim = _value_similarity(wanted_val, ov)
            if sim >= 0.70:
                return sim, True

    # 4. Cross-Measurement Match
    # If wanted_val is a measurement, check if any offered measurement in the same dimension matches
    if unit_converter.parse_measurement(wanted_val) is not None:
        for ok, ov in offered.items():
            meas_sim = unit_converter.compare_measurements(wanted_val, None, ov, None)
            if meas_sim is not None and meas_sim >= 0.70:
                return meas_sim, True

    return 0.5, False


# Suffix the UI appends to flag an attribute for heavier matching weight.
_MUST_MATCH_SUFFIX = "__must_match"
_MUST_MATCH_WEIGHT = 3.0
_MUST_MATCH_MISSING = 0.15


def attribute_score(
    wanted: dict[str, Any], offered: dict[str, Any]
) -> Optional[float]:
    """How well the candidate satisfies the attributes the requester named.

    Supports Key-to-Value Inversion, Key Synonym Graphs, and Multi-Dimensional
    Unit Normalization (e.g. 2cm vs 20mm -> 1.0).
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

        score, found = _find_best_attribute_match(key, value, offered_clean)
        if not found:
            score = _MUST_MATCH_MISSING if is_must else 0.5

        total += weight * score
        weight_sum += weight

    return total / weight_sum if weight_sum else None


def _per_unit_prices(
    buyer_target: Decimal,
    buyer_unit: Optional[str],
    seller_ask: Decimal,
    seller_unit: Optional[str],
    buyer_attributes: Optional[dict[str, Any]] = None,
    seller_attributes: Optional[dict[str, Any]] = None,
) -> Optional[tuple[Decimal, Decimal]]:
    """Both prices restated per the same unit, or None if that is impossible.

    Units resolvable to a base (kg, pcs, m, l) are converted: 28,000/tonne and
    30/kg become 28 and 30 per kg. Units without a fixed base ("bora",
    "carton") resolve via pack_weight_kg if supplied (or default 50kg for standard
    agricultural bags/boras) or compare directly when they are the same word.
    When only one side, or neither, said what the price is per, both are assumed
    to be quoted on the same basis -- the same assumption the listing form makes.
    """
    left, right = normalise_unit(buyer_unit), normalise_unit(seller_unit)
    if not left or not right:
        return buyer_target, seller_ask

    b = unit_converter.price_per_base_unit(buyer_target, left)
    if b is None and unit_converter.is_packaging_unit(left):
        pw_kg = None
        if buyer_attributes:
            pw_kg = buyer_attributes.get("pack_weight_kg") or buyer_attributes.get("pack_weight")
        if pw_kg is None and left in ("bora", "boras", "bori", "katta", "kattas", "bag", "bags", "sack", "sacks", "बोरी", "बोरा", "कट्टा"):
            pw_kg = Decimal("50")
        if pw_kg is not None:
            try:
                pw_dec = Decimal(str(pw_kg))
                if pw_dec > 0:
                    b = (buyer_target / pw_dec, "kg")
            except (InvalidOperation, ValueError, TypeError):
                pass

    s = unit_converter.price_per_base_unit(seller_ask, right)
    if s is None and unit_converter.is_packaging_unit(right):
        pw_kg = None
        if seller_attributes:
            pw_kg = seller_attributes.get("pack_weight_kg") or seller_attributes.get("pack_weight")
        if pw_kg is None and right in ("bora", "boras", "bori", "katta", "kattas", "bag", "bags", "sack", "sacks", "बोरी", "बोरा", "कट्टा"):
            pw_kg = Decimal("50")
        if pw_kg is not None:
            try:
                pw_dec = Decimal(str(pw_kg))
                if pw_dec > 0:
                    s = (seller_ask / pw_dec, "kg")
            except (InvalidOperation, ValueError, TypeError):
                pass

    if b is not None and s is not None:
        if b[1] != s[1]:
            return None  # per kg vs per piece: not the same thing
        return b[0], s[0]
    if left == right:
        return buyer_target, seller_ask
    return None


def price_score(
    buyer_target: Optional[Decimal],
    seller_ask: Optional[Decimal],
    buyer_currency: Optional[str] = None,
    seller_currency: Optional[str] = None,
    buyer_unit: Optional[str] = None,
    seller_unit: Optional[str] = None,
    buyer_attributes: Optional[dict[str, Any]] = None,
    seller_attributes: Optional[dict[str, Any]] = None,
) -> Optional[float]:
    """1.0 when the seller is at or under the buyer's target, decaying above it.

    Unscored (None) rather than guessed when:

    * either price is missing or not positive -- a 0 is "price on request",
      not a free offer, and must not beat every real quote;
    * either currency is missing -- 100 of an unknown currency is not
      comparable to 90 USD;
    * a currency is unknown to the conversion table;
    * the units are incomparable (per kg vs per piece).

    Different currencies are converted to the buyer's first, then both prices
    are restated per a common base unit (see ``_per_unit_prices``).
    """
    if buyer_target is None or seller_ask is None:
        return None
    try:
        b_target = Decimal(str(buyer_target))
        s_ask = Decimal(str(seller_ask))
    except (InvalidOperation, TypeError, ValueError):
        return None

    if b_target <= 0 or s_ask <= 0:
        return None

    buyer_ccy = (buyer_currency or "").strip().upper() or None
    seller_ccy = (seller_currency or "").strip().upper() or None
    if (buyer_ccy is None and seller_ccy is not None) or (buyer_ccy is not None and seller_ccy is None):
        return None
    if buyer_ccy and seller_ccy and buyer_ccy != seller_ccy:
        converted = currency.convert(s_ask, seller_ccy, buyer_ccy)
        if converted is None:
            return None
        s_ask = converted

    pair = _per_unit_prices(
        b_target, buyer_unit, s_ask, seller_unit,
        buyer_attributes=buyer_attributes, seller_attributes=seller_attributes,
    )
    if pair is None:
        return None
    b_target, s_ask = pair
    if b_target <= 0:
        return None

    if s_ask <= b_target:
        return 1.0

    # Linear decay: at twice the target the score reaches zero.
    overshoot = float((s_ask - b_target) / b_target)
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
    if needed is None or available is None:
        return None
    try:
        needed_d = Decimal(str(needed))
        available_d = Decimal(str(available))
    except (InvalidOperation, TypeError, ValueError):
        return None

    if needed_d <= 0:
        return None

    # Try resolving both sides to kilograms (kg)
    needed_kg: Optional[Decimal] = None
    if needed_attributes and "total_weight_kg" in needed_attributes:
        try:
            needed_kg = Decimal(str(needed_attributes["total_weight_kg"]))
        except (InvalidOperation, ValueError, TypeError):
            pass
    if needed_kg is None and needed_unit and unit_converter.is_mass_unit(needed_unit):
        needed_kg = unit_converter.to_kg(needed_d, needed_unit)
    if needed_kg is None:
        pw_kg = None
        if needed_attributes:
            pw_kg = needed_attributes.get("pack_weight_kg") or needed_attributes.get("pack_weight")
        clean_nu = unit_converter.clean_unit(needed_unit)
        if pw_kg is None and clean_nu in ("bora", "boras", "bori", "katta", "kattas", "bag", "bags", "sack", "sacks", "बोरी", "बोरा", "कट्टा"):
            pw_kg = Decimal("50")
        if pw_kg is not None:
            try:
                needed_kg = needed_d * Decimal(str(pw_kg))
            except (InvalidOperation, ValueError, TypeError):
                pass

    available_kg: Optional[Decimal] = None
    if available_attributes and "total_weight_kg" in available_attributes:
        try:
            available_kg = Decimal(str(available_attributes["total_weight_kg"]))
        except (InvalidOperation, ValueError, TypeError):
            pass
    if available_kg is None and available_unit and unit_converter.is_mass_unit(available_unit):
        available_kg = unit_converter.to_kg(available_d, available_unit)
    if available_kg is None:
        pw_kg = None
        if available_attributes:
            pw_kg = available_attributes.get("pack_weight_kg") or available_attributes.get("pack_weight")
        clean_au = unit_converter.clean_unit(available_unit)
        if pw_kg is None and clean_au in ("bora", "boras", "bori", "katta", "kattas", "bag", "bags", "sack", "sacks", "बोरी", "बोरा", "कट्टा"):
            pw_kg = Decimal("50")
        if pw_kg is not None:
            try:
                available_kg = available_d * Decimal(str(pw_kg))
            except (InvalidOperation, ValueError, TypeError):
                pass

    if needed_kg is not None and available_kg is not None:
        if needed_kg <= 0:
            return None
        if available_kg >= needed_kg or math.isclose(float(available_kg), float(needed_kg), rel_tol=1e-5):
            return 1.0
        return max(0.0, min(1.0, float(available_kg / needed_kg)))

    left, right = normalise_unit(needed_unit), normalise_unit(available_unit)
    if left and right and left != right:
        # Same dimension in different units: 24 pcs vs 2 dozen, 5 m vs 500 cm.
        lb, rb = unit_converter.base_unit(left), unit_converter.base_unit(right)
        if lb is None or rb is None or lb[1] != rb[1]:
            # Comparing 500 kg to 500 cartons would be nonsense.
            return None
        needed_d, available_d = needed_d * lb[0], available_d * rb[0]
        if needed_d <= 0:
            return None

    if available_d >= needed_d or math.isclose(float(available_d), float(needed_d), rel_tol=1e-5):
        return 1.0
    return max(0.0, min(1.0, float(available_d / needed_d)))


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
    try:
        r_lat_f, r_lng_f = float(r_lat), float(r_lng)
        c_lat_f, c_lng_f = float(c_lat), float(c_lng)
    except (ValueError, TypeError):
        return None

    # Null Island guard: (0.0, 0.0) is often an uninitialized or default coordinate placeholder
    if (r_lat_f == 0.0 and r_lng_f == 0.0) or (c_lat_f == 0.0 and c_lng_f == 0.0):
        return None

    return haversine_km(r_lat_f, r_lng_f, c_lat_f, c_lng_f)


def _administrative_score(
    requester: dict[str, Optional[object]], candidate: dict[str, Optional[object]]
) -> Optional[float]:
    """What the city/state/country fields alone say about proximity."""

    def same(field: str) -> bool:
        left, right = requester.get(field), candidate.get(field)
        return bool(left and right and str(left).strip().lower() == str(right).strip().lower())

    # A city or state name is only local inside one country: Hyderabad in
    # India and Hyderabad in Pakistan are a border apart.
    left_country = normalise_country(requester.get("country"))
    right_country = normalise_country(candidate.get("country"))
    if left_country and right_country and left_country != right_country:
        return 0.1

    # Likewise a city name shared by two states (Aurangabad, Maharashtra and
    # Aurangabad, Bihar) is not the same city.
    states_conflict = bool(
        requester.get("state") and candidate.get("state") and not same("state")
    )
    if same("city") and not states_conflict:
        return 1.0
    if same("state"):
        return 0.6
    if left_country and right_country:
        return 0.3  # same country, different state
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


# Days of lateness over which the deadline score falls from 1 to 0.
_DEADLINE_GRACE_DAYS = 14


def _as_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware for safe cross-timezone comparison."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def deadline_score(
    buyer_need_by: Optional[datetime], seller_ready_by: Optional[datetime]
) -> Optional[float]:
    """Can the seller deliver by the buyer's date? Always the buyer's view.

    A buyer RFQ's ``deadline_at`` is the date the goods are needed by; a seller
    RFQ's is the date they can dispatch by (the card adds transit on top). So
    the arguments are positional by role, not by who is searching: the caller
    passes the buyer side first whichever of them ran the match.

    Ready on or before the need-by date is full marks -- earlier is never worse.
    Every day late costs 1/14th, reaching zero two weeks past the date.
    """
    if buyer_need_by is None or seller_ready_by is None:
        return None

    b_need = _as_utc(buyer_need_by)
    s_ready = _as_utc(seller_ready_by)

    if s_ready <= b_need:
        return 1.0

    late_days = (s_ready - b_need).total_seconds() / 86400
    return max(0.0, 1.0 - late_days / _DEADLINE_GRACE_DAYS)


def blend(
    scores: dict[str, Optional[float]],
    weights: Optional[dict[str, float]] = None,
) -> float:
    """Weighted mean over the dimensions that could actually be evaluated.

    Supports custom dynamic weights passed from user profile preferences.
    """
    active_weights = weights or WEIGHTS
    total = weight_sum = 0.0
    for dimension, value in scores.items():
        if value is None:
            continue
        weight = active_weights.get(dimension, WEIGHTS.get(dimension, 0.0))
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            weight = 0.0
        # A negative weight would let the mean leave [0, 1].
        if not math.isfinite(weight) or weight < 0:
            weight = 0.0
        total += weight * value
        weight_sum += weight
    if weight_sum == 0:
        return 0.0
    return round(min(1.0, max(0.0, total / weight_sum)), 4)


def sanitise_weights(raw: Any) -> dict[str, float]:
    """User-supplied weights made safe to score with.

    Unknown keys and non-numbers are ignored, negatives and non-finite values
    count as 0, dimensions the user did not mention keep their default, and the
    result is renormalised to sum to 1. If nothing usable is left, the defaults
    are returned -- a bad preference must never break matching.
    """
    if not isinstance(raw, dict):
        return dict(WEIGHTS)
    merged = dict(WEIGHTS)
    for key, value in raw.items():
        if key not in WEIGHTS or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        merged[key] = max(0.0, number)
    total = sum(merged.values())
    if total <= 0:
        return dict(WEIGHTS)
    return {key: value / total for key, value in merged.items()}


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


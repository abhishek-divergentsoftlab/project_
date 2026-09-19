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
from decimal import Decimal
from typing import Any, Optional

from services import currency

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

# Units that mean the same thing, so "500 pcs" and "500 pieces" compare.
_UNIT_SYNONYMS: dict[str, str] = {
    "pc": "pcs", "pcs": "pcs", "piece": "pcs", "pieces": "pcs",
    "unit": "pcs", "units": "pcs", "no": "pcs", "nos": "pcs", "each": "pcs",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg",
    "kilograms": "kg",
    "tonne": "tonne", "tonnes": "tonne", "ton": "tonne", "tons": "tonne",
    "mt": "tonne",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "box": "box", "boxes": "box", "carton": "carton", "cartons": "carton",
    "roll": "roll", "rolls": "roll", "set": "set", "sets": "set",
}

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

    if str(wanted).strip().lower() == str(offered).strip().lower():
        return 1.0

    left_tokens = tokenize(" ".join(map(str, wanted)) if isinstance(wanted, (list, tuple)) else str(wanted))
    right_tokens = tokenize(" ".join(map(str, offered)) if isinstance(offered, (list, tuple)) else str(offered))
    if not left_tokens or not right_tokens:
        return 0.0

    # Categorical values are the same or they are not. Partial token overlap
    # mostly reflects a shared prefix -- "SS 316L" against "SS 304" shares only
    # the word "SS" -- so it is heavily discounted rather than read as "close".
    # Numeric attributes keep their graded distance above, where closeness is
    # genuinely meaningful.
    overlap = _overlap(left_tokens, right_tokens)
    return overlap if overlap >= 0.999 else overlap * 0.4


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

    # Filter to real, non-empty attributes (skip name keys and metadata keys).
    requested = {
        key: value
        for key, value in wanted.items()
        if key not in _PRODUCT_NAME_KEYS
        and not key.endswith(_MUST_MATCH_SUFFIX)
        and value not in (None, "")
    }
    if not requested:
        return None

    # Also strip __must_match keys from the offered side (they are metadata).
    offered_clean = {
        key: value
        for key, value in offered.items()
        if not key.endswith(_MUST_MATCH_SUFFIX)
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
) -> Optional[float]:
    """Full marks when the seller can cover the buyer's requirement.

    A partial fill still scores proportionally rather than zero -- two sellers at
    half the volume each is a real outcome in this market.
    """
    if needed is None or available is None or needed <= 0:
        return None

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

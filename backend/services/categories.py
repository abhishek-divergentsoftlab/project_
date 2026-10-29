"""Central category taxonomy and normalization service.

Ensures that variant forms, singular/plural differences, and user typos
(e.g. 'electronic', 'Electronic', 'electronics', 'vagitables') resolve to
consistent canonical category names across the database, search indexes,
and counterparty matching.
"""

import json
import re
from typing import Optional
from services import match_scoring

# Canonical categories displayed in UI and used throughout the marketplace.
CANONICAL_CATEGORIES: tuple[str, ...] = (
    "Electronics",
    "Packaging",
    "Textiles",
    "Agriculture",
    "Industrial",
    "Furniture",
    "Chemicals",
    "Construction",
    "Stationery",
    "Learning",
    "Beauty & Personal Care",
    "Food & Beverages",
    "Healthcare & Medical",
    "Automotive",
    "Solar & Energy",
)

# Valid but not a topical category: what a listing with no better home is stored as.
GENERAL_CATEGORY = "General Goods"

# Common aliases, singular forms, colloquial terms and misspellings -> Canonical Category
CATEGORY_ALIASES: dict[str, str] = {
    # Electronics
    "electronic": "Electronics",
    "electronics": "Electronics",
    "electric": "Electronics",
    "electrical": "Electronics",
    "electricals": "Electronics",
    "consumer electronic": "Electronics",
    "consumer electronics": "Electronics",
    "gadget": "Electronics",
    "gadgets": "Electronics",

    # Packaging
    "package": "Packaging",
    "packages": "Packaging",
    "packaging": "Packaging",
    "packagings": "Packaging",
    "packing": "Packaging",
    "box": "Packaging",
    "boxes": "Packaging",
    "carton": "Packaging",
    "cartons": "Packaging",

    # Textiles
    "textile": "Textiles",
    "textiles": "Textiles",
    "fabric": "Textiles",
    "fabrics": "Textiles",
    "garment": "Textiles",
    "garments": "Textiles",
    "apparel": "Textiles",
    "apparels": "Textiles",
    "cloth": "Textiles",
    "clothes": "Textiles",
    "clothing": "Textiles",
    "cotton": "Textiles",

    # Agriculture
    "agriculture": "Agriculture",
    "agricultural": "Agriculture",
    "agro": "Agriculture",
    "farming": "Agriculture",
    "crop": "Agriculture",
    "crops": "Agriculture",
    "produce": "Agriculture",
    "vegetable": "Agriculture",
    "vegetables": "Agriculture",
    "vagitable": "Agriculture",
    "vagitables": "Agriculture",
    "vegitable": "Agriculture",
    "vegitables": "Agriculture",
    "veggie": "Agriculture",
    "veggies": "Agriculture",
    "fruit": "Agriculture",
    "fruits": "Agriculture",
    "grain": "Agriculture",
    "grains": "Agriculture",
    "spice": "Agriculture",
    "spices": "Agriculture",

    # Industrial
    "industrial": "Industrial",
    "industry": "Industrial",
    "industrials": "Industrial",
    "metal": "Industrial",
    "metals": "Industrial",
    "steel": "Industrial",
    "hardware": "Industrial",

    # Furniture
    "furniture": "Furniture",
    "furnitures": "Furniture",
    "furnishing": "Furniture",
    "furnishings": "Furniture",
    "seating": "Furniture",

    # Chemicals
    "chemical": "Chemicals",
    "chemicals": "Chemicals",

    # Construction
    "construction": "Construction",
    "building material": "Construction",
    "building materials": "Construction",

    # Stationery
    "stationery": "Stationery",
    "stationary": "Stationery",
    "office supply": "Stationery",
    "office supplies": "Stationery",

    # Learning / Education
    "learning": "Learning",
    "education": "Learning",

    # Beauty & Personal Care
    "beauty": "Beauty & Personal Care",
    "personal care": "Beauty & Personal Care",
    "beauty & personal care": "Beauty & Personal Care",
    "cosmetics": "Beauty & Personal Care",
    "cosmetic": "Beauty & Personal Care",
    "hair care": "Beauty & Personal Care",
    "hair spray": "Beauty & Personal Care",
    "hair sprey": "Beauty & Personal Care",
    "hairspray": "Beauty & Personal Care",
    "hair color": "Beauty & Personal Care",
    "hair colour": "Beauty & Personal Care",
    "hair dye": "Beauty & Personal Care",
    "skincare": "Beauty & Personal Care",
    "skin care": "Beauty & Personal Care",
    "toiletries": "Beauty & Personal Care",

    # Food & Beverages
    "food": "Food & Beverages",
    "beverage": "Food & Beverages",
    "beverages": "Food & Beverages",
    "food & beverages": "Food & Beverages",
    "food and beverages": "Food & Beverages",
    "corn": "Food & Beverages",
    "sweet corn": "Food & Beverages",
    "maize": "Food & Beverages",

    # Healthcare & Medical
    "medical": "Healthcare & Medical",
    "healthcare": "Healthcare & Medical",
    "healthcare & medical": "Healthcare & Medical",
    "pharma": "Healthcare & Medical",
    "pharmaceutical": "Healthcare & Medical",
    "pharmaceuticals": "Healthcare & Medical",
    "gloves": "Healthcare & Medical",
    "surgical": "Healthcare & Medical",

    # Automotive
    "automotive": "Automotive",
    "auto": "Automotive",
    "auto parts": "Automotive",
    "automobile": "Automotive",
    "vehicles": "Automotive",
    "spare parts": "Automotive",

    # Solar & Energy
    "solar": "Solar & Energy",
    "solar & energy": "Solar & Energy",
    "solar and energy": "Solar & Energy",
    "solar energy": "Solar & Energy",
    "renewable energy": "Solar & Energy",
    "renewable": "Solar & Energy",
    "energy": "Solar & Energy",
    "solar panel": "Solar & Energy",
    "solar panels": "Solar & Energy",
}

# Lookup map with lowercased canonical categories for direct case-insensitive matching
_CANONICAL_LOWER_MAP: dict[str, str] = {
    c.lower(): c for c in CANONICAL_CATEGORIES
}


def category_variants(canonical: str) -> set[str]:
    """Lower-cased spellings that normalize to ``canonical``.

    Used to match rows stored before normalization was applied on write, so a
    filter for "Electronics" also finds a legacy "electronic" row in SQL.
    """
    variants = {canonical.lower()}
    variants.update(alias for alias, target in CATEGORY_ALIASES.items() if target == canonical)
    return variants


def normalize_category(category: Optional[str]) -> str:
    """Normalize any category input to its canonical form.

    Examples:
        'electronic'  -> 'Electronics'
        'Electronic'  -> 'Electronics'
        'electronics' -> 'Electronics'
        'ELECTRONIC'  -> 'Electronics'
        'vagitables'  -> 'Agriculture'
        'packaging'   -> 'Packaging'
        '' / None     -> 'General Goods'
    """
    if not category or not str(category).strip():
        return "General Goods"

    cleaned = " ".join(str(category).strip().split())
    lowered = cleaned.lower()

    # 1. Direct canonical case-insensitive match
    if lowered in _CANONICAL_LOWER_MAP:
        return _CANONICAL_LOWER_MAP[lowered]

    # 2. Direct alias lookup
    if lowered in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[lowered]

    # 3. Singularized lookup
    singular = match_scoring.singularise(lowered)
    if singular in _CANONICAL_LOWER_MAP:
        return _CANONICAL_LOWER_MAP[singular]
    if singular in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[singular]

    # 4. Plural form (append 's' if not already ending in 's')
    plural = lowered + "s" if not lowered.endswith("s") else lowered
    if plural in _CANONICAL_LOWER_MAP:
        return _CANONICAL_LOWER_MAP[plural]
    if plural in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[plural]

    # 5. Check if any canonical category name is a whole word or substring
    for canon in CANONICAL_CATEGORIES:
        c_low = canon.lower()
        c_sing = match_scoring.singularise(c_low)
        if c_low in lowered or c_sing in lowered:
            return canon

    # 6. Fallback: clean title casing for custom user-defined category
    return cleaned.title()


def resolve_category_strict(category: Optional[str]) -> Optional[str]:
    """Canonical category for ``category`` when it genuinely names one, else None.

    Same lookups as ``normalize_category`` (canonical, alias, singular/plural, whole-word containment) but
    without its final fallback, so a made-up word ("Weapons", "asdf") is NOT turned into a category.
    Learned categories (admin-approved) are accepted as well.
    """
    if not category or not str(category).strip():
        return None
    cleaned = " ".join(str(category).strip().split())
    lowered = cleaned.lower()
    if lowered == GENERAL_CATEGORY.lower():
        return GENERAL_CATEGORY
    singular = match_scoring.singularise(lowered)
    plural = lowered + "s" if not lowered.endswith("s") else lowered
    for key in (lowered, singular, plural):
        if key in _CANONICAL_LOWER_MAP:
            return _CANONICAL_LOWER_MAP[key]
        if key in CATEGORY_ALIASES:
            return CATEGORY_ALIASES[key]
    # whole-word containment only ("Fresh Food Products" -> Food & Beverages), never a loose substring
    for canon in CANONICAL_CATEGORIES:
        c_low = canon.lower()
        if re.search(rf"\b{re.escape(c_low)}\b", lowered) or re.search(
            rf"\b{re.escape(match_scoring.singularise(c_low))}\b", lowered
        ):
            return canon
    try:
        from services import learned_vocabulary

        for name in learned_vocabulary.known_categories():
            if name.lower() == lowered:
                return name
    except Exception:  # noqa: BLE001 - vocabulary is optional here
        pass
    return None


class CategoryError(ValueError):
    """The category is not one the marketplace knows and could not be classified."""


_CLASSIFY_CACHE: dict[str, str] = {}
_CLASSIFY_MISS: dict[str, float] = {}
_MISS_TTL = 300.0
_CLASSIFY_MAX = 2000


def allowed_categories_text() -> str:
    return ", ".join(CANONICAL_CATEGORIES)


async def resolve_category(category: Optional[str], title: Optional[str] = None, description: Optional[str] = None) -> str:
    """Canonical category for a new/edited listing; raises ``CategoryError`` listing the allowed values.

    1. deterministic resolve of what the caller sent; 2. else one cached LLM classification of the
    title + the sent category into the canonical list; 3. else CategoryError. Existing rows are never
    re-validated (only create/edit call this).
    """
    strict = resolve_category_strict(category)
    if strict:
        return strict
    sent = " ".join(str(category or "").split())
    text = " | ".join(p for p in (sent, " ".join(str(title or "").split()), " ".join(str(description or "").split())[:200]) if p)
    key = text.lower()
    if not key:
        raise CategoryError(f"category is required; use one of: {allowed_categories_text()}")
    if key in _CLASSIFY_CACHE:
        return _CLASSIFY_CACHE[key]
    import time

    now = time.monotonic()
    if _CLASSIFY_MISS.get(key, 0) > now:
        raise CategoryError(_unknown_message(sent))

    from services import llm_client

    options = [*CANONICAL_CATEGORIES, GENERAL_CATEGORY]
    prompt = (
        "Classify a B2B marketplace listing into exactly one category.\n"
        f"Allowed categories: {json.dumps(options)}\n"
        'Use "General Goods" only for a legitimate product that fits none of the others. '
        'If the text is not a real tradable product (gibberish, a random word), answer "none".\n'
        f"Listing text (category the user typed | title | description): {json.dumps(text[:400], ensure_ascii=False)}\n"
        'Reply as JSON: {"category": "<one allowed value or none>"}'
    )
    schema = {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]}
    data = await llm_client.generate_json(prompt, schema, timeout=25.0)
    if data is None:
        # model unavailable: never cache; fall back to the title alone, then refuse honestly
        by_title = resolve_category_strict(title)
        if by_title:
            return by_title
        raise CategoryError(_unknown_message(sent, ai_down=True))
    answer = str(data.get("category", "") if isinstance(data, dict) else "").strip()
    chosen = next((c for c in options if c.lower() == answer.lower()), None)
    if chosen is None:
        if len(_CLASSIFY_MISS) > _CLASSIFY_MAX:
            _CLASSIFY_MISS.clear()
        _CLASSIFY_MISS[key] = now + _MISS_TTL
        raise CategoryError(_unknown_message(sent))
    if len(_CLASSIFY_CACHE) > _CLASSIFY_MAX:
        _CLASSIFY_CACHE.clear()
    _CLASSIFY_CACHE[key] = chosen
    return chosen


def _unknown_message(sent: str, ai_down: bool = False) -> str:
    base = f"unknown category '{sent[:60]}'" if sent else "category is required"
    tail = " (the classifier is unavailable)" if ai_down else ""
    return f"{base}{tail}; use one of: {allowed_categories_text()}"

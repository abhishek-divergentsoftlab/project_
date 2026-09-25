"""Central category taxonomy and normalization service.

Ensures that variant forms, singular/plural differences, and user typos
(e.g. 'electronic', 'Electronic', 'electronics', 'vagitables') resolve to
consistent canonical category names across the database, search indexes,
and counterparty matching.
"""

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
)

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
}

# Lookup map with lowercased canonical categories for direct case-insensitive matching
_CANONICAL_LOWER_MAP: dict[str, str] = {
    c.lower(): c for c in CANONICAL_CATEGORIES
}
_CANONICAL_LOWER_MAP["learning"] = "Learning"


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

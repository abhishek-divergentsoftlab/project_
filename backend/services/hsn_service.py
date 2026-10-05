"""Harmonized System of Nomenclature (HSN / HS) Code Service.

Provides international customs-grade hierarchical commodity matching:
- Governed by the World Customs Organization (WCO) across 200+ countries.
- Universal, language-independent classification covering 98% of world trade.

Hierarchy:
- 2 Digits (Chapter): Broad industry classification (01 to 99)
- 4 Digits (Heading): Commercial product family (e.g. 1005 = Maize, 3305 = Hair preparations)
- 6 Digits (Subheading): Precise tariff item (e.g. 3305.30 = Hair spray, 1005.90 = Sweet corn)
- 8 Digits (National Tariff): Country-specific tax/tariff rate

Mathematical Compatibility:
- 6+ digit match: 1.00 (identical tariff commodity)
- 4-digit match:  0.88 (direct commercial substitute under same heading)
- 2-digit match:  0.55 (same industrial trade chapter)
- Mismatch:       0.00 (cross-industry rejection)
"""

import json
import logging
import re
from collections import OrderedDict
from functools import lru_cache
from typing import Any, Optional

from core.config import settings

logger = logging.getLogger(__name__)

# Standard WCO 2-digit HS Chapters
HS_CHAPTERS: dict[str, str] = {
    "01": "Live animals",
    "02": "Meat and edible meat offal",
    "03": "Fish and crustaceans, molluscs and other aquatic invertebrates",
    "04": "Dairy produce; birds' eggs; natural honey; edible products of animal origin",
    "05": "Products of animal origin, not elsewhere specified or included",
    "06": "Live trees and other plants; bulbs, roots and the like; cut flowers",
    "07": "Edible vegetables and certain roots and tubers",
    "08": "Edible fruit and nuts; peel of citrus fruit or melons",
    "09": "Coffee, tea, mate and spices",
    "10": "Cereals (wheat, maize/corn, rice, barley, oats)",
    "11": "Products of the milling industry; malt; starches; inulin; wheat gluten",
    "12": "Oil seeds and oleaginous fruits; miscellaneous grains, seeds and fruit",
    "13": "Lac; gums, resins and other vegetable saps and extracts",
    "14": "Vegetable plaiting materials; vegetable products not elsewhere specified",
    "15": "Animal or vegetable fats and oils and their cleavage products",
    "16": "Preparations of meat, of fish or of crustaceans, molluscs",
    "17": "Sugars and sugar confectionery",
    "18": "Cocoa and cocoa preparations",
    "19": "Preparations of cereals, flour, starch or milk; pastrycooks' products",
    "20": "Preparations of vegetables, fruit, nuts or other parts of plants",
    "21": "Miscellaneous edible preparations",
    "22": "Beverages, spirits and vinegar",
    "23": "Residues and waste from the food industries; prepared animal fodder",
    "24": "Tobacco and manufactured tobacco substitutes",
    "25": "Salt; sulphur; earths and stone; plastering materials, lime and cement",
    "26": "Ores, slag and ash",
    "27": "Mineral fuels, mineral oils and products of their distillation",
    "28": "Inorganic chemicals; organic or inorganic compounds of precious metals",
    "29": "Organic chemicals (citric acid, amino acids, hydrocarbons)",
    "30": "Pharmaceutical products",
    "31": "Fertilisers",
    "32": "Tanning or dyeing extracts; tannins and their derivatives; dyes, pigments",
    "33": "Essential oils and resinoids; perfumery, cosmetic or toilet preparations",
    "34": "Soap, organic surface-active agents, washing preparations, lubricating",
    "35": "Albuminoidal substances; modified starches; glues; enzymes",
    "36": "Explosives; pyrotechnic products; matches; pyrophoric alloys",
    "37": "Photographic or cinematographic goods",
    "38": "Miscellaneous chemical products",
    "39": "Plastics and articles thereof (polymers, nitrile resins, PVC)",
    "40": "Rubber and articles thereof (latex, vulcanized rubber, gloves)",
    "41": "Raw hides and skins (other than furskins) and leather",
    "42": "Articles of leather; saddlery and harness; travel goods, handbags",
    "43": "Furskins and artificial fur; manufactures thereof",
    "44": "Wood and articles of wood; wood charcoal",
    "45": "Cork and articles of cork",
    "46": "Manufactures of straw, of esparto or of other plaiting materials",
    "47": "Pulp of wood or of other fibrous cellulosic material; waste paper",
    "48": "Paper and paperboard; articles of paper pulp, of paper or of paperboard",
    "49": "Printed books, newspapers, pictures and other products of printing",
    "50": "Silk",
    "51": "Wool, fine or coarse animal hair; horsehair yarn and woven fabric",
    "52": "Cotton",
    "53": "Other vegetable textile fibres; paper yarn and woven fabrics of paper yarn",
    "54": "Man-made filaments; strip and the like of man-made textile materials",
    "55": "Man-made staple fibres",
    "56": "Wadding, felt and nonwovens; special yarns; twine, cordage, ropes",
    "57": "Carpets and other textile floor coverings",
    "58": "Special woven fabrics; tufted textile fabrics; lace; tapestries",
    "59": "Impregnated, coated, covered or laminated textile fabrics",
    "60": "Knitted or crocheted fabrics",
    "61": "Articles of apparel and clothing accessories, knitted or crocheted",
    "62": "Articles of apparel and clothing accessories, not knitted or crocheted",
    "63": "Other made up textile articles; sets; worn clothing and worn textile articles",
    "64": "Footwear, gaiters and the like; parts of such articles",
    "65": "Headgear and parts thereof",
    "66": "Umbrellas, sun umbrellas, walking sticks, seat-sticks, whips",
    "67": "Prepared feathers and down and articles made of feathers or of down",
    "68": "Articles of stone, plaster, cement, asbestos, mica or similar materials",
    "69": "Ceramic products",
    "70": "Glass and glassware",
    "71": "Natural or cultured pearls, precious or semi-precious stones, precious metals",
    "72": "Iron and steel",
    "73": "Articles of iron or steel",
    "74": "Copper and articles thereof",
    "75": "Nickel and articles thereof",
    "76": "Aluminium and articles thereof",
    "78": "Lead and articles thereof",
    "79": "Zinc and articles thereof",
    "80": "Tin and articles thereof",
    "81": "Other base metals; cermets; articles thereof",
    "82": "Tools, implements, cutlery, spoons and forks, of base metal",
    "83": "Miscellaneous articles of base metal",
    "84": "Nuclear reactors, boilers, machinery and mechanical appliances; parts",
    "85": "Electrical machinery and equipment; sound recorders and reproducers; solar panels",
    "86": "Railway or tramway locomotives, rolling stock and parts thereof",
    "87": "Vehicles other than railway or tramway rolling stock, and parts (auto spares)",
    "88": "Aircraft, spacecraft, and parts thereof",
    "89": "Ships, boats and floating structures",
    "90": "Optical, photographic, cinematographic, measuring, medical or surgical",
    "91": "Clocks and watches and parts thereof",
    "92": "Musical instruments; parts and accessories of such articles",
    "93": "Arms and ammunition; parts and accessories thereof",
    "94": "Furniture; bedding, mattresses; luminaires and lighting fittings",
    "95": "Toys, games and sports requisites; parts and accessories thereof",
    "96": "Miscellaneous manufactured articles",
    "97": "Works of art, collectors' pieces and antiques",
}


def normalize_hsn(raw: Optional[Any]) -> Optional[str]:
    """Clean and normalize an HSN/HS code string into numeric digits."""
    if raw is None:
        return None
    s = str(raw).strip()
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return None
    # Pad single leading digit if chapter < 10 (e.g., "7" -> "07")
    if len(digits) == 1 or len(digits) == 3 or len(digits) == 5:
        digits = "0" + digits
    # Valid HSN codes are 2, 4, 6, or 8 digits
    if len(digits) < 2:
        return None
    return digits[:8]


def get_chapter(hsn: Optional[str]) -> Optional[str]:
    """Return the 2-digit WCO chapter for an HSN code."""
    norm = normalize_hsn(hsn)
    return norm[:2] if norm and len(norm) >= 2 else None


def get_chapter_description(hsn: Optional[str]) -> Optional[str]:
    """Return the official WCO description of the chapter."""
    ch = get_chapter(hsn)
    return HS_CHAPTERS.get(ch) if ch else None


@lru_cache(maxsize=10000)
def hsn_similarity(hsn_1: Optional[str], hsn_2: Optional[str]) -> Optional[float]:
    """Compute mathematical hierarchical compatibility between two HSN codes.

    Returns:
    - 1.00: 6+ digit match (Identical tariff commodity / form)
    - 0.88: 4-digit match (Direct commercial substitute under same heading)
    - 0.55: 2-digit match (Same industrial chapter)
    - 0.00: Different chapters (Different industries)
    - None: Either code is missing or unparseable
    """
    n1 = normalize_hsn(hsn_1)
    n2 = normalize_hsn(hsn_2)
    if not n1 or not n2:
        return None

    # Check 2-digit Chapter first
    if n1[:2] != n2[:2]:
        return 0.0

    # Minimum length for higher comparisons
    min_len = min(len(n1), len(n2))

    # 6+ digit match
    if min_len >= 6 and n1[:6] == n2[:6]:
        return 1.00

    # 4-digit Heading match
    if min_len >= 4 and n1[:4] == n2[:4]:
        return 0.88

    # 2-digit Chapter match
    return 0.55


# Common baseline fast mappings for instant zero-shot resolution
_COMMON_HEADING_KEYWORDS: dict[str, str] = {
    # Cereals & Grains
    "corn": "1005", "maize": "1005", "sweet corn": "100590", "baby corn": "070999",
    "rice": "1006", "wheat": "1001", "barley": "1003", "oats": "1004", "millet": "1008",
    "chickpea": "0713", "gram": "0713", "lentil": "0713", "chia seed": "1207",
    # Personal Care & Cosmetics
    "hair spray": "330530", "hair sprey": "330530", "hairspray": "330530", "hair lacquer": "330530",
    "hair color": "330590", "hair colour": "330590", "hair dye": "330590", "henna": "330590",
    "shampoo": "330510", "hair cleanser": "330510", "conditioner": "330590",
    # Auto Parts & Spares
    "brake pad": "870830", "brake pads": "870830", "brake disc": "870830", "brake rotor": "870830",
    "headlight": "851220", "led headlight": "851220", "spark plug": "851110",
    "engine oil": "271019", "motor oil": "271019",
    # Solar & Energy
    "solar panel": "854143", "solar panels": "854143", "pv module": "854143",
    "solar inverter": "850440", "inverter": "850440", "battery": "850760",
    # Medical & PPE
    "nitrile gloves": "392620", "surgical gloves": "401511", "examination gloves": "401519",
    "n95 mask": "630790", "surgical mask": "630790", "respirator": "902000",
    # Specialty Chemicals
    "citric acid": "291814", "potassium sorbate": "291619", "sodium benzoate": "291631",
    "caustic soda": "281511", "sodium hydroxide": "281511",
}


@lru_cache(maxsize=10000)
def predict_hsn_from_text(text: str) -> Optional[str]:
    """Deterministic HSN prediction from the keyword table only: no network, no LLM.

    Safe to call from match scoring. For open-world products use
    ``classify_hsn_with_ai`` at listing-save time and store the result.
    """
    if not text:
        return None
    cleaned = text.strip().lower()
    # Longer, more specific phrases first.
    for kw in sorted(_COMMON_HEADING_KEYWORDS.keys(), key=len, reverse=True):
        if re.search(rf"\b{re.escape(kw)}\b", cleaned):
            return _COMMON_HEADING_KEYWORDS[kw]
    return None


_AI_HSN_CACHE_SIZE = 2000
_ai_hsn_cache: "OrderedDict[str, Optional[str]]" = OrderedDict()


def valid_ai_hsn(raw: Any) -> Optional[str]:
    """A model-proposed HSN is kept only if it is 4+ digits and its chapter exists."""
    hsn = normalize_hsn(raw)
    if not hsn or len(hsn) < 4 or hsn[:2] not in HS_CHAPTERS:
        return None
    return hsn


async def classify_hsn_with_ai(text: str) -> Optional[str]:
    """LLM HSN classification via local Ollama. Never raises; None when unsure or disabled.

    Results (including misses) are cached in an LRU. Not for use in scoring.
    """
    if not text or not text.strip() or not getattr(settings, "HSN_AI_ENABLED", False):
        return None
    key = text.strip().lower()[:200]
    if key in _ai_hsn_cache:
        _ai_hsn_cache.move_to_end(key)
        return _ai_hsn_cache[key]
    result: Optional[str] = None
    try:
        import httpx

        model = (
            getattr(settings, "HSN_AI_MODEL", "")
            or getattr(settings, "AI_ROUTER_MODEL", "")
            or "qwen2.5:1.5b"
        )
        prompt = (
            "You are a World Customs Organization trade classifier. "
            "What is the standard 4-digit or 6-digit Harmonized System (HS / HSN) code "
            f"for this product: '{key}'? "
            'Respond with ONLY a JSON object: {"hsn_code": "<digits_only>"}'
        )
        timeout = float(getattr(settings, "HSN_AI_TIMEOUT_SECONDS", 3.0))
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate",
                json={
                    "model": model, "prompt": prompt, "stream": False,
                    "format": "json", "options": {"temperature": 0.0},
                },
            )
            resp.raise_for_status()
            raw_res = json.loads(resp.json().get("response", "{}"))
        result = valid_ai_hsn(raw_res.get("hsn_code"))
    except Exception as exc:
        logger.debug("AI HSN classification skipped: %s", exc)
        return None  # transient failure: do not cache
    _ai_hsn_cache[key] = result
    if len(_ai_hsn_cache) > _AI_HSN_CACHE_SIZE:
        _ai_hsn_cache.popitem(last=False)
    return result


def stored_hsn(product_details: Optional[dict]) -> Optional[str]:
    """The HSN already stored on a listing (``hsn_code`` or ``hsn``), if valid."""
    pd = product_details or {}
    for key in ("hsn_code", "hsn"):
        hsn = normalize_hsn(pd.get(key)) if pd.get(key) else None
        if hsn and len(hsn) >= 4:
            return hsn
    return None


def resolve_hsn(product_details: Optional[dict], text: str) -> Optional[str]:
    """Stored HSN first, else the deterministic keyword prediction. Never the LLM."""
    return stored_hsn(product_details) or predict_hsn_from_text(text)

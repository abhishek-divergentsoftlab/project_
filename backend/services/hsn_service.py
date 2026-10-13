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


# Common baseline fast mappings for instant zero-shot resolution across commercial trade categories
_COMMON_HEADING_KEYWORDS: dict[str, str] = {
    # Eyewear, Optics & Protective Instruments (Chapter 90 & 70)
    "sunglasses": "90041000", "sun glasses": "90041000", "sunglass": "90041000", "sun glass": "90041000",
    "spectacles": "900490", "goggles": "900490", "reading glasses": "900490", "safety glasses": "900490",
    "spectacle frames": "900311", "contact lenses": "900130", "contact lens": "900130",
    "glass for sun glasses": "70159020",

    # Beverages, Soft Drinks & Mineral Water (Chapter 22 & 20)
    "soft drink": "2202", "soft drinks": "2202", "softdrink": "2202",
    "cold drink": "2202", "cold drinks": "2202", "aerated drink": "2202", "aerated drinks": "2202",
    "coca cola": "220210", "cococola": "220210", "coco cola": "220210", "coke": "220210",
    "pepsi": "220210", "thums up": "220210", "sprite": "220210", "7up": "220210",
    "fanta": "220210", "mirinda": "220210", "mountain dew": "220210", "limca": "220210",
    "appy fizz": "220299", "appy": "200971", "red bull": "220299", "sting": "220299", "energy drink": "220299",
    "fruit juice": "2009", "mango juice": "200989", "apple juice": "200971", "orange juice": "200912",
    "mineral water": "220110", "drinking water": "220190", "packaged water": "220190", "soda water": "220110",

    # Footwear, Shoes & Apparel (Chapter 64, 61, 62)
    "sneakers": "640411", "sneaker": "640411", "sports shoes": "640411", "running shoes": "640411",
    "tennis shoes": "640411", "gym shoes": "640411", "leather shoes": "640359", "formal shoes": "640359",
    "sandals": "640220", "slippers": "640299", "chappal": "640299", "boots": "640391",
    "t-shirt": "610910", "t shirt": "610910", "tshirt": "610910", "polo shirt": "610510",
    "shirt": "620520", "shirts": "620520", "jeans": "620342", "denim": "620342",
    "trousers": "620342", "pants": "620342", "hoodie": "611020", "jacket": "620130",
    "saree": "520859", "kurti": "620442", "tracksuit": "611212", "socks": "611595",

    # Bags, Luggage & Travel Goods (Chapter 42)
    "laptop bag": "420212", "laptop case": "420212", "laptop sleeve": "420212",
    "backpack": "420292", "backpacks": "420292", "school bag": "420292", "travel bag": "420292",
    "suitcase": "420212", "luggage": "420212", "trolley bag": "420212", "handbag": "420222", "wallet": "420231",

    # Consumer Electronics, Computers & Telecom (Chapter 84 & 85)
    "smartphone": "851713", "smart phone": "851713", "mobile phone": "851713", "cell phone": "851713",
    "iphone": "851713", "samsung galaxy": "851713", "android phone": "851713",
    "laptop": "847130", "notebook computer": "847130", "macbook": "847130",
    "tablet": "847130", "ipad": "847130", "desktop computer": "847141",
    "smartwatch": "851762", "smart watch": "851762", "apple watch": "851762",
    "headphones": "851830", "headphone": "851830", "earphones": "851830", "earphone": "851830",
    "earbuds": "851830", "airpods": "851830", "bluetooth speaker": "851821", "soundbar": "851822",
    "television": "852872", "smart tv": "852872", "tv": "852872", "monitor": "852852",
    "mobile charger": "850440", "charger": "850440", "power adapter": "850440",
    "power bank": "850760", "powerbank": "850760", "lithium battery": "850760",
    "usb cable": "854442", "cctv camera": "852589", "pendrive": "852351", "hard drive": "847170",

    # Home & Kitchen Electrical Appliances (Chapter 84 & 85)
    "air conditioner": "841510", "ac": "841510", "split ac": "841510",
    "refrigerator": "841821", "fridge": "841821", "freezer": "841830",
    "washing machine": "845011", "microwave": "851650", "microwave oven": "851650",
    "ceiling fan": "841451", "table fan": "841451", "fan": "841451",
    "water heater": "851610", "geyser": "851610", "mixer grinder": "850940",
    "water purifier": "842121", "vacuum cleaner": "850811", "electric kettle": "851679",

    # Dairy, Grains & Food Staples (Chapter 04, 07, 10, 11, 17, 09, 15)
    "milk": "0401", "cow milk": "040120", "buffalo milk": "040120", "butter": "040510",
    "ghee": "040590", "curd": "040320", "dahi": "040320", "yogurt": "040320",
    "cheese": "040690", "paneer": "040610", "milk powder": "040210",
    "corn": "1005", "maize": "1005", "sweet corn": "100590", "baby corn": "070999",
    "rice": "1006", "basmati rice": "10063020", "wheat": "1001", "atta": "110100",
    "maida": "110100", "suji": "110311", "besan": "110610", "barley": "1003", "oats": "1004", "millet": "1008",
    "chickpea": "071320", "chana": "071320", "kabuli chana": "07132010", "lentil": "071340",
    "moong dal": "071331", "urad dal": "071331", "toor dal": "071360", "rajma": "071333",
    "sugar": "170199", "tea": "090240", "coffee": "090121",
    "black pepper": "090411", "cardamom": "090831", "cumin": "090931", "jeera": "090931",
    "turmeric": "091030", "haldi": "091030", "red chilli": "090422",
    "mustard oil": "151491", "sunflower oil": "151219", "soybean oil": "150790", "palm oil": "151190",

    # Packaged Foods, Confectionery & FMCG (Chapter 19, 21, 25)
    "biscuit": "190531", "biscuits": "190531", "cookies": "190531", "parle g": "190531",
    "noodles": "190230", "maggi": "190230", "pasta": "190219", "chocolate": "180632",
    "salt": "250100", "tata salt": "250100", "tomato ketchup": "210320",

    # Personal Care, Cosmetics & Hygiene (Chapter 33, 34, 38, 96)
    "soap": "340111", "bathing soap": "340111", "body wash": "340130", "hand wash": "340130",
    "shampoo": "330510", "hair cleanser": "330510", "conditioner": "330590",
    "hair spray": "330530", "hair sprey": "330530", "hairspray": "330530", "hair lacquer": "330530",
    "hair color": "330590", "hair colour": "330590", "hair dye": "330590", "hair oil": "330590",
    "toothpaste": "330610", "toothbrush": "960321", "perfume": "330300", "deodorant": "330720",
    "sunscreen": "330499", "face wash": "330499", "lipstick": "330410",
    "sanitary pad": "961900", "sanitary pads": "961900", "sanitary napkin": "961900",
    "diaper": "961900", "diapers": "961900", "baby diaper": "961900",
    "hand sanitizer": "380894", "antiseptic": "380894",

    # Stationery & Paper (Chapter 48 & 96)
    "pen": "960810", "ball pen": "960810", "gel pen": "960810", "pencil": "960910",
    "notebook": "482020", "a4 paper": "480256", "copy paper": "480256",

    # Building Materials, Steel & Hardware (Chapter 25, 72, 73, 69, 32)
    "cement": "252329", "portland cement": "252329", "tmt bar": "72142090", "tmt bars": "72142090",
    "sariya": "72142090", "steel pipe": "730630", "steel sheets": "721049",
    "tiles": "690721", "ceramic tiles": "690721", "paint": "320910", "emulsion": "320910",
    "plywood": "441233", "float glass": "700529",

    # Automotive & Tyres (Chapter 40, 85, 87)
    "car tyre": "401110", "car tire": "401110", "tyre": "401110", "tire": "401110",
    "truck tyre": "401120", "bike tyre": "401140", "car battery": "850710",
    "helmet": "650610", "brake pad": "870830", "brake pads": "870830", "spark plug": "851110",
    "engine oil": "271019", "motor oil": "271019", "headlight": "851220",

    # Solar & Renewable Energy
    "solar panel": "854143", "solar panels": "854143", "pv module": "854143",
    "solar inverter": "850440", "inverter": "850440", "battery": "850760",

    # Medical & PPE
    "nitrile gloves": "392620", "surgical gloves": "401511", "examination gloves": "401519",
    "n95 mask": "630790", "surgical mask": "630790", "respirator": "902000",
    "thermometer": "902519", "bp monitor": "901890", "syringe": "901831",

    # Chemicals & Industrial
    "citric acid": "291814", "potassium sorbate": "291619", "sodium benzoate": "291631",
    "caustic soda": "281511", "sodium hydroxide": "281511", "urea": "310210",
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

        model = getattr(settings, "HSN_AI_MODEL", "") or settings.AI_CHAT_MODEL
        prompt = (
            "You are an expert Indian GST customs classifier. Give the most specific Indian GST HSN code "
            "(6 or 8 digits, never a 2-digit chapter) for the commercial product below. "
            "First think of the generic product type (e.g. 'maaza' is a mango fruit drink), then classify it. "
            "Examples: samosa -> 210690; maaza -> 220299; pepsi -> 220210; bread/pav -> 190590; "
            "rusk -> 190540; kurkure -> 210690; paneer -> 040690; sunglasses -> 900410; smartphone -> 851713. "
            f"Product: '{key}'. "
            'Respond with ONLY a JSON object: {"generic_name": "<generic product type>", "hsn_code": "<digits_only>"}'
        )
        timeout = float(getattr(settings, "HSN_AI_TIMEOUT_SECONDS", 8.0))
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


# ---------------------------------------------------------------------------
# HSN search post-processing: collapse tariff levels, disambiguate look-alike rows
# ---------------------------------------------------------------------------

# Short words that tell a user which "kind" of a product a tariff line is (checked in this order of
# preference when the query matches several look-alike rows and we need to pick the recommended one).
_QUALIFIER_WORDS = (
    "fresh", "raw", "chilled", "frozen", "dried", "dehydrated", "preserved", "prepared", "processed",
    "cooked", "roasted", "smoked", "salted", "canned", "juice", "jam", "jams", "seeds", "oil", "powder",
    "waste", "yarn", "fabric", "fabrics", "sweetened", "unsweetened", "refined", "crude", "wild", "cultivated",
)
_QUALIFIER_PREFERENCE = ("fresh", "raw", "chilled", "frozen")
_QUALIFIER_RE = re.compile(r"\b(" + "|".join(_QUALIFIER_WORDS) + r")\b", re.IGNORECASE)
_CONTEXT_MAX_LEN = 60
_MEANINGLESS_DESCRIPTIONS = {"", "other", "others"}


def _norm_text(text: str) -> str:
    """Lower-case, strip punctuation, crude singular form: 'APPLES.' == 'Apple'."""
    words = re.sub(r"[^\w\s]", " ", (text or "").lower()).split()
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words)


def _qualifier(description: str) -> str:
    """The short qualifier (fresh / dried / prepared or preserved ...) an ancestor description carries."""
    # Ignore the "other than that of headings ..." tail: it names what the heading excludes.
    head = re.split(r"\bother than\b|\bexcluding\b|;", description or "", maxsplit=1, flags=re.IGNORECASE)[0]
    m = _QUALIFIER_RE.search(head)
    if not m:
        return ""
    word = m.group(1).lower()
    tail = re.match(r"\s+or\s+(" + "|".join(_QUALIFIER_WORDS) + r")\b", head[m.end():], re.IGNORECASE)
    if tail:
        word = f"{word} or {tail.group(1).lower()}"
    return {"jams": "jam"}.get(word, word)


def _short(description: str) -> str:
    text = (description or "").strip().rstrip(":. ")
    if text.isupper():
        text = text.capitalize()
    return text if len(text) <= _CONTEXT_MAX_LEN else text[: _CONTEXT_MAX_LEN - 1].rstrip(" ,;") + "…"


def _collapse_levels(rows: list[dict]) -> list[dict]:
    """Merge a shorter code into its most specific descendant when both read the same."""
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(_norm_text(r["formatted_title"]), []).append(r)
    merged: dict[str, str] = {}  # dropped code -> kept code
    for group in by_key.values():
        for p in group:
            desc = [c for c in group if c is not p and c["code"].startswith(p["code"])]
            if not desc:
                continue
            deepest = [c for c in desc if not any(o is not c and o["code"].startswith(c["code"]) for o in desc)]
            if len(deepest) == 1:
                merged[p["code"]] = deepest[0]["code"]
    out: list[dict] = []
    for r in rows:
        if r["code"] in merged:
            continue
        r = dict(r)
        r["also_codes"] = sorted((d for d, k in merged.items() if k == r["code"]), key=lambda c: (len(c), c))
        out.append(r)
    return out


def _assign_context(rows: list[dict], descriptions: dict[str, str]) -> None:
    """Set ``context`` / ``parent_description`` on rows whose title collides with another row's."""
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(_norm_text(r["formatted_title"]), []).append(r)
    for group in by_key.values():
        if len(group) < 2:
            continue
        levels: list[tuple[str, ...]] = []
        for size in (4, 2):
            levels.append(tuple(r["code"][:size] if len(r["code"]) > size else "" for r in group))
        levels.append(tuple(r["code"][:6] if len(r["code"]) > 6 else "" for r in group))
        for ancestors in levels:
            texts = [descriptions.get(a, "") if a else "" for a in ancestors]
            if len({_norm_text(t) for t in texts}) < 2:
                continue
            if all(_norm_text(t) in _MEANINGLESS_DESCRIPTIONS for t in texts):
                continue
            quals = [_qualifier(t) for t in texts]
            use_quals = all(quals) and len(set(quals)) == len(quals)
            for r, text, qual in zip(group, texts, quals):
                if not text or _norm_text(text) in _MEANINGLESS_DESCRIPTIONS:
                    continue
                r["context"] = qual if use_quals else _short(text)
                r["parent_description"] = _short(text)
            break


def enrich_search_results(
    rows: list[dict],
    descriptions: dict[str, str],
    query: str = "",
    suggested_code: Optional[str] = None,
) -> list[dict]:
    """Make HSN search rows pickable.

    ``rows`` are dicts with at least code / formatted_title / level (in search order);
    ``descriptions`` maps ancestor codes (2/4/6 digits) to their description.
    Adds ``display_title``, ``context``, ``parent_description``, ``also_codes`` and ``recommended``,
    collapses 6-digit/8-digit duplicates, and orders: recommended first, then search order with
    broader (ancestor) rows after their more specific children.
    """
    rows = _collapse_levels(rows)
    _assign_context(rows, descriptions)

    # Titles that still collide (same ancestors, e.g. several "Other" lines) get the code appended.
    seen: dict[str, list[dict]] = {}
    for r in rows:
        title = r["formatted_title"]
        if r.get("context"):
            title = f"{title} — {r['context']}"
        r["display_title"] = title
        seen.setdefault(_norm_text(title), []).append(r)
    for group in seen.values():
        if len(group) > 1:
            for r in group:
                r["display_title"] = f"{r['display_title']} ({r['code']})"

    # Recommended row: the /hsn/suggest answer when it is in the results, else (only when the query names
    # look-alike rows) the "fresh/raw" one among the rows titled exactly like the query.
    recommended: Optional[dict] = None
    if suggested_code:
        recommended = next((r for r in rows if r["code"] == suggested_code), None) or next(
            (r for r in rows if r["code"].startswith(suggested_code)), None
        )
    if recommended is None:
        q = _norm_text(query)
        exact = [r for r in rows if r.get("context") and _norm_text(r["formatted_title"]) == q]
        if exact:
            def rank(r: dict) -> int:
                first = (r["context"] or "").split(" ")[0]
                return _QUALIFIER_PREFERENCE.index(first) if first in _QUALIFIER_PREFERENCE else len(_QUALIFIER_PREFERENCE)
            recommended = min(exact, key=rank)
    for r in rows:
        r["recommended"] = r is recommended

    # A broader row (e.g. the heading) goes after its specific children when those children were
    # disambiguated or collapsed; results without look-alikes keep their search order.
    idx = {r["code"]: i for i, r in enumerate(rows)}
    def sort_key(r: dict) -> tuple:
        deeper = [
            idx[o["code"]] for o in rows
            if o is not r and o["code"].startswith(r["code"]) and (o.get("context") or o.get("also_codes"))
        ]
        pos = (max(deeper) + 0.5) if deeper else idx[r["code"]]
        return (not r["recommended"], pos, -len(r["code"]))
    return sorted(rows, key=sort_key)

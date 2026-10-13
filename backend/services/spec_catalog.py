"""The spec catalogue: a fixed set of spec names per product family, with known values, used by everyone.

The chat, the RFQ form and matching all read and write specs through this module, so "Apple Variety", "variety"
and "product type" are the same spec (``variety``) and "20000 g", "20kg" and "20 kg" are the same value.

* A *family* is the head noun of a product ("fuji apples" -> ``apple``, "basmati rice" -> ``rice``). Variety words are
  VALUES of a family's spec, never families of their own.
* A family's attributes are generated once by the attribute recommender (an LLM call of ~12 s), converted to the
  catalogue shape below, merged with the COMMON attributes (packaging, pack size, certifications, grade, origin,
  condition) and stored in ``spec_catalog``; later lookups never call the model.
* ``Family.normalize`` turns any (key, value) a person, form or model wrote into the canonical (key, value):
  choices map to their known options (unknown values are kept: the catalogue is flexible), multi-choices become lists,
  numbers become the FLAT string ``"<value> <unit>"`` in the attribute's unit. Unknown keys stay as free specs.

Attribute shape (stored as JSONB)::

    {key, label, type: choice|multi_choice|number|text, options: [str], unit: str|None,
     unit_family: mass|length|volume|percent|other|None, synonyms: [str], option_synonyms: {option: [str]},
     must_match_default: bool, explanation: str, placeholder: str}

Not done: learning new options from values seen three times (an unknown value is kept as typed, never added to the
family's options).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from services import chat_specs, unit_converter

logger = logging.getLogger(__name__)

MUST_SUFFIX = chat_specs.MUST_MATCH_SUFFIX
PASSTHROUGH_KEYS = frozenset({"name", "hsn_code", "hsn_source"})
FUZZY = 0.85
MAX_OPTIONS = 14

CHOICE, MULTI, NUMBER, TEXT = "choice", "multi_choice", "number", "text"


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _norm(text: Any) -> str:
    """Lowercase words separated by single spaces: "Pack_Weight (kg)" -> "pack weight kg"."""
    t = str(text).lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9°%.ऀ-ॿ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip(" .")


def _snake(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def _singular(word: str) -> str:
    w = word.lower()
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if w.endswith("oes") and len(w) > 4:
        return w[:-2]
    if w.endswith(("ss", "us", "is")):
        return w
    if w.endswith("s"):
        return w[:-1]
    return w


def _singular_phrase(text: str) -> str:
    return " ".join(_singular(w) for w in text.split())


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _label_core(label: str) -> str:
    return re.sub(r"\([^)]*\)", " ", label or "").strip()


_PAREN_RE = re.compile(r"\(([^)]*)\)")
_UNIT_TOKEN_RE = re.compile(r"^[°%]?\s*[A-Za-z%°]{1,6}$")


def _paren_unit(label: str) -> Optional[str]:
    m = _PAREN_RE.search(label or "")
    if m and _UNIT_TOKEN_RE.match(m.group(1).strip()):
        return m.group(1).strip()
    return None


def display_label(label: str) -> str:
    """"Pack size (kg)" -> "Pack size": the label without its unit."""
    return re.sub(r"\s+", " ", _label_core(label)).strip() or label


def _title(value: str) -> str:
    return " ".join(w.capitalize() if w.islower() else w for w in value.split())


_ACRONYMS = {
    "fssai": "FSSAI", "haccp": "HACCP", "iso": "ISO", "gmp": "GMP", "brc": "BRC", "usda": "USDA", "apeda": "APEDA",
    "fda": "FDA", "eu": "EU", "npop": "NPOP", "bis": "BIS", "isi": "ISI", "fsc": "FSC", "sqf": "SQF", "ifs": "IFS",
    "bsci": "BSCI", "sedex": "SEDEX", "ce": "CE", "ukca": "UKCA", "agmark": "AGMARK", "kosher": "Kosher",
    "halal": "Halal", "organic": "Organic", "globalgap": "GlobalG.A.P.", "gap": "GAP",
}


def _title_item(item: str) -> str:
    return " ".join(_ACRONYMS.get(w.lower(), w.capitalize() if w.islower() else w) for w in item.split())


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
_UNIT_ALIAS = {
    "%": "%", "percent": "%", "pct": "%", "per cent": "%", "percentage": "%",
    "bx": "°Bx", "°bx": "°Bx", "brix": "°Bx", "°brix": "°Bx", "deg brix": "°Bx", "degree brix": "°Bx", "degrees brix": "°Bx",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg", "kgm": "kg",
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g", "gr": "g", "mg": "mg",
    "tonne": "tonne", "tonnes": "tonne", "ton": "tonne", "tons": "tonne", "mt": "tonne", "quintal": "quintal",
    "lb": "lb", "lbs": "lb", "l": "l", "ltr": "l", "litre": "l", "litres": "l", "liter": "l", "liters": "l", "ml": "ml",
    "mm": "mm", "cm": "cm", "m": "m", "mtr": "m", "meter": "m", "metre": "m", "meters": "m", "metres": "m",
    "pcs": "pcs", "pc": "pcs", "piece": "pcs", "pieces": "pcs", "nos": "pcs",
}
_SUFFIX_UNITS = frozenset({
    "kg", "g", "gm", "mg", "mm", "cm", "m", "l", "ml", "pct", "percent", "mt", "tonne", "lb", "lbs", "pcs", "days", "day",
})


def canonical_unit(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    u = _norm(unit)
    if u in _UNIT_ALIAS:
        return _UNIT_ALIAS[u]
    cleaned = unit_converter.clean_unit(unit)
    if cleaned and cleaned in _UNIT_ALIAS:
        return _UNIT_ALIAS[cleaned]
    typed = unit.strip()
    return typed if (cleaned or "").lower() == typed.lower() else (cleaned or typed)  # keep "kPa", not "kpa"


def unit_family_of(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    if unit == "%":
        return "percent"
    if unit_converter.is_mass_unit(unit):
        return "mass"
    if unit_converter.is_length_unit(unit):
        return "length"
    if unit_converter.is_volume_unit(unit):
        return "volume"
    return "other"


def _convert(value: Decimal, from_unit: str, to_unit: str) -> Optional[Decimal]:
    if from_unit == to_unit:
        return value
    try:
        return unit_converter.convert(value, from_unit, to_unit)
    except Exception:
        return None


def _fmt(value: Decimal) -> str:
    try:
        q = value.quantize(Decimal("0.0001")).normalize()
    except InvalidOperation:
        q = value.normalize()
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


# ---------------------------------------------------------------------------
# Numbers: "<value> <unit>", "12-14 °Bx", "≥14 °Bx"
# ---------------------------------------------------------------------------
_MIN_WORDS = r"≥|>=|>|min(?:imum)?\.?|at\s*least|not\s+(?:less|lower)\s+than|above|over|more\s+than|greater\s+than"
_MAX_WORDS = r"≤|<=|<|max(?:imum)?\.?|at\s*most|not\s+(?:more|higher)\s+than|up\s*to|upto|below|under|less\s+than"
_NUM_RE = re.compile(
    rf"^\s*(?:(?P<min>{_MIN_WORDS})|(?P<max>{_MAX_WORDS}))?\s*(?P<n1>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:(?:-|–|to)\s*(?P<n2>\d[\d,]*(?:\.\d+)?))?\s*(?P<u>[%°a-zA-Z/.\s]*?)\s*$",
    re.IGNORECASE,
)
_UNIT_NOISE_RE = re.compile(r"\b(?:each|per\s+(?:box|bag|carton|pack|packet|piece|unit|crate|sack)|/\s*(?:box|bag|carton|pack)|net|approx(?:imately)?|about|around|only)\b", re.IGNORECASE)


_MEASURE_IN_TEXT_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:" + "|".join(sorted((re.escape(u) for u in _UNIT_ALIAS if u.isalpha() or u in ("%",)), key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


def _dec(text: str) -> Optional[Decimal]:
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def parse_number(value: Any) -> Optional[dict[str, Any]]:
    """{"lo": Decimal, "hi": Decimal|None, "q": ""|"≥"|"≤"|"-", "unit": str|None} or None when ``value`` is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        n = _dec(str(value))
        return None if n is None else {"lo": n, "hi": None, "q": "", "unit": None}
    text = _UNIT_NOISE_RE.sub(" ", str(value))
    m = _NUM_RE.match(text)
    if not m:
        return None
    unit_raw = (m.group("u") or "").strip(" ./")
    if unit_raw and not _UNIT_TOKEN_RE.match(unit_raw) and len(unit_raw.split()) > 2:
        return None
    n1, n2 = _dec(m.group("n1")), _dec(m.group("n2") or "")
    if n1 is None:
        return None
    q = "≥" if m.group("min") else "≤" if m.group("max") else ""
    if n2 is not None:
        lo, hi = (n1, n2) if n1 <= n2 else (n2, n1)
        return {"lo": lo, "hi": hi, "q": "-", "unit": canonical_unit(unit_raw)}
    return {"lo": n1, "hi": None, "q": q, "unit": canonical_unit(unit_raw)}


def format_number(parsed: dict[str, Any], unit: Optional[str]) -> str:
    lo, hi, q = parsed["lo"], parsed["hi"], parsed["q"]
    body = f"{_fmt(lo)}-{_fmt(hi)}" if q == "-" and hi is not None else f"{q if q in ('≥', '≤') else ''}{_fmt(lo)}"
    return f"{body} {unit}".strip() if unit else body


def normalize_number(value: Any, unit: Optional[str], unit_hint: Optional[str] = None) -> Optional[str]:
    """``value`` as the flat string in the attribute's ``unit`` (None when it is not a number).

    "20000 g" -> "20 kg" (unit kg); "20" -> "20 kg"; "min 14 brix" -> "≥14 °Bx"; "12-14" -> "12-14 °Bx". A unit that cannot be
    converted to the attribute's (20 mm for kg) is kept as written.
    """
    parsed = parse_number(value)
    if parsed is None:
        return None
    given = parsed["unit"] or canonical_unit(unit_hint)
    target = canonical_unit(unit)
    if given and target and given.lower() == target.lower():
        given = target
    if given and target and given != target:
        lo = _convert(parsed["lo"], given, target)
        hi = _convert(parsed["hi"], given, target) if parsed["hi"] is not None else None
        if lo is not None and (parsed["hi"] is None or hi is not None):
            return format_number({**parsed, "lo": lo, "hi": hi}, target)
        return format_number(parsed, given)
    return format_number(parsed, given or target)


def _interval(parsed: dict[str, Any]) -> tuple[Optional[Decimal], Optional[Decimal]]:
    q = parsed["q"]
    if q == "≥":
        return parsed["lo"], None
    if q == "≤":
        return None, parsed["lo"]
    if q == "-" and parsed["hi"] is not None:
        return parsed["lo"], parsed["hi"]
    return parsed["lo"], parsed["lo"]


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------
def make_attr(
    key: str, label: str, type_: str, *, options: Iterable[str] = (), unit: Optional[str] = None,
    synonyms: Iterable[str] = (), option_synonyms: Optional[dict[str, list[str]]] = None, must: bool = False,
    explanation: str = "", placeholder: str = "",
) -> dict[str, Any]:
    unit = canonical_unit(unit) if unit else None
    return {
        "key": key, "label": label, "type": type_, "options": [str(o) for o in options], "unit": unit,
        "unit_family": unit_family_of(unit), "synonyms": [str(s) for s in synonyms],
        "option_synonyms": {k: list(v) for k, v in (option_synonyms or {}).items()},
        "must_match_default": bool(must), "explanation": explanation or "", "placeholder": placeholder or "",
    }


COMMON_ATTRIBUTES: list[dict[str, Any]] = [
    make_attr(
        "packaging_type", "Packaging", CHOICE,
        options=["Box", "Carton", "Crate", "Bag", "Sack", "Drum", "Pallet", "Loose / Bulk"],
        synonyms=["packaging", "packaging type", "packing", "packing type", "pack type", "package", "package type", "pack"],
        option_synonyms={
            "Box": ["boxes", "peti", "dabba", "cardboard box"],
            "Carton": ["cartons", "carton box", "master carton"],
            "Crate": ["crates", "plastic crate", "wooden crate"],
            "Bag": ["bags", "pouch", "pouches", "thaila", "pp bag", "jute bag", "gunny bag"],
            "Sack": ["sacks", "bori", "bora", "katta", "sack bag"],
            "Drum": ["drums", "barrel", "barrels"],
            "Pallet": ["pallets", "palletised", "palletized"],
            "Loose / Bulk": ["loose", "bulk", "khula", "unpacked", "bulk packing"],
        },
        explanation="How the goods are packed for dispatch.", placeholder="e.g. Carton",
    ),
    make_attr(
        "pack_size", "Pack size (kg)", NUMBER, unit="kg", options=["10", "20", "25", "50"],
        synonyms=[
            "pack size", "pack weight", "packaging size", "packaging weight", "package size", "package weight", "packing size",
            "packing weight", "box weight", "box size", "carton size", "carton weight", "bag size", "bag weight",
            "weight per box", "weight per bag", "weight per carton", "weight per pack", "weight per unit", "per bag", "per box",
            "per carton", "net weight per pack", "unit weight",
        ],
        explanation="Weight of one pack (box, bag or carton).", placeholder="e.g. 20",
    ),
    make_attr(
        "certifications", "Certifications", MULTI,
        options=["Organic", "FSSAI", "ISO 9001", "ISO 22000", "HACCP", "GlobalG.A.P.", "BIS", "Halal", "Kosher", "FDA"],
        synonyms=["certification", "certificate", "certificates", "certified", "certified by", "standards"],
        option_synonyms={
            "Organic": ["jaivik", "organically grown", "organic certified"],
            "GlobalG.A.P.": ["globalgap", "global gap", "globalgp"],
            "ISO 9001": ["iso9001", "iso-9001"],
            "ISO 22000": ["iso22000", "iso-22000"],
            "FSSAI": ["fssai licence", "fssai license"],
        },
        explanation="Quality and safety certifications the goods carry.", placeholder="e.g. Organic, FSSAI",
    ),
    make_attr(
        "grade", "Grade", CHOICE, options=["A", "B", "C", "Premium", "Export Quality"],
        synonyms=["quality", "grading", "class", "quality grade", "grade quality"],
        option_synonyms={
            "A": ["grade a", "a grade", "class a", "a1", "a 1"], "B": ["grade b", "b grade", "class b"],
            "C": ["grade c", "c grade", "class c"],
            "Premium": ["premium quality", "top quality", "best quality", "prime", "premium grade"],
            "Export Quality": ["export", "export grade", "export standard", "export quality grade"],
        },
        explanation="Quality grade of the goods.", placeholder="e.g. A",
    ),
    make_attr(
        "origin", "Origin", TEXT,
        synonyms=["country of origin", "place of origin", "origin country", "grown in", "made in", "source country", "sourced from"],
        explanation="Where the goods come from.", placeholder="e.g. India",
    ),
    make_attr(
        "condition", "Condition", CHOICE, options=["Fresh", "Dried", "Frozen", "Chilled", "Processed"],
        synonyms=["condition", "freshness", "fresh or dried", "storage condition"],
        option_synonyms={
            "Fresh": ["taza", "tazaa", "fresh produce"], "Dried": ["dry", "dehydrated", "sukha", "sun dried"],
            "Frozen": ["deep frozen", "iqf", "freezed"], "Chilled": ["cold", "refrigerated", "cold stored"],
            "Processed": ["processing", "value added"],
        },
        explanation="State the goods are supplied in.", placeholder="e.g. Fresh",
    ),
]
_COMMON_BY_KEY = {a["key"]: a for a in COMMON_ATTRIBUTES}

_ALL = ["packaging_type", "pack_size", "certifications", "grade", "origin"]
CATEGORY_COMMON: dict[str, list[str]] = {
    "Agriculture": [*_ALL, "condition"],
    "Food & Beverages": [*_ALL, "condition"],
    "Electronics": ["packaging_type", "certifications", "origin"],
    "Packaging": ["certifications", "grade", "origin"],
    "Textiles": ["packaging_type", "certifications", "grade", "origin"],
    "Industrial": _ALL,
    "Chemicals": _ALL,
    "Construction": _ALL,
    "Furniture": ["packaging_type", "certifications", "grade", "origin"],
    "Stationery": ["packaging_type", "origin"],
    "Learning": ["packaging_type", "origin"],
    "Beauty & Personal Care": ["packaging_type", "pack_size", "certifications", "origin"],
    "Healthcare & Medical": ["packaging_type", "pack_size", "certifications", "origin"],
    "Automotive": ["packaging_type", "certifications", "grade", "origin"],
}
DEFAULT_COMMON = _ALL

# Extra synonyms for well-known specific keys (the model rarely lists them).
_KEY_SYNONYMS: dict[str, list[str]] = {
    "variety": ["product type", "cultivar", "variant", "variety type", "kind"],
    "color": ["colour"],
    "colour": ["color"],
    "size": ["sizes"],
}


def common_keys_for(category: Optional[str]) -> list[str]:
    if not category:
        return list(DEFAULT_COMMON)
    try:
        from services.categories import normalize_category

        canon = normalize_category(category)
    except Exception:
        canon = category
    return list(CATEGORY_COMMON.get(canon, DEFAULT_COMMON))


def common_attributes(category: Optional[str]) -> list[dict[str, Any]]:
    return [_copy(_COMMON_BY_KEY[k]) for k in common_keys_for(category)]


def _copy(attr: dict[str, Any]) -> dict[str, Any]:
    out = dict(attr)
    out["options"] = list(attr.get("options") or [])
    out["synonyms"] = list(attr.get("synonyms") or [])
    out["option_synonyms"] = {k: list(v) for k, v in (attr.get("option_synonyms") or {}).items()}
    return out


# ---------------------------------------------------------------------------
# Family names
# ---------------------------------------------------------------------------
_STOP_WORDS = frozenset({
    "fresh", "organic", "premium", "pure", "natural", "raw", "dried", "dry", "frozen", "export", "quality", "grade", "wholesale",
    "bulk", "big", "small", "large", "good", "best", "new", "used", "high", "low", "extra", "a", "an", "the", "of", "for", "and",
    "with", "in", "kg", "g", "mt", "tonne", "ton", "pcs", "piece", "buy", "sell", "need", "want", "supply", "supplier",
    "fine", "super", "top", "first", "class", "standard", "bold", "whole", "ready", "farm",
})
_COMPOUND_HEADS = frozenset({
    "powder", "juice", "paste", "sauce", "flour", "pulp", "puree", "syrup", "chip", "flake", "extract", "concentrate",
})


def family_key_for(product_name: Any) -> str:
    """The head noun of a product name, singular and lowercase: "fuji apples" -> "apple", "milk powder" -> "milk powder"."""
    words = [w for w in re.findall(r"[a-z]+", str(product_name or "").lower()) if len(w) > 1 or w == "c"]
    toks = [_singular(w) for w in words]
    content = [t for t in toks if t not in _STOP_WORDS]
    if not content:
        content = toks
    if not content:
        return ""
    head = content[-1]
    if head in _COMPOUND_HEADS and len(content) >= 2:
        return f"{content[-2]} {head}"
    return head


def hsn_heading_of(hsn_code: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", str(hsn_code or ""))
    return digits[:4] if len(digits) >= 4 else None


# ---------------------------------------------------------------------------
# Family
# ---------------------------------------------------------------------------
class Family:
    """A product family's attributes with the lookups the normalizer needs. Not tied to the database."""

    def __init__(
        self, family_key: str, display_name: Optional[str] = None, category: Optional[str] = None,
        hsn_heading: Optional[str] = None, attributes: Optional[list[dict[str, Any]]] = None, source: str = "llm",
        persisted: bool = False,
    ) -> None:
        self.family_key = family_key
        self.display_name = display_name or family_key.title()
        self.category = category
        self.hsn_heading = hsn_heading
        self.source = source
        self.persisted = persisted
        self.attributes: list[dict[str, Any]] = [_fill(a) for a in (attributes or [])]
        offered = {a["key"] for a in self.attributes}
        # Common attributes this family does not offer are still recognised, so "packing: carton" files properly.
        self._known = self.attributes + [_copy(a) for a in COMMON_ATTRIBUTES if a["key"] not in offered]
        self._by_key = {a["key"]: a for a in self._known}
        self._product_tokens = {
            _singular(t) for t in re.findall(r"[a-z]+", f"{family_key} {self.display_name}".lower()) if len(t) > 2
        }
        self._phrases: dict[str, dict[str, Any]] = {}
        for a in self._known:
            for p in [a["key"], a["label"], _label_core(a["label"]), *a["synonyms"], *_KEY_SYNONYMS.get(a["key"], [])]:
                n = _norm(p)
                if n:
                    self._phrases.setdefault(n, a)
                    self._phrases.setdefault(_singular_phrase(n), a)
        self._option_cache: dict[str, list[tuple[str, str]]] = {}

    # -- lookups ---------------------------------------------------------
    def attr(self, key: str) -> Optional[dict[str, Any]]:
        return self._by_key.get(key)

    def label_for(self, key: str) -> str:
        a = self._by_key.get(key)
        return display_label(a["label"]) if a else display_label(_title(key.replace("_", " ")))

    def _variants(self, raw_key: str) -> tuple[list[str], Optional[str]]:
        """Normalized spellings of a raw key to try, most specific first, and a unit written in/after it."""
        unit = _paren_unit(raw_key)
        t = _norm(_label_core(raw_key))
        words = t.split()
        if words and words[-1] in _SUFFIX_UNITS and len(words) > 1:
            unit = unit or canonical_unit(words[-1])
            no_unit = " ".join(words[:-1])
        else:
            no_unit = t
        out: list[str] = []
        for base in (t, no_unit):
            for v in (base, " ".join(w for w in base.split() if _singular(w) not in self._product_tokens)):
                v = v.strip()
                if v and v not in out:
                    out.append(v)
                    s = _singular_phrase(v)
                    if s not in out:
                        out.append(s)
        stripped = [" ".join(w for w in v.split() if w not in ("type", "kind", "name", "of", "the")) for v in list(out)]
        for v in stripped:
            if v and v not in out:
                out.append(v)
        return out, unit

    def resolve(self, raw_key: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """(attribute, unit hint) for a raw key by key, label or synonym; fuzzy at >= 0.85; (None, hint) when unknown."""
        variants, unit = self._variants(raw_key)
        for v in variants:
            if v in self._phrases:
                return self._phrases[v], unit
        for v in variants:
            if len(v) >= 5:
                best, score = None, 0.0
                for phrase, a in self._phrases.items():
                    if len(phrase) < 5:
                        continue
                    r = _ratio(v, phrase)
                    if r > score:
                        best, score = a, r
                if best is not None and score >= FUZZY:
                    return best, unit
        return None, unit

    def canonical_key(self, raw_key: str) -> str:
        a, _ = self.resolve(raw_key)
        return a["key"] if a else self.free_key(raw_key)

    def free_key(self, raw_key: str) -> str:
        """snake_case of a key no attribute claims, without the product word and a trailing unit ("pack_weight_kg")."""
        words = [w for w in _norm(_label_core(raw_key)).split()]
        if len(words) > 1 and words[-1] in _SUFFIX_UNITS:
            words = words[:-1]
        kept = [w for w in words if _singular(w) not in self._product_tokens]
        return _snake(" ".join(kept or words))

    # -- options ---------------------------------------------------------
    def _option_index(self, attr: dict[str, Any]) -> list[tuple[str, str]]:
        """(normalized phrase, option) for every option and option synonym; longer phrases first."""
        cached = self._option_cache.get(attr["key"])
        if cached is not None:
            return cached
        pairs: list[tuple[str, str]] = []
        for opt in attr["options"]:
            pairs.append((_norm(opt), opt))
            for syn in (attr.get("option_synonyms") or {}).get(opt, []):
                pairs.append((_norm(syn), opt))
        for opt, syns in (attr.get("option_synonyms") or {}).items():
            if opt not in attr["options"]:
                for syn in syns:
                    pairs.append((_norm(syn), opt))
        pairs = [(p, o) for p, o in pairs if p]
        pairs.sort(key=lambda po: -len(po[0]))
        self._option_cache[attr["key"]] = pairs
        return pairs

    def match_option(self, attr: dict[str, Any], text: str) -> Optional[str]:
        """The attribute's known option the text names, or None (exact, synonym, one option inside the text, fuzzy >= 0.85)."""
        t = _norm(text)
        if not t:
            return None
        index = self._option_index(attr)
        candidates = [t, _singular_phrase(t)]
        no_product = " ".join(w for w in t.split() if _singular(w) not in self._product_tokens)
        if no_product and no_product not in candidates:
            candidates += [no_product, _singular_phrase(no_product)]
        for c in candidates:
            for phrase, opt in index:
                if c == phrase or c == _singular_phrase(phrase):
                    return opt
        padded = f" {no_product or t} "
        found: dict[str, int] = {}
        for phrase, opt in index:
            if len(phrase) < 3:
                continue
            for variant in {phrase, _singular_phrase(phrase)}:
                pos = padded.find(f" {variant} ")
                if pos < 0 and variant.endswith("y"):
                    continue
                if pos >= 0 and (opt not in found or pos < found[opt]):
                    found[opt] = pos
        if found:
            return min(found, key=lambda o: found[o])
        if len(t) >= 4:
            best, score = None, 0.0
            for phrase, opt in index:
                if len(phrase) < 4:
                    continue
                r = _ratio(t, phrase)
                if r > score:
                    best, score = opt, r
            if best is not None and score >= FUZZY:
                return best
        # "grade a" / "class a": a single-letter option behind a generic word
        stripped = " ".join(w for w in t.split() if w not in ("grade", "class", "quality", "type"))
        if stripped != t and stripped:
            for phrase, opt in index:
                if phrase == stripped:
                    return opt
        return None

    def option_owner(self, raw_key: str) -> Optional[tuple[dict[str, Any], str]]:
        """A multi-choice attribute and the option a bare flag key names: ``organic: True`` -> (certifications, "Organic")."""
        n = _norm(raw_key)
        if not n:
            return None
        for a in self._known:
            if a["type"] != MULTI:
                continue
            for phrase, opt in self._option_index(a):
                if n == phrase or _singular_phrase(n) == _singular_phrase(phrase):
                    return a, opt
        return None

    # -- values ----------------------------------------------------------
    _SPLIT_RE = re.compile(r"\s*(?:,|;|\+|&|\band\b|\bor\b|\bplus\b|\baur\b)\s*", re.IGNORECASE)
    _FILLER = frozenset({"certified", "certificate", "certificates", "certification", "certifications", "all", "the", "and", "must", "mandatory"})

    def _items(self, attr: dict[str, Any], value: Any) -> list[str]:
        raw: list[str] = []
        for v in value if isinstance(value, (list, tuple, set)) else [value]:
            if v in (None, ""):
                continue
            text = str(v).strip()
            if self.match_option(attr, text) and _norm(text) in {p for p, _ in self._option_index(attr)}:
                raw.append(text)  # "Loose / Bulk" is one option, not two
                continue
            raw.extend(p for p in self._SPLIT_RE.split(text) if p.strip())
            if "/" in text and len(raw) <= 1:
                raw = [p for r in raw for p in re.split(r"\s*/\s*", r)]
        return [r.strip() for r in raw if r.strip() and _norm(r) not in self._FILLER]

    def _normalize_choice(self, attr: dict[str, Any], value: Any) -> Any:
        out: list[str] = []
        for item in self._items(attr, value):
            out.append(self.match_option(attr, item) or _title_item(item))
        out = list(dict.fromkeys(out))
        if not out:
            return None
        return out[0] if len(out) == 1 else ", ".join(out)

    def _normalize_multi(self, attr: dict[str, Any], value: Any) -> Optional[list[str]]:
        out = [self.match_option(attr, item) or _title_item(item) for item in self._items(attr, value)]
        out = list(dict.fromkeys(out))
        return out or None

    def normalize(self, raw_key: Any, raw_value: Any) -> Optional[tuple[str, Any]]:
        """The canonical (key, value) of one spec, or None for an empty value or one of the form's own fields."""
        raw_key = str(raw_key)
        if chat_specs.is_core_key(raw_key) or _norm(raw_key) in {"name"}:
            return None
        value = raw_value
        if isinstance(value, dict):
            flat = chat_specs.flatten_details({raw_key: value})
            if len(flat) != 1:
                return None
            (k, v), = flat.items()
            if k != raw_key:
                return self.normalize(k, v)
            value = v
        if value is None or value == "" or value == [] or value == ():
            return None
        if isinstance(value, bool):
            owner = self.option_owner(raw_key)
            if owner is not None:
                return (owner[0]["key"], [owner[1]]) if value else None
            attr, _ = self.resolve(raw_key)
            return (attr["key"] if attr else self.free_key(raw_key), value)

        attr, unit_hint = self.resolve(raw_key)
        if attr is None:
            return self.free_key(raw_key), self._free_value(value, unit_hint)
        kind = attr["type"]
        if kind == NUMBER:
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(v) for v in value)
            norm = normalize_number(value, attr.get("unit"), unit_hint)
            if norm is None:
                found = _MEASURE_IN_TEXT_RE.search(str(value))  # "box of 20kg each" -> "20 kg"
                if found:
                    norm = normalize_number(found.group(0), attr.get("unit"), unit_hint)
            return attr["key"], norm if norm is not None else str(value).strip()
        if kind == MULTI:
            items = self._normalize_multi(attr, value)
            return (attr["key"], items) if items else None
        if kind == CHOICE:
            val = self._normalize_choice(attr, value)
            return (attr["key"], val) if val is not None else None
        text = ", ".join(str(v).strip() for v in value) if isinstance(value, (list, tuple)) else str(value).strip()
        return (attr["key"], text) if text else None

    @staticmethod
    def _free_value(value: Any, unit_hint: Optional[str]) -> Any:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, (int, float)) and unit_hint:
            return f"{value} {unit_hint}"
        if isinstance(value, str):
            text = value.strip()
            if unit_hint and re.fullmatch(r"\d+(?:\.\d+)?", text):
                return f"{text} {unit_hint}"
            return text
        return value

    # -- whole product_details -------------------------------------------
    def normalize_details(self, details: Optional[dict[str, Any]], *, strip_core: bool = False) -> dict[str, Any]:
        """``details`` with canonical keys and values. Nested values are flattened; ``<key>__must_match`` flags follow their
        spec and a False flag is dropped. The form's core fields are kept (or removed with ``strip_core``)."""
        flat = chat_specs.flatten_details(details)
        out: dict[str, Any] = {}
        exact: set[str] = set()
        flags: list[tuple[str, bool]] = []
        for raw_key, raw_value in flat.items():
            key = str(raw_key)
            if key in PASSTHROUGH_KEYS:
                out[key] = raw_value
                continue
            if key.endswith(MUST_SUFFIX):
                flags.append((key[: -len(MUST_SUFFIX)], bool(raw_value) and str(raw_value).strip().lower() not in ("false", "no", "0")))
                continue
            if chat_specs.is_core_key(key):
                if not strip_core:
                    out[key] = raw_value
                continue
            res = self.normalize(key, raw_value)
            if res is None:
                continue
            ckey, cval = res
            is_exact = _norm(key) == _norm(ckey)
            if ckey in out:
                prev = out[ckey]
                if isinstance(prev, list) and isinstance(cval, list):
                    out[ckey] = list(dict.fromkeys([*prev, *cval]))
                    continue
                if ckey in exact and not is_exact:
                    continue
            out[ckey] = cval
            if is_exact:
                exact.add(ckey)
        for base, flag in flags:
            if not flag:
                continue
            ckey = self.canonical_key(base)
            if ckey in PASSTHROUGH_KEYS:
                continue
            out[f"{ckey}{MUST_SUFFIX}"] = True
        return out

    # -- outputs ---------------------------------------------------------
    def suggestions(self) -> list[dict[str, Any]]:
        """The attributes in the SuggestedAttribute shape (plus ``option_synonyms`` for the chat's own use)."""
        out: list[dict[str, Any]] = []
        for a in self.attributes:
            out.append({
                "key": a["key"], "label": a["label"], "placeholder": a.get("placeholder") or f"e.g. {display_label(a['label'])}",
                "input_type": {NUMBER: "numeric", TEXT: "text"}.get(a["type"], "select"), "options": list(a["options"]),
                "explanation": a.get("explanation") or None, "must_match": bool(a.get("must_match_default")),
                "unit": a.get("unit"), "multi": a["type"] == MULTI, "synonyms": list(a["synonyms"]),
                "option_synonyms": {k: list(v) for k, v in (a.get("option_synonyms") or {}).items()},
            })
        return out

    def phrases(self, key: str) -> list[str]:
        """Words a person may use for ``key`` (its label and synonyms), for must-match phrases in free text."""
        a = self._by_key.get(key)
        if a is None:
            return [key.replace("_", " ")]
        out = [display_label(a["label"]), key.replace("_", " "), *a["synonyms"], *_KEY_SYNONYMS.get(key, [])]
        return list(dict.fromkeys(p for p in out if p))

    def to_attributes(self) -> list[dict[str, Any]]:
        return [_copy(a) for a in self.attributes]


def _fill(attr: dict[str, Any]) -> dict[str, Any]:
    a = _copy(attr)
    a.setdefault("label", _title(a["key"].replace("_", " ")))
    a["type"] = a.get("type") if a.get("type") in (CHOICE, MULTI, NUMBER, TEXT) else TEXT
    a["unit"] = canonical_unit(a.get("unit")) if a.get("unit") else None
    a["unit_family"] = a.get("unit_family") or unit_family_of(a["unit"])
    a["must_match_default"] = bool(a.get("must_match_default"))
    a.setdefault("explanation", "")
    a.setdefault("placeholder", "")
    return a


# ---------------------------------------------------------------------------
# LLM suggestions -> catalogue attributes
# ---------------------------------------------------------------------------
def _product_tokens(product_name: str) -> set[str]:
    return {_singular(t) for t in re.findall(r"[a-z]+", str(product_name).lower()) if len(t) > 2}


def _suggestion_key(s: dict[str, Any], product_tokens: set[str]) -> tuple[str, Optional[str]]:
    """(canonical key, unit) of a recommender suggestion: its label without the product word and unit."""
    label = str(s.get("label") or s.get("key") or "").strip()
    unit = s.get("unit") or _paren_unit(label)
    if "synonyms" in s and s.get("key"):  # already a catalogue attribute: the key is canonical
        return _snake(s["key"]), unit
    words = _norm(_label_core(label)).split()
    kept = [w for w in words if _singular(w) not in product_tokens]
    key = _snake(" ".join(kept or words)) or _snake(s.get("key") or "")
    return key, unit


def _from_suggestion(s: dict[str, Any], key: str, unit: Optional[str]) -> dict[str, Any]:
    options = list(dict.fromkeys(str(o).strip() for o in (s.get("options") or []) if str(o).strip()))
    input_type = str(s.get("input_type") or "").lower()
    if s.get("multi"):
        kind = MULTI
    elif input_type == "numeric" or (unit and not options):
        kind = NUMBER
    elif input_type == "select" and options:
        kind = CHOICE
    else:
        kind = TEXT
    label = str(s.get("label") or key.replace("_", " ").title()).strip()
    return make_attr(
        key, label, kind, options=options if kind != TEXT else [], unit=unit if kind == NUMBER else None,
        synonyms=s.get("synonyms") or [], option_synonyms=s.get("option_synonyms") or None, must=bool(s.get("must_match")),
        explanation=str(s.get("explanation") or ""), placeholder=str(s.get("placeholder") or ""),
    )


def _merge_into(base: dict[str, Any], extra: dict[str, Any], fam: Family) -> None:
    """Fold a duplicate attribute (an LLM "Packaging Type") into ``base``: names become synonyms, options are mapped."""
    names = [extra["label"], display_label(extra["label"]), extra["key"].replace("_", " "), *extra["synonyms"]]
    have = {_norm(x) for x in [base["key"], base["label"], display_label(base["label"]), *base["synonyms"]]}
    for n in names:
        if n and _norm(n) not in have:
            base["synonyms"].append(n)
            have.add(_norm(n))
    base["must_match_default"] = base["must_match_default"] or extra["must_match_default"]
    if not base.get("explanation"):
        base["explanation"] = extra.get("explanation") or ""
    if base["type"] in (CHOICE, MULTI) and extra["type"] in (CHOICE, MULTI):
        tmp = Family(base["key"], attributes=[base])
        for opt in extra["options"]:
            known = tmp.match_option(base, opt)
            if known is None:
                if len(base["options"]) < MAX_OPTIONS:
                    base["options"].append(opt)
                    tmp._option_cache.clear()
            elif _norm(opt) != _norm(known):
                syns = base["option_synonyms"].setdefault(known, [])
                if _norm(opt) not in {_norm(x) for x in syns}:
                    syns.append(opt)
                tmp._option_cache.clear()
    elif base["type"] == NUMBER and not base.get("unit") and extra.get("unit"):
        base["unit"] = extra["unit"]
        base["unit_family"] = unit_family_of(extra["unit"])


def attributes_from_suggestions(
    product_name: str, category: Optional[str], suggestions: Optional[list[dict[str, Any]]], *, with_common: bool = True,
) -> list[dict[str, Any]]:
    """Recommender suggestions as catalogue attributes: canonical keys (no product word, unit split off the label),
    duplicates merged (also into the COMMON attributes, so "Packaging Type" becomes ``packaging_type``), and the category's
    common attributes appended. Idempotent for catalogue-shaped input."""
    tokens = _product_tokens(product_name) | _product_tokens(family_key_for(product_name))
    common = {a["key"]: a for a in common_attributes(category)} if with_common else {}
    common_fam = Family("_common", attributes=list(COMMON_ATTRIBUTES))
    out: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for s in suggestions or []:
        if not isinstance(s, dict) or not (s.get("label") or s.get("key")):
            continue
        key, unit = _suggestion_key(s, tokens)
        if not key:
            continue
        if "synonyms" not in s:  # a raw recommender suggestion may name a common spec in its own words
            hit, _ = common_fam.resolve(str(s.get("label") or s.get("key")))
            if hit is None:
                hit, _ = common_fam.resolve(str(s.get("key") or ""))
            if hit is not None:
                key = hit["key"]
        attr = _from_suggestion(s, key, unit)
        if key in common and key not in by_key:
            base = _copy(common[key])
            _merge_into(base, attr, common_fam)
            by_key[key] = base
            out.append(base)
        elif key in by_key:
            _merge_into(by_key[key], attr, common_fam)
        else:
            by_key[key] = attr
            out.append(attr)
    for key, attr in common.items():
        if key not in by_key:
            by_key[key] = _copy(attr)
            out.append(by_key[key])
    # Specs that are the same thing under two names (fuzzy label match) collapse into the first.
    final: list[dict[str, Any]] = []
    for a in out:
        twin = next((f for f in final if _ratio(_norm(display_label(f["label"])), _norm(display_label(a["label"]))) >= 0.92), None)
        if twin is not None and twin["type"] == a["type"]:
            _merge_into(twin, a, common_fam)
        else:
            final.append(a)
    return final


def family_from_suggestions(
    product_name: Any, category: Optional[str] = None, suggestions: Optional[list[dict[str, Any]]] = None,
    hsn_code: Any = None,
) -> Family:
    """A family built in memory (no database): the suggestions plus the category's common attributes."""
    name = str(product_name or "")
    key = family_key_for(name) or "product"
    attrs = attributes_from_suggestions(name, category, suggestions)
    return Family(key, key.title(), category, hsn_heading_of(hsn_code), attrs, source="chat")


def canonicalize_suggestions(
    product_name: str, category: Optional[str], suggestions: Optional[list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """The chat's suggestion list with canonical keys, merged duplicates and the catalogue's extra fields, WITHOUT adding
    attributes the list did not have."""
    attrs = attributes_from_suggestions(product_name, category, suggestions, with_common=False)
    return Family(family_key_for(product_name) or "product", attributes=attrs).suggestions()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def family_from_row(row: Any) -> Family:
    return Family(
        row.family_key, row.display_name, row.category, row.hsn_heading, list(row.attributes or []), row.source, persisted=True
    )


async def find_family(
    db: AsyncSession, product_name: Any, category: Optional[str] = None, hsn_code: Any = None
) -> Optional[Family]:
    """The stored family for a product, or None. Looks up the head noun, then the whole phrase, then a family one of whose
    choice options is a word of the name ("fuji" -> apple), then the HSN heading when exactly one family has it."""
    from models.spec_catalog import SpecCatalog

    key = family_key_for(product_name)
    phrase = " ".join(_singular(w) for w in re.findall(r"[a-z]+", str(product_name or "").lower()) if w not in _STOP_WORDS)
    for candidate in dict.fromkeys(k for k in (phrase, key) if k):
        row = (await db.execute(select(SpecCatalog).where(SpecCatalog.family_key == candidate))).scalar_one_or_none()
        if row is not None:
            return family_from_row(row)
    rows = list((await db.execute(select(SpecCatalog))).scalars())
    words = {_singular(w) for w in re.findall(r"[a-z]+", str(product_name or "").lower()) if len(w) > 2}
    for row in rows:
        fam = family_from_row(row)
        for a in fam.attributes:
            if a["key"] in _COMMON_BY_KEY:
                continue  # "box" or "premium" is an option of every family, so it names none
            if a["type"] in (CHOICE, MULTI) and any(_singular_phrase(_norm(o)) in words for o in a["options"]):
                return fam
    heading = hsn_heading_of(hsn_code)
    if heading:
        same = [r for r in rows if r.hsn_heading == heading]
        if len(same) == 1:
            return family_from_row(same[0])
    return None


_locks: dict[str, tuple[Any, asyncio.Lock]] = {}
_failed_until: dict[str, float] = {}
FAILURE_BACKOFF = 60.0


def _lock_for(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    hit = _locks.get(key)
    if hit is None or hit[0] is not loop:
        hit = (loop, asyncio.Lock())
        _locks[key] = hit
    return hit[1]


def reset_state() -> None:
    _locks.clear()
    _failed_until.clear()


async def ensure_family(
    product_name: Any, category: Optional[str] = None, hsn_code: Any = None, role: Optional[str] = None, *,
    generate: bool = True,
) -> Family:
    """The family for a product: the stored row, else one generated ONCE by the attribute recommender and stored.

    Concurrent calls for one family wait on a per-family lock and share a single generation (a second process is covered by
    the unique ``family_key``: the insert is ``ON CONFLICT DO NOTHING`` and the stored row wins). When the recommender
    returns nothing the family is NOT stored: the caller gets the common attributes only and a later call tries again
    (after a short back-off).
    """
    from db.session import SessionLocal
    from models.spec_catalog import SpecCatalog
    from services import attribute_recommender

    key = family_key_for(product_name)
    if not key:
        return family_from_suggestions(product_name, category)
    async with SessionLocal() as db:
        found = await find_family(db, product_name, category, hsn_code)
    if found is not None or not generate:
        return found or family_from_suggestions(product_name, category, hsn_code=hsn_code)
    async with _lock_for(key):
        async with SessionLocal() as db:
            found = await find_family(db, product_name, category, hsn_code)
        if found is not None:
            return found
        if _failed_until.get(key, 0.0) > time.monotonic():
            return family_from_suggestions(product_name, category, hsn_code=hsn_code)
        attrs, _source = await attribute_recommender.recommend_product_attributes(key.title(), category, role)
        if not attrs:
            _failed_until[key] = time.monotonic() + FAILURE_BACKOFF
            return family_from_suggestions(product_name, category, hsn_code=hsn_code)
        merged = attributes_from_suggestions(key, category, attrs)
        async with SessionLocal() as db:
            await db.execute(
                pg_insert(SpecCatalog).values(
                    family_key=key, display_name=key.title(), category=category, hsn_heading=hsn_heading_of(hsn_code),
                    attributes=merged, source="llm",
                ).on_conflict_do_nothing(index_elements=["family_key"])
            )
            await db.commit()
            row = (await db.execute(select(SpecCatalog).where(SpecCatalog.family_key == key))).scalar_one()
            return family_from_row(row)


async def normalize_for_rfq(
    db: AsyncSession, details: Optional[dict[str, Any]], title: Any = None, category: Optional[str] = None,
) -> dict[str, Any]:
    """``product_details`` of an RFQ through its family's catalogue (stored row, else the common attributes only).

    Never generates (no model call at save time) and never raises: a failure leaves the details as they were.
    """
    if not isinstance(details, dict) or not details:
        return details or {}
    try:
        name = details.get("name") or title or ""
        fam = await find_family(db, name, category, details.get("hsn_code")) if name else None
        if fam is None:
            fam = family_from_suggestions(name, category, hsn_code=details.get("hsn_code"))
        return fam.normalize_details(details)
    except Exception as exc:
        logger.warning("spec normalization skipped: %s", exc)
        return details


# ---------------------------------------------------------------------------
# Comparing values (matching)
# ---------------------------------------------------------------------------
_COMMON_FAMILY = Family("_common", attributes=list(COMMON_ATTRIBUTES))


def _as_items(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v not in (None, "")]
    return [p for p in re.split(r"\s*(?:,|;|&|\band\b)\s*", str(value)) if p.strip()]


def compare_values(key: str, wanted: Any, offered: Any) -> Optional[tuple[float, str]]:
    """(score 0..1, reason) comparing two values of the same catalogue key by type, or None when the pair is not typed
    (the caller then falls back to its generic comparison)."""
    attr = _COMMON_FAMILY.attr(key)
    if isinstance(wanted, (list, tuple)) or isinstance(offered, (list, tuple)) or (attr is not None and attr["type"] == MULTI):
        w_items = _as_items(wanted)
        o_items = _as_items(offered)
        if not w_items or not o_items:
            return None
        if attr is not None and attr["type"] == MULTI:
            canon = lambda x: _COMMON_FAMILY.match_option(attr, x) or _title_item(x)  # noqa: E731
        else:
            canon = lambda x: _norm(x)  # noqa: E731
        w, o = {canon(x) for x in w_items}, {canon(x) for x in o_items}
        hit = len(w & o)
        if hit == len(w):
            return 1.0, "All requested values offered"
        if hit == 0:
            return 0.0, "None of the requested values offered"
        return min(0.84, 0.35 + 0.6 * hit / len(w)), f"Offers {hit} of {len(w)} requested values"
    pw, po = parse_number(wanted), parse_number(offered)
    if pw is not None and po is not None:
        unit = pw["unit"] or po["unit"] or (attr or {}).get("unit")
        if not unit:
            return None  # two bare numbers: the caller's own comparison
        w, o = _interval_in(pw, unit), _interval_in(po, unit)
        if w is None or o is None:
            return None  # different dimensions (kg vs mm)
        return _interval_score(*w, *o)
    if attr is not None and attr["type"] == CHOICE and isinstance(wanted, str) and isinstance(offered, str):
        cw, co = _COMMON_FAMILY.match_option(attr, wanted), _COMMON_FAMILY.match_option(attr, offered)
        if cw and co:
            return (1.0, "Exact match" if _norm(wanted) == _norm(offered) else f"Same option ({cw})") if cw == co else (0.0, "Different option")
    return None


def _interval_in(parsed: dict[str, Any], unit: str) -> Optional[tuple[Optional[Decimal], Optional[Decimal]]]:
    """The number's interval expressed in ``unit`` (None when its own unit cannot be converted to it)."""
    given = parsed["unit"]
    lo, hi = parsed["lo"], parsed["hi"]
    if given and given != unit:
        lo = _convert(lo, given, unit)
        hi = _convert(hi, given, unit) if hi is not None else None
        if lo is None or (parsed["hi"] is not None and hi is None):
            return None
    return _interval({**parsed, "lo": lo, "hi": hi})


def _interval_score(
    lo_w: Optional[Decimal], hi_w: Optional[Decimal], lo_o: Optional[Decimal], hi_o: Optional[Decimal]
) -> tuple[float, str]:
    inf = Decimal("1e30")
    a1, a2 = (lo_w if lo_w is not None else -inf), (hi_w if hi_w is not None else inf)
    b1, b2 = (lo_o if lo_o is not None else -inf), (hi_o if hi_o is not None else inf)
    if lo_w is not None and lo_w == hi_w and lo_o is not None and lo_o == hi_o:  # two exact numbers
        top = max(abs(a1), abs(b1)) or Decimal(1)
        rel = abs(a1 - b1) / top
        if rel <= Decimal("0.05"):
            return 1.0, "Equal within 5%"
        return max(0.0, float(1 - rel)), "Different value"
    if (b1 >= a1 and b2 <= a2) or (a1 >= b1 and a2 <= b2):
        return 1.0, "Within the requested range"
    if b2 >= a1 and b1 <= a2:
        return 0.7, "Ranges overlap"
    gap = (a1 - b2) if b2 < a1 else (b1 - a2)
    ref = max(abs(a1) if a1 > -inf else Decimal(0), abs(a2) if a2 < inf else Decimal(0), Decimal(1))
    return max(0.0, float(1 - 2 * gap / ref)), "Outside the requested range"

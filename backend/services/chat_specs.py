"""Keeping the chat draft's product_details honest and clean: strip the form's core fields out of the free-form specs, flatten nested values to text, drop unit-suffixed duplicates, and normalise the price's per-unit. Pure functions."""


import logging
import re
from typing import Any, Optional


logger = logging.getLogger(__name__)



MUST_MATCH_SUFFIX = "__must_match"


# Keys that belong to the RFQ form itself, never to the free-form specs.
CORE_KEYS = frozenset({
    "quantity_value", "quantity_unit", "quantity", "price_amount", "price_currency", "price_per_unit", "price",
    "min_order_value", "min_order_unit", "min_order", "minimum_order", "city", "state", "country", "location",
    "deadline_days", "deadline_date", "deadline", "role", "category", "status", "title", "description",
    "product_name",
})


_KEEP_HSN = {"hsn_code", "hsn_source"}


def _norm(key: Any) -> str:
    return re.sub(r"[\s\-]+", "_", str(key).strip().lower())


def is_core_key(key: Any) -> bool:
    k = _norm(key)
    if k.endswith(MUST_MATCH_SUFFIX):
        return False
    if k.startswith("hsn_"):
        return k not in _KEEP_HSN
    return k in CORE_KEYS


# ---------------------------------------------------------------------------
# Price unit
# ---------------------------------------------------------------------------
_PER_PREFIX_RE = re.compile(r"^\s*(?:(?:per|par|prati|प्रति)\b\s*|/\s*)+", re.IGNORECASE)


def normalize_per_unit(value: Any) -> Optional[str]:
    """"per kg" / "par kg" / "/kg" -> "kg"; booleans and empty values -> None."""
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    text = _PER_PREFIX_RE.sub("", str(value)).strip(" .")
    if not text or text.lower() in ("true", "false", "yes", "no", "none", "null", "n/a", "per"):
        return None
    return _PER_UNIT_SPELLINGS.get(text.lower(), text)


# Spellings of one unit ("12 rs kilo" is per kg); real unit reference data, not a vocabulary guess.
_PER_UNIT_SPELLINGS = {
    "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg", "kgs": "kg", "kg.": "kg",
    "qtl": "quintal", "quintals": "quintal",
}


# ---------------------------------------------------------------------------
# Flatten and clean
# ---------------------------------------------------------------------------
_VALUE_KEYS = {"value", "amount", "qty", "quantity", "number"}


_UNIT_KEYS = {"unit", "units", "uom"}


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _flatten_into(out: dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, dict):
        keys = {_norm(k) for k in value}
        if keys and keys <= (_VALUE_KEYS | _UNIT_KEYS) and keys & _VALUE_KEYS:
            val = next(v for k, v in value.items() if _norm(k) in _VALUE_KEYS)
            unit = next((v for k, v in value.items() if _norm(k) in _UNIT_KEYS), "")
            text = f"{_as_text(val)} {unit or ''}".strip()
            if text:
                out.setdefault(key, text)
            return
        for inner_key, inner in value.items():
            _flatten_into(out, str(inner_key), inner)
        return
    if isinstance(value, (list, tuple, set)):
        items = [_as_text(i) if not isinstance(i, dict) else ", ".join(
            f"{k} {_as_text(v)}" for k, v in i.items()
        ) for i in value if i not in (None, "")]
        if items:
            out[key] = items
        return
    if value is None or value == "":
        return
    out[key] = value


_UNIT_SUFFIX_RE = re.compile(r"^(.+?)_(kg|g|mg|gm|mm|cm|m|l|ml|pcs|pc|days|day|mt|n_cm2|percent|pct)$")


_NUM_IN_TEXT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    return [float(m.replace(",", "")) for m in _NUM_IN_TEXT_RE.findall(str(value))]


def drop_unit_variants(details: dict[str, Any]) -> dict[str, Any]:
    """Drop ``pack_weight_kg: 20`` when ``pack_weight: "20 kg"`` already says it."""
    by_norm = {_norm(k): k for k in details}
    for key in list(details):
        m = _UNIT_SUFFIX_RE.match(_norm(key))
        if not m:
            continue
        base_key = by_norm.get(m.group(1))
        if base_key is None or base_key == key:
            continue
        mine, base = _numbers(details[key]), _numbers(details[base_key])
        if mine and base and mine == base:
            details.pop(key, None)
            details.pop(f"{key}{MUST_MATCH_SUFFIX}", None)
    return details


def flatten_details(details: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Nested dicts become text or top-level keys; ``{"value": 20, "unit": "kg"}`` becomes "20 kg"."""
    out: dict[str, Any] = {}
    for key, value in (details or {}).items():
        _flatten_into(out, str(key), value)
    return drop_unit_variants(out)


def strip_core(details: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split ``details`` into (specs, core entries) where core entries are the form's own fields."""
    clean: dict[str, Any] = {}
    core: dict[str, Any] = {}
    for key, value in details.items():
        (core if is_core_key(key) else clean)[key] = value
    return clean, core


def clean_product_details(details: Optional[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flatten, then split into (clean specs, core entries that were stripped out)."""
    return strip_core(flatten_details(details))


def core_as_fields(core: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """Core entries a model put into the specs, as tool arguments for the draft fields that are still empty."""
    fields: dict[str, Any] = {}
    norm = {_norm(k): v for k, v in core.items()}
    if not draft.get("quantity") and norm.get("quantity_value") is not None:
        fields["quantity_value"] = norm["quantity_value"]
        if norm.get("quantity_unit"):
            fields["quantity_unit"] = norm["quantity_unit"]
    if not draft.get("price_target") and norm.get("price_amount") is not None:
        fields["price_amount"] = norm["price_amount"]
        for k in ("price_currency", "price_per_unit"):
            if norm.get(k) is not None:
                fields[k] = norm[k]
    if not draft.get("minimum_order") and norm.get("min_order_value") is not None:
        fields["min_order_value"] = norm["min_order_value"]
        if norm.get("min_order_unit"):
            fields["min_order_unit"] = norm["min_order_unit"]
    loc = draft.get("location") or {}
    for k in ("city", "state", "country"):
        if not loc.get(k) and norm.get(k):
            fields[k] = norm[k]
    dl = draft.get("deadline") or {}
    if not dl and norm.get("deadline_days") is not None:
        fields["deadline_days"] = norm["deadline_days"]
    if not dl and norm.get("deadline_date"):
        fields["deadline_date"] = norm["deadline_date"]
    if not draft.get("category") and norm.get("category"):
        fields["category"] = norm["category"]
    return fields

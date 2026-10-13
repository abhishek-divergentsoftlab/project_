"""Intelligent unit converter for mass, weight, and agricultural packaging.

Provides exact conversion factors for metric and imperial mass units:
- Metric Tonnes / Tons / MT (1,000 kg)
- Quintals / qtl (100 kg)
- Kilograms / kg / kilos (1 kg)
- Grams / g / gm (0.001 kg)
- Pounds / lbs (0.453592 kg)

Count units resolve to pieces (pcs, dozen = 12, pair = 2, gross = 144), and
``clean_unit`` absorbs typing noise such as "Kgs." or "Metric  Tons".

And recognized commercial & agricultural packaging containers:
- Bora / Bori / Katta / Gunny bag / Bag / Sack (jute/poly bags)
- Bale / Carton / Box / Crate / Drum / Pallet
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

# Standard conversion multiplier to Kilograms (kg)
MASS_CONVERSION_TO_KG: dict[str, Decimal] = {
    # Metric Tonnes / Tons / MT
    "tonne": Decimal("1000"),
    "tonnes": Decimal("1000"),
    "ton": Decimal("1000"),
    "tons": Decimal("1000"),
    "mt": Decimal("1000"),
    "t": Decimal("1000"),
    "metric ton": Decimal("1000"),
    "metric tons": Decimal("1000"),
    "metric tonne": Decimal("1000"),
    "metric tonnes": Decimal("1000"),
    # Quintals (standard in Indian and agricultural mandis: 1 quintal = 100 kg)
    "quintal": Decimal("100"),
    "quintals": Decimal("100"),
    "qtl": Decimal("100"),
    # Kilograms
    "k": Decimal("1"),
    "kg": Decimal("1"),
    "kgs": Decimal("1"),
    "kilo": Decimal("1"),
    "kilos": Decimal("1"),
    "kilogram": Decimal("1"),
    "kilograms": Decimal("1"),
    # Grams
    "g": Decimal("0.001"),
    "gm": Decimal("0.001"),
    "gms": Decimal("0.001"),
    "gram": Decimal("0.001"),
    "grams": Decimal("0.001"),
    # Imperial
    "lb": Decimal("0.453592"),
    "lbs": Decimal("0.453592"),
    "pound": Decimal("0.453592"),
    "pounds": Decimal("0.453592"),
    # Hindi / Mandi mass units
    "क्विंटल": Decimal("100"),
    "क्विंतल": Decimal("100"),
    "किलो": Decimal("1"),
    "किलोग्राम": Decimal("1"),
    "कीलो": Decimal("1"),
    "ग्राम": Decimal("0.001"),
    "टन": Decimal("1000"),
    "मीट्रिक टन": Decimal("1000"),
    # Ounces
    "oz": Decimal("0.0283495"),
    "ounce": Decimal("0.0283495"),
    "ounces": Decimal("0.0283495"),
    # Fixed packaging weight representations
    "100kg": Decimal("100"),
    "100 kg": Decimal("100"),
    "50kg": Decimal("50"),
    "50 kg": Decimal("50"),
}

# Standard conversion multiplier to Meters (m)
LENGTH_CONVERSION_TO_METER: dict[str, Decimal] = {
    "m": Decimal("1"),
    "meter": Decimal("1"),
    "meters": Decimal("1"),
    "metre": Decimal("1"),
    "metres": Decimal("1"),
    "cm": Decimal("0.01"),
    "cms": Decimal("0.01"),
    "centimeter": Decimal("0.01"),
    "centimeters": Decimal("0.01"),
    "centimetre": Decimal("0.01"),
    "centimetres": Decimal("0.01"),
    "mm": Decimal("0.001"),
    "mms": Decimal("0.001"),
    "millimeter": Decimal("0.001"),
    "millimeters": Decimal("0.001"),
    "millimetre": Decimal("0.001"),
    "millimetres": Decimal("0.001"),
    "km": Decimal("1000"),
    "kilometer": Decimal("1000"),
    "kilometers": Decimal("1000"),
    "in": Decimal("0.0254"),
    "inch": Decimal("0.0254"),
    "inches": Decimal("0.0254"),
    "ft": Decimal("0.3048"),
    "feet": Decimal("0.3048"),
    "foot": Decimal("0.3048"),
    "yd": Decimal("0.9144"),
    "yard": Decimal("0.9144"),
    "yards": Decimal("0.9144"),
    # Hindi length units
    "मीटर": Decimal("1"),
    "किमी": Decimal("1000"),
    "सेमी": Decimal("0.01"),
    "मिमी": Decimal("0.001"),
}

# Standard conversion multiplier to Liters (L)
VOLUME_CONVERSION_TO_LITER: dict[str, Decimal] = {
    "l": Decimal("1"),
    "liter": Decimal("1"),
    "liters": Decimal("1"),
    "litre": Decimal("1"),
    "litres": Decimal("1"),
    "ltr": Decimal("1"),
    "ltrs": Decimal("1"),
    "ml": Decimal("0.001"),
    "milliliter": Decimal("0.001"),
    "milliliters": Decimal("0.001"),
    "millilitre": Decimal("0.001"),
    "millilitres": Decimal("0.001"),
    "cc": Decimal("0.001"),
    "kl": Decimal("1000"),
    "kilolitre": Decimal("1000"),
    "kilolitres": Decimal("1000"),
    "kiloliter": Decimal("1000"),
    "kiloliters": Decimal("1000"),
    "m3": Decimal("1000"),
    "cubic meter": Decimal("1000"),
    "cubic meters": Decimal("1000"),
    "cubic metre": Decimal("1000"),
    "cubic metres": Decimal("1000"),
    "m³": Decimal("1000"),
    "gallon": Decimal("3.78541"),
    "gallons": Decimal("3.78541"),
    "gal": Decimal("3.78541"),
    "barrel": Decimal("158.987"),
    "barrels": Decimal("158.987"),
    "bbl": Decimal("158.987"),
    # Hindi volume units
    "लीटर": Decimal("1"),
    "मिली": Decimal("0.001"),
}

# Standard conversion multiplier to Watts (W)
POWER_CONVERSION_TO_WATT: dict[str, Decimal] = {
    "w": Decimal("1"),
    "watt": Decimal("1"),
    "watts": Decimal("1"),
    "kw": Decimal("1000"),
    "kilowatt": Decimal("1000"),
    "kilowatts": Decimal("1000"),
    "mw": Decimal("1000000"),
    "hp": Decimal("745.7"),
}

# Standard conversion multiplier to Volts (V)
VOLTAGE_CONVERSION_TO_VOLT: dict[str, Decimal] = {
    "v": Decimal("1"),
    "volt": Decimal("1"),
    "volts": Decimal("1"),
    "kv": Decimal("1000"),
    "kilovolt": Decimal("1000"),
    "mv": Decimal("0.001"),
}

# Standard conversion multiplier to Ampere-hours (Ah)
CAPACITY_CONVERSION_TO_AH: dict[str, Decimal] = {
    "ah": Decimal("1"),
    "mah": Decimal("0.001"),
}

# Count units, as multiples of one piece. "2 dozen" is 24 pieces, so a buyer
# asking for 24 pcs and a seller offering 2 dozen are the same order.
COUNT_CONVERSION_TO_PIECES: dict[str, Decimal] = {
    "pc": Decimal("1"),
    "pcs": Decimal("1"),
    "piece": Decimal("1"),
    "pieces": Decimal("1"),
    "unit": Decimal("1"),
    "units": Decimal("1"),
    "no": Decimal("1"),
    "nos": Decimal("1"),
    "number": Decimal("1"),
    "numbers": Decimal("1"),
    "each": Decimal("1"),
    "ea": Decimal("1"),
    "item": Decimal("1"),
    "items": Decimal("1"),
    "pair": Decimal("2"),
    "pairs": Decimal("2"),
    "dozen": Decimal("12"),
    "dozens": Decimal("12"),
    "doz": Decimal("12"),
    "dz": Decimal("12"),
    "gross": Decimal("144"),
    "set": Decimal("1"),
    "sets": Decimal("1"),
    "bundle": Decimal("1"),
    "bundles": Decimal("1"),
    "roll": Decimal("1"),
    "rolls": Decimal("1"),
    "sheet": Decimal("1"),
    "sheets": Decimal("1"),
    "pack": Decimal("1"),
    "packs": Decimal("1"),
    # Hindi count units
    "दर्जन": Decimal("12"),
    "पीस": Decimal("1"),
    "नग": Decimal("1"),
    "पैकेट": Decimal("1"),
    "सेट": Decimal("1"),
}

# Recognized trade packaging containers
PACKAGING_UNITS: frozenset[str] = frozenset(
    {
        "bora",
        "boras",
        "bori",
        "boris",
        "katta",
        "kattas",
        "bag",
        "bags",
        "sack",
        "sacks",
        "gunny bag",
        "gunny bags",
        "bale",
        "bales",
        "carton",
        "cartons",
        "box",
        "boxes",
        "crate",
        "crates",
        "drum",
        "drums",
        "pallet",
        "pallets",
        "pack",
        "packs",
        # Hindi packaging containers
        "बोरी",
        "बोरा",
        "कट्टा",
        "कट्टे",
        "बैग",
        "थैला",
        "पेटी",
        "कार्टन",
    }
)


_UNIT_PUNCT_RE = re.compile(r"[.\s]+")
_PREFIX_STRIP_RE = re.compile(
    r"^(?:"
    r"per\s*[/:\-]?\s*"
    r"|/\s*-\s*"
    r"|/\s*"
    r"|-\s*"
    r"|प्रति\s*[/:\-]?\s*"
    r"|(?:₹|rs\.?|inr|rupees?|रुपये|रुपया|\$|usd|eur|€|£|gbp)\s*(?:/|per\s*|प्रति\s*)?\s*"
    r")+",
    re.IGNORECASE,
)


def clean_unit(unit: Optional[str]) -> Optional[str]:
    """Canonical spelling of a unit as people type it.

    Lowercased, trailing dots dropped, leading slashes/hyphens/currencies/per stripped,
    and whitespace collapsed, so "/quintal", "rs/kg", "per/kg", "Kgs.",
    " KG " and "Metric  Tons" look the way the conversion tables expect.
    """
    if unit is None:
        return None
    cleaned = str(unit).strip().lower().rstrip(".").strip()
    cleaned = _PREFIX_STRIP_RE.sub("", cleaned).strip()
    cleaned = _UNIT_PUNCT_RE.sub(" ", cleaned).strip()
    # Check if last word is a known unit (e.g. "rupees per kg" -> "kg", "1000 per quintal" -> "quintal")
    if (
        cleaned not in MASS_CONVERSION_TO_KG
        and cleaned not in VOLUME_CONVERSION_TO_LITER
        and cleaned not in COUNT_CONVERSION_TO_PIECES
        and cleaned not in LENGTH_CONVERSION_TO_METER
        and cleaned not in PACKAGING_UNITS
    ):
        words = cleaned.split()
        if words and words[-1] in MASS_CONVERSION_TO_KG:
            cleaned = words[-1]
        elif words and words[-1] in VOLUME_CONVERSION_TO_LITER:
            cleaned = words[-1]
        elif words and words[-1] in COUNT_CONVERSION_TO_PIECES:
            cleaned = words[-1]
        elif words and words[-1] in LENGTH_CONVERSION_TO_METER:
            cleaned = words[-1]
        elif words and words[-1] in PACKAGING_UNITS:
            cleaned = words[-1]
    return cleaned or None


def is_mass_unit(unit: Optional[str]) -> bool:
    cleaned = clean_unit(unit)
    return bool(cleaned) and cleaned in MASS_CONVERSION_TO_KG


def is_volume_unit(unit: Optional[str]) -> bool:
    cleaned = clean_unit(unit)
    return bool(cleaned) and cleaned in VOLUME_CONVERSION_TO_LITER


def is_length_unit(unit: Optional[str]) -> bool:
    cleaned = clean_unit(unit)
    return bool(cleaned) and cleaned in LENGTH_CONVERSION_TO_METER


def is_count_unit(unit: Optional[str]) -> bool:
    cleaned = clean_unit(unit)
    return bool(cleaned) and cleaned in COUNT_CONVERSION_TO_PIECES


def is_packaging_unit(unit: Optional[str]) -> bool:
    cleaned = clean_unit(unit)
    return bool(cleaned) and cleaned in PACKAGING_UNITS


def to_pieces(value: Any, unit: Optional[str]) -> Optional[Decimal]:
    """Converts a count (pcs, dozen, pair, gross) to pieces. None if not a count unit."""
    if value is None:
        return None
    try:
        dec_val = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    cleaned = clean_unit(unit)
    multiplier = COUNT_CONVERSION_TO_PIECES.get(cleaned) if cleaned else None
    if multiplier is None:
        return None
    return dec_val * multiplier


def base_unit(unit: Optional[str]) -> Optional[tuple[Decimal, str]]:
    """(factor, base) for a quantity unit: how many base units one ``unit`` is.

    Mass resolves to kg, counts to pcs, length to m and volume to l. Anything
    else (a packaging unit, an unknown word) returns None: "1 bora" has no
    fixed size without a declared pack weight.
    """
    cleaned = clean_unit(unit)
    if not cleaned:
        return None
    if cleaned in MASS_CONVERSION_TO_KG:
        return MASS_CONVERSION_TO_KG[cleaned], "kg"
    if cleaned in VOLUME_CONVERSION_TO_LITER:
        return VOLUME_CONVERSION_TO_LITER[cleaned], "l"
    if cleaned in COUNT_CONVERSION_TO_PIECES:
        return COUNT_CONVERSION_TO_PIECES[cleaned], "pcs"
    if cleaned in LENGTH_CONVERSION_TO_METER:
        return LENGTH_CONVERSION_TO_METER[cleaned], "m"
    return None


def to_kg_from_text(value: Any) -> Optional[Decimal]:
    """"20 kg" / "20000 g" -> kilograms; None when it is not a mass."""
    parsed = parse_measurement(value)
    if parsed is None:
        return None
    return to_kg(parsed[0], parsed[1])


def resolve_to_base_unit(
    unit: Optional[str],
    attributes: Optional[dict[str, Any]] = None,
) -> Optional[tuple[Decimal, str, str]]:
    """(factor_to_base, base_unit_name, physical_dimension).

    Resolves:
    - Mass -> (factor, "kg", "mass")
    - Volume -> (factor, "l", "volume")
    - Count -> (factor, "pcs", "count")
    - Length -> (factor, "m", "length")
    - Packaging:
      - with pack_weight -> (weight_in_kg, "kg", "mass")
      - agricultural mandi default (bora, bag, katta) -> (50, "kg", "mass")
      - with pack_count -> (pack_count, "pcs", "count")
      - bare packaging -> (1, "unit", "packaging")
    """
    cleaned = clean_unit(unit)
    if not cleaned:
        return None

    if cleaned in MASS_CONVERSION_TO_KG:
        return MASS_CONVERSION_TO_KG[cleaned], "kg", "mass"

    if cleaned in VOLUME_CONVERSION_TO_LITER:
        return VOLUME_CONVERSION_TO_LITER[cleaned], "l", "volume"

    if cleaned in COUNT_CONVERSION_TO_PIECES:
        return COUNT_CONVERSION_TO_PIECES[cleaned], "pcs", "count"

    if cleaned in LENGTH_CONVERSION_TO_METER:
        return LENGTH_CONVERSION_TO_METER[cleaned], "m", "length"

    # Packaging unit resolution
    if cleaned in PACKAGING_UNITS:
        pw = None
        if attributes:
            pw = attributes.get("pack_weight_kg") or attributes.get("pack_weight")
            if pw is None:  # the spec catalogue's flat "20 kg"
                size = to_kg_from_text(attributes.get("pack_size"))
                pw = size
        if pw is None and cleaned in (
            "bora", "boras", "bori", "boris", "katta", "kattas",
            "bag", "bags", "sack", "sacks", "gunny bag", "gunny bags",
            "बोरी", "बोरा", "कट्टा", "कट्टे", "बैग", "थैला"
        ):
            pw = Decimal("50")

        if pw is not None:
            try:
                pw_dec = Decimal(str(pw))
                if pw_dec > 0:
                    return pw_dec, "kg", "mass"
            except (InvalidOperation, ValueError, TypeError):
                pass

        pc = None
        if attributes:
            pc = attributes.get("pack_count") or attributes.get("pack_pieces") or attributes.get("pcs_per_pack")
        if pc is not None:
            try:
                pc_dec = Decimal(str(pc))
                if pc_dec > 0:
                    return pc_dec, "pcs", "count"
            except (InvalidOperation, ValueError, TypeError):
                pass

        return Decimal("1"), cleaned, "packaging"

    return None


def normalize_quantity_to_base(
    value: Any,
    unit: Optional[str],
    attributes: Optional[dict[str, Any]] = None,
) -> Optional[tuple[Decimal, str, str]]:
    """Converts a quantity value and unit into (base_value, base_unit, dimension).

    e.g.
    - (10, "tonne") -> (Decimal("10000"), "kg", "mass")
    - (50, "quintals") -> (Decimal("5000"), "kg", "mass")
    - (2, "dozen") -> (Decimal("24"), "pcs", "count")
    - (5, "kl") -> (Decimal("5000"), "l", "volume")
    - (500, "ml") -> (Decimal("0.5"), "l", "volume")
    - (100, "bag", {"pack_weight_kg": 25}) -> (Decimal("2500"), "kg", "mass")
    """
    if value is None:
        return None
    try:
        val_d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if val_d <= 0:
        return None

    # Check total weight overrides in attributes first
    if attributes:
        if "total_weight_kg" in attributes and attributes["total_weight_kg"] is not None:
            try:
                tw_kg = Decimal(str(attributes["total_weight_kg"]))
                if tw_kg > 0:
                    return tw_kg, "kg", "mass"
            except (InvalidOperation, ValueError, TypeError):
                pass
        if "total_weight_tons" in attributes and attributes["total_weight_tons"] is not None:
            try:
                tw_t = Decimal(str(attributes["total_weight_tons"]))
                if tw_t > 0:
                    return tw_t * Decimal("1000"), "kg", "mass"
            except (InvalidOperation, ValueError, TypeError):
                pass

    resolved = resolve_to_base_unit(unit, attributes)
    if resolved is not None:
        factor, base_u, dim = resolved
        return val_d * factor, base_u, dim

    clean_u = clean_unit(unit) or "unit"
    return val_d, clean_u, "unknown"


def normalize_price_to_base(
    amount: Any,
    currency_code: Optional[str],
    per_unit: Optional[str],
    quantity_unit: Optional[str] = None,
    attributes: Optional[dict[str, Any]] = None,
    target_currency: Optional[str] = None,
) -> Optional[tuple[Decimal, str, str, str]]:
    """Restates price per base unit in target (or canonical) currency:
    (base_price_amount, currency, base_unit, dimension).

    e.g.
    - 28000 INR per tonne -> (Decimal("28"), "INR", "kg", "mass")
    - 24 USD per dozen -> (Decimal("2"), "USD", "pcs", "count")
    - 5000 INR per kl -> (Decimal("5"), "INR", "l", "volume")
    - 2500 INR per 50kg bag -> (Decimal("50"), "INR", "kg", "mass")
    """
    if amount is None:
        return None
    try:
        amt_d = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if amt_d <= 0:
        return None

    unit_to_use = per_unit or quantity_unit
    resolved = resolve_to_base_unit(unit_to_use, attributes)
    if resolved is not None:
        factor, base_u, dim = resolved
        price_per_base = amt_d / factor if factor > 0 else amt_d
    else:
        clean_u = clean_unit(unit_to_use) or "unit"
        price_per_base = amt_d
        base_u = clean_u
        dim = "unknown"

    from services import currency
    norm_ccy = currency.normalize_currency(currency_code) if currency_code else None
    if norm_ccy is None and currency_code:
        norm_ccy = str(currency_code).strip().upper()

    if norm_ccy and target_currency:
        target_norm = currency.normalize_currency(target_currency) or target_currency.strip().upper()
        if target_norm and target_norm != norm_ccy:
            converted = currency.convert(price_per_base, norm_ccy, target_norm)
            if converted is not None:
                price_per_base = converted
                norm_ccy = target_norm

    return price_per_base, norm_ccy or "", base_u, dim


def price_per_base_unit(
    amount: Any, per_unit: Optional[str]
) -> Optional[tuple[Decimal, str]]:
    """A unit price restated per base unit: 28,000/tonne -> (28, "kg").

    Returns None when the unit has no fixed base (see ``base_unit``).
    """
    if amount is None:
        return None
    try:
        dec_amt = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return None
    resolved = base_unit(per_unit)
    if resolved is None:
        return None
    factor, base = resolved
    if factor == 0:
        return None
    return dec_amt / factor, base


def to_kg(value: Any, unit: Optional[str]) -> Optional[Decimal]:
    """Converts any mass value to kilograms (kg). Returns None if not a mass unit."""
    if value is None or not unit:
        return None
    try:
        dec_val = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    multiplier = MASS_CONVERSION_TO_KG.get(clean_unit(unit) or "")
    if multiplier is None:
        return None
    return dec_val * multiplier


def to_tonnes(value: Any, unit: Optional[str]) -> Optional[Decimal]:
    """Converts any mass value to metric tonnes (MT). Returns None if not a mass unit."""
    kg = to_kg(value, unit)
    if kg is None:
        return None
    return kg / Decimal("1000")


def convert_mass(value: Any, from_unit: str, to_unit: str) -> Optional[Decimal]:
    """Converts between two mass units. Returns None if either is not a mass unit."""
    kg = to_kg(value, from_unit)
    if kg is None:
        return None
    target_mult = MASS_CONVERSION_TO_KG.get(clean_unit(to_unit) or "")
    if target_mult is None:
        return None
    return kg / target_mult


def convert(value: Any, from_unit: str, to_unit: str) -> Optional[Decimal]:
    """Universal unit converter across mass, count, and dimensional units."""
    if value is None or not from_unit or not to_unit:
        return None
    # 1. Mass conversion
    mass_res = convert_mass(value, from_unit, to_unit)
    if mass_res is not None:
        return mass_res

    # 2. Count conversion (e.g. dozen, gross, pairs to pcs)
    try:
        val_d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None

    if is_count_unit(from_unit) and is_count_unit(to_unit):
        p_from = to_pieces(val_d, from_unit)
        p_to_base = to_pieces(Decimal("1"), to_unit)
        if p_from is not None and p_to_base is not None and p_to_base > 0:
            return p_from / p_to_base

    # 3. SI dimension conversion (e.g. mm to cm, m to mm)
    si_from = to_base_si(val_d, from_unit)
    si_to = to_base_si(Decimal("1"), to_unit)
    if si_from and si_to and si_from[2] == si_to[2] and si_to[0] > 0:
        return si_from[0] / si_to[0]

    return None


def compute_effective_mass_kg(
    quantity: Decimal,
    unit: Optional[str],
    pack_weight_val: Optional[Decimal],
    pack_weight_unit: Optional[str],
) -> Optional[Decimal]:
    """Computes total mass in kg.

    If unit is already mass (e.g. 30000 tons), converts directly to kg.
    If unit is packaging (e.g. 1000 boras) and pack_weight is given (e.g. 55 kg),
    computes count * pack_weight.
    """
    if is_mass_unit(unit):
        return to_kg(quantity, unit)

    if pack_weight_val is not None and pack_weight_unit and is_mass_unit(pack_weight_unit):
        single_pack_kg = to_kg(pack_weight_val, pack_weight_unit)
        if single_pack_kg is not None:
            return quantity * single_pack_kg

    return None


_MEASUREMENT_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*([a-zA-Zμµ°/]+(?:\^[0-9]+)?)\s*$",
    re.IGNORECASE,
)


def parse_measurement(val_or_str: Any) -> Optional[tuple[Decimal, str]]:
    """Parse any measurement expression, whether string ('20mm', '2 cms'), dict, or tuple."""
    if val_or_str is None:
        return None
    if isinstance(val_or_str, dict):
        v = val_or_str.get("value")
        u = val_or_str.get("unit")
        if v is not None and u:
            try:
                return Decimal(str(v)), str(u).strip().lower()
            except (InvalidOperation, ValueError, TypeError):
                return None
        return None
    if isinstance(val_or_str, (int, float, Decimal)):
        return None
    s = str(val_or_str).strip()
    match = _MEASUREMENT_RE.match(s)
    if match:
        try:
            return Decimal(match.group(1)), match.group(2).lower()
        except (InvalidOperation, ValueError, TypeError):
            return None
    return None


def to_base_si(value: Decimal, unit: str) -> Optional[tuple[Decimal, str, str]]:
    """Converts a value and unit into (si_value, si_unit, physical_dimension).

    Returns None if the unit is unrecognized.
    """
    cleaned = clean_unit(unit) or ""

    # 1. Length [L] -> meter (m)
    if cleaned in LENGTH_CONVERSION_TO_METER:
        return value * LENGTH_CONVERSION_TO_METER[cleaned], "m", "length"

    # 2. Mass [M] -> kilogram (kg)
    if cleaned in MASS_CONVERSION_TO_KG:
        return value * MASS_CONVERSION_TO_KG[cleaned], "kg", "mass"

    # 3. Volume [V] -> liter (L)
    if cleaned in VOLUME_CONVERSION_TO_LITER:
        return value * VOLUME_CONVERSION_TO_LITER[cleaned], "l", "volume"

    # 4. Count [C] -> piece (pcs)
    if cleaned in COUNT_CONVERSION_TO_PIECES:
        return value * COUNT_CONVERSION_TO_PIECES[cleaned], "pcs", "count"

    # 5. Power [P] -> watt (W)
    if cleaned in POWER_CONVERSION_TO_WATT:
        return value * POWER_CONVERSION_TO_WATT[cleaned], "w", "power"

    # 6. Voltage [V] -> volt (V)
    if cleaned in VOLTAGE_CONVERSION_TO_VOLT:
        return value * VOLTAGE_CONVERSION_TO_VOLT[cleaned], "v", "voltage"

    # 7. Capacity [Ah] -> ampere-hour (Ah)
    if cleaned in CAPACITY_CONVERSION_TO_AH:
        return value * CAPACITY_CONVERSION_TO_AH[cleaned], "ah", "capacity"

    return None


def compare_measurements(
    val1: Any,
    unit1: Optional[str] = None,
    val2: Any = None,
    unit2: Optional[str] = None,
) -> Optional[float]:
    """Compare two measurements across arbitrary units.

    e.g. compare_measurements('2cm', None, '20mm', None) -> 1.0 (Exact 100% Match!)
    e.g. compare_measurements(2, 'cm', 20, 'mm') -> 1.0 (Exact 100% Match!)
    """
    m1 = (
        (Decimal(str(val1)), str(unit1).lower())
        if unit1 is not None and val1 is not None
        else parse_measurement(val1)
    )
    m2 = (
        (Decimal(str(val2)), str(unit2).lower())
        if unit2 is not None and val2 is not None
        else parse_measurement(val2)
    )

    if m1 is None or m2 is None:
        return None

    si1 = to_base_si(m1[0], m1[1])
    si2 = to_base_si(m2[0], m2[1])

    if si1 is None or si2 is None:
        return None

    val_si1, unit_si1, dim1 = si1
    val_si2, unit_si2, dim2 = si2

    if dim1 != dim2:
        return 0.0

    if val_si1 == val_si2:
        return 1.0

    largest = max(abs(val_si1), abs(val_si2))
    if largest == 0:
        return 1.0
    return max(0.0, 1.0 - float(abs(val_si1 - val_si2) / largest))


"""Intelligent unit converter for mass, weight, and agricultural packaging.

Provides exact conversion factors for metric and imperial mass units:
- Metric Tonnes / Tons / MT (1,000 kg)
- Quintals / qtl (100 kg)
- Kilograms / kg / kilos (1 kg)
- Grams / g / gm (0.001 kg)
- Pounds / lbs (0.453592 kg)

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
    # Quintals (standard in Indian and agricultural mandis: 1 quintal = 100 kg)
    "quintal": Decimal("100"),
    "quintals": Decimal("100"),
    "qtl": Decimal("100"),
    # Kilograms
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
}

# Standard conversion multiplier to Liters (L)
VOLUME_CONVERSION_TO_LITER: dict[str, Decimal] = {
    "l": Decimal("1"),
    "liter": Decimal("1"),
    "liters": Decimal("1"),
    "litre": Decimal("1"),
    "litres": Decimal("1"),
    "ml": Decimal("0.001"),
    "milliliter": Decimal("0.001"),
    "milliliters": Decimal("0.001"),
    "millilitre": Decimal("0.001"),
    "millilitres": Decimal("0.001"),
    "gallon": Decimal("3.78541"),
    "gallons": Decimal("3.78541"),
    "gal": Decimal("3.78541"),
    "m3": Decimal("1000"),
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
    }
)


def is_mass_unit(unit: Optional[str]) -> bool:
    if not unit:
        return False
    return unit.strip().lower() in MASS_CONVERSION_TO_KG


def is_packaging_unit(unit: Optional[str]) -> bool:
    if not unit:
        return False
    return unit.strip().lower() in PACKAGING_UNITS


def to_kg(value: Optional[Decimal], unit: Optional[str]) -> Optional[Decimal]:
    """Converts any mass value to kilograms (kg). Returns None if not a mass unit."""
    if value is None or not unit:
        return None
    multiplier = MASS_CONVERSION_TO_KG.get(unit.strip().lower())
    if multiplier is None:
        return None
    return value * multiplier


def to_tonnes(value: Optional[Decimal], unit: Optional[str]) -> Optional[Decimal]:
    """Converts any mass value to metric tonnes (MT). Returns None if not a mass unit."""
    kg = to_kg(value, unit)
    if kg is None:
        return None
    return kg / Decimal("1000")


def convert_mass(value: Decimal, from_unit: str, to_unit: str) -> Optional[Decimal]:
    """Converts between two mass units. Returns None if either is not a mass unit."""
    kg = to_kg(value, from_unit)
    if kg is None:
        return None
    target_mult = MASS_CONVERSION_TO_KG.get(to_unit.strip().lower())
    if target_mult is None:
        return None
    return kg / target_mult


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
    cleaned = unit.strip().lower()

    # 1. Length [L] -> meter (m)
    if cleaned in LENGTH_CONVERSION_TO_METER:
        return value * LENGTH_CONVERSION_TO_METER[cleaned], "m", "length"

    # 2. Mass [M] -> kilogram (kg)
    if cleaned in MASS_CONVERSION_TO_KG:
        return value * MASS_CONVERSION_TO_KG[cleaned], "kg", "mass"

    # 3. Volume [V] -> liter (L)
    if cleaned in VOLUME_CONVERSION_TO_LITER:
        return value * VOLUME_CONVERSION_TO_LITER[cleaned], "l", "volume"

    # 4. Power [P] -> watt (W)
    if cleaned in POWER_CONVERSION_TO_WATT:
        return value * POWER_CONVERSION_TO_WATT[cleaned], "w", "power"

    # 5. Voltage [V] -> volt (V)
    if cleaned in VOLTAGE_CONVERSION_TO_VOLT:
        return value * VOLTAGE_CONVERSION_TO_VOLT[cleaned], "v", "voltage"

    # 6. Capacity [Ah] -> ampere-hour (Ah)
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


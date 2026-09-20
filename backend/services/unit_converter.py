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

from decimal import Decimal
from typing import Optional

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

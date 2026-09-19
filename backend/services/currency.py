"""Currency conversion for price comparison.

The rates below are **static and illustrative**. They exist so that a buyer
quoting EUR can be compared against a seller quoting CNY at all; they are not
accurate enough to quote to a counterparty, and should be replaced by a rates
feed (cached, with an as-of timestamp stored alongside every converted figure)
before anyone trades on them.

Conversion is deliberately explicit: an unknown currency returns None rather
than being assumed to be the base, because silently treating VND as USD would
be off by four orders of magnitude.
"""

from decimal import Decimal
from typing import Optional

BASE = "USD"

# Units of the currency per 1 USD.
RATES: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("0.92"),
    "GBP": Decimal("0.79"),
    "INR": Decimal("83"),
    "CNY": Decimal("7.2"),
    "AED": Decimal("3.67"),
    "SAR": Decimal("3.75"),
    "TRY": Decimal("34"),
    "VND": Decimal("25000"),
    "THB": Decimal("36"),
    "IDR": Decimal("16000"),
    "MYR": Decimal("4.7"),
    "SGD": Decimal("1.35"),
    "PLN": Decimal("4.0"),
    "CAD": Decimal("1.36"),
    "MXN": Decimal("17"),
    "BRL": Decimal("5.4"),
    "EGP": Decimal("48"),
    "KES": Decimal("129"),
    "ZAR": Decimal("18.5"),
    "AUD": Decimal("1.5"),
}


def supported(code: Optional[str]) -> bool:
    return bool(code) and code.upper() in RATES


def convert(amount: Decimal, source: str, target: str) -> Optional[Decimal]:
    """Convert between two known currencies, or None if either is unknown."""
    source, target = (source or "").upper(), (target or "").upper()
    if source == target:
        return amount
    if source not in RATES or target not in RATES:
        return None
    return amount / RATES[source] * RATES[target]

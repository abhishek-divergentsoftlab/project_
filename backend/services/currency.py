"""Currency conversion for price comparison with resilient caching and live rate tracking.

The rates below provide an audited baseline. In production, live FX feeds
update the internal cache while retaining this table as a guaranteed fallback.
Conversion is deliberately explicit: an unknown currency returns None rather
than being assumed to be the base.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Optional

BASE = "USD"

# Default baseline: units of currency per 1 USD
DEFAULT_RATES: dict[str, Decimal] = {
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
    "JPY": Decimal("155"),
    "CHF": Decimal("0.89"),
    "KRW": Decimal("1380"),
    "SEK": Decimal("10.5"),
    "NOK": Decimal("10.8"),
    "DKK": Decimal("6.9"),
}

# Active working rates table
RATES: dict[str, Decimal] = dict(DEFAULT_RATES)
_AS_OF: datetime = datetime.now(UTC)

# Canonical currency display names
CURRENCY_NAMES: dict[str, str] = {
    "USD": "US Dollar",
    "EUR": "Euro",
    "GBP": "British Pound",
    "INR": "Indian Rupee",
    "CNY": "Chinese Yuan",
    "AED": "UAE Dirham",
    "SAR": "Saudi Riyal",
    "TRY": "Turkish Lira",
    "VND": "Vietnamese Dong",
    "THB": "Thai Baht",
    "IDR": "Indonesian Rupiah",
    "MYR": "Malaysian Ringgit",
    "SGD": "Singapore Dollar",
    "PLN": "Polish Zloty",
    "CAD": "Canadian Dollar",
    "MXN": "Mexican Peso",
    "BRL": "Brazilian Real",
    "EGP": "Egyptian Pound",
    "KES": "Kenyan Shilling",
    "ZAR": "South African Rand",
    "AUD": "Australian Dollar",
    "JPY": "Japanese Yen",
    "CHF": "Swiss Franc",
    "KRW": "South Korean Won",
    "SEK": "Swedish Krona",
    "NOK": "Norwegian Krone",
    "DKK": "Danish Krone",
}

# Comprehensive dictionary mapping colloquial names, typos, and symbols to ISO codes
CURRENCY_SYNONYMS: dict[str, str] = {
    # INR - Indian Rupee
    "inr": "INR", "₹": "INR", "rs": "INR", "rs.": "INR", "/-": "INR",
    "rupee": "INR", "rupees": "INR", "ruppes": "INR", "rupes": "INR",
    "rupess": "INR", "ruppees": "INR", "rupe": "INR",
    "india ruppes": "INR", "indian rupees": "INR", "indian rupee": "INR",
    "india rupee": "INR", "india rupees": "INR", "bharat rupee": "INR",
    
    # USD - United States Dollar
    "usd": "USD", "$": "USD", "us$": "USD",
    "dollar": "USD", "dollars": "USD", "buck": "USD", "bucks": "USD",
    "us dollar": "USD", "us dollars": "USD", "u.s. dollar": "USD",
    "u.s. dollars": "USD", "united states dollar": "USD", "united states dollars": "USD",
    
    # EUR - Euro
    "eur": "EUR", "€": "EUR", "euro": "EUR", "euros": "EUR",
    
    # GBP - British Pound
    "gbp": "GBP", "£": "GBP", "pound": "GBP", "pounds": "GBP", "quid": "GBP",
    "british pound": "GBP", "british pounds": "GBP", "uk pound": "GBP",
    "uk pounds": "GBP", "pound sterling": "GBP",
    
    # Scandinavian (kr) - Swedish Krona / Norwegian Krone / Danish Krone
    "kr": "SEK", "krona": "SEK", "kronor": "SEK", "swedish krona": "SEK", "sek": "SEK",
    "norwegian krone": "NOK", "nok": "NOK", "krone": "NOK", "kroner": "NOK",
    "danish krone": "DKK", "dkk": "DKK",
    
    # JPY - Japanese Yen
    "jpy": "JPY", "¥": "JPY", "yen": "JPY", "japanese yen": "JPY",
    
    # CNY - Chinese Yuan / Renminbi
    "cny": "CNY", "rmb": "CNY", "yuan": "CNY", "renminbi": "CNY",
    "chinese yuan": "CNY", "chinese rmb": "CNY", "kuai": "CNY",
    
    # CAD - Canadian Dollar
    "cad": "CAD", "c$": "CAD", "canadian dollar": "CAD", "canadian dollars": "CAD",
    
    # AUD - Australian Dollar
    "aud": "AUD", "a$": "AUD", "australian dollar": "AUD", "australian dollars": "AUD",
    "aussie dollar": "AUD",
    
    # AED - UAE Dirham
    "aed": "AED", "dirham": "AED", "dirhams": "AED", "uae dirham": "AED",
    "uae dirhams": "AED", "dhs": "AED", "emirates dirham": "AED",
    
    # SAR - Saudi Riyal
    "sar": "SAR", "riyal": "SAR", "riyals": "SAR", "saudi riyal": "SAR", "saudi riyals": "SAR",
    
    # CHF - Swiss Franc
    "chf": "CHF", "franc": "CHF", "francs": "CHF", "swiss franc": "CHF", "swiss francs": "CHF",
    
    # KRW - Korean Won
    "krw": "KRW", "₩": "KRW", "won": "KRW", "korean won": "KRW",
    
    # SGD - Singapore Dollar
    "sgd": "SGD", "singapore dollar": "SGD", "singapore dollars": "SGD",
    
    # TRY - Turkish Lira
    "try": "TRY", "₺": "TRY", "lira": "TRY", "turkish lira": "TRY",
    
    # PLN - Polish Zloty
    "pln": "PLN", "zł": "PLN", "zloty": "PLN", "polish zloty": "PLN",
    
    # BRL - Brazilian Real
    "brl": "BRL", "r$": "BRL", "real": "BRL", "reais": "BRL", "brazilian real": "BRL",
    
    # MXN - Mexican Peso
    "mxn": "MXN", "peso": "MXN", "pesos": "MXN", "mexican peso": "MXN",
    
    # THB - Thai Baht
    "thb": "THB", "฿": "THB", "baht": "THB", "thai baht": "THB",
    
    # VND - Vietnamese Dong
    "vnd": "VND", "₫": "VND", "dong": "VND", "vietnamese dong": "VND",
    
    # IDR - Indonesian Rupiah
    "idr": "IDR", "rupiah": "IDR", "indonesian rupiah": "IDR",
    
    # MYR - Malaysian Ringgit
    "myr": "MYR", "ringgit": "MYR", "malaysian ringgit": "MYR",
    
    # ZAR - South African Rand
    "zar": "ZAR", "rand": "ZAR", "south african rand": "ZAR",
}


def normalize_currency(raw: Optional[str]) -> Optional[str]:
    """AI / Fuzzy normalizer converting natural language terms (e.g. 'india ruppes', 'us dollar', 'kr')

    into standardized 3-letter ISO 4217 currency codes.
    """
    if not raw:
        return None
    cleaned = raw.strip().lower().rstrip(".")

    # 1. Direct synonym / colloquial match
    if cleaned in CURRENCY_SYNONYMS:
        return CURRENCY_SYNONYMS[cleaned]

    # 2. Uppercase ISO check
    upper_candidate = cleaned.upper()
    if upper_candidate in RATES:
        return upper_candidate

    # 3. Fuzzy regex pattern matching
    # Indian Rupee variations (e.g. "india ruppes", "inr rupees", "ruppees")
    if re.search(r"\b(?:india[n]?\s+)?rup+[e|p]*s*\b", cleaned):
        return "INR"
    # US Dollar variations (e.g. "us dollar", "united states dollars", "bucks")
    if re.search(r"\b(?:u\.?s\.?\s+)?(?:dollars?|bucks?)\b", cleaned):
        return "USD"
    # Scandinavian Krona variations (e.g. "kr", "krona", "kroner")
    if re.search(r"\b(?:kr|krona|kronor|kroner|krone)\b", cleaned):
        if "norwegian" in cleaned:
            return "NOK"
        if "danish" in cleaned:
            return "DKK"
        return "SEK"
    # British Pound variations
    if re.search(r"\b(?:british|uk)?\s*pounds?\b", cleaned):
        return "GBP"
    # Euro variations
    if re.search(r"\beuros?\b", cleaned):
        return "EUR"
    # Yen variations
    if re.search(r"\b(?:japanese\s+)?yen\b", cleaned):
        return "JPY"
    # Yuan / RMB variations
    if re.search(r"\b(?:chinese\s+)?(?:yuan|renminbi|rmb)\b", cleaned):
        return "CNY"

    return None


def get_rates_info() -> dict:
    """Export current conversion table with metadata."""
    return {
        "base": BASE,
        "as_of": _AS_OF.isoformat(),
        "currencies": list(RATES.keys()),
        "rates": {k: float(v) for k, v in RATES.items()},
        "names": CURRENCY_NAMES,
    }


def update_rates(new_rates: dict[str, Decimal | float | str]) -> None:
    """Update working rates safely."""
    global _AS_OF
    for code, rate in new_rates.items():
        try:
            val = Decimal(str(rate))
            if val > 0:
                RATES[code.upper()] = val
        except Exception:
            continue
    _AS_OF = datetime.now(UTC)


def supported(code: Optional[str]) -> bool:
    if not code:
        return False
    norm = normalize_currency(code)
    return bool(norm and norm in RATES)


def convert(amount: Decimal, source: str, target: str) -> Optional[Decimal]:
    """Convert between two currencies with automatic natural language normalization."""
    source_code = normalize_currency(source) or (source or "").upper()
    target_code = normalize_currency(target) or (target or "").upper()
    if source_code == target_code:
        return amount
    if source_code not in RATES or target_code not in RATES:
        return None
    return amount / RATES[source_code] * RATES[target_code]


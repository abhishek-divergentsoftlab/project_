"""Known cities, shared by the seeder and the query parser.

Coordinates matter: location scoring prefers real distance over string equality
whenever both sides have them, so a city recognised here is worth far more to
matching than a free-text string.

Each city also carries its country's usual trading currency, which is what makes
a multi-country corpus generate believable prices.
"""

from decimal import Decimal
from typing import NamedTuple, Optional


class City(NamedTuple):
    name: str
    region: str
    country: str
    currency: str
    latitude: float
    longitude: float


def _c(name, region, country, currency, lat, lng) -> City:
    return City(name, region, country, currency, lat, lng)


CITIES: dict[str, City] = {
    # --- India ---------------------------------------------------------------
    "indore": _c("Indore", "Madhya Pradesh", "India", "INR", 22.7196, 75.8577),
    "bhopal": _c("Bhopal", "Madhya Pradesh", "India", "INR", 23.2599, 77.4126),
    "pune": _c("Pune", "Maharashtra", "India", "INR", 18.5204, 73.8567),
    "mumbai": _c("Mumbai", "Maharashtra", "India", "INR", 19.0760, 72.8777),
    "nashik": _c("Nashik", "Maharashtra", "India", "INR", 19.9975, 73.7898),
    "aurangabad": _c("Aurangabad", "Maharashtra", "India", "INR", 19.8762, 75.3433),
    "nagpur": _c("Nagpur", "Maharashtra", "India", "INR", 21.1458, 79.0882),
    "ahmedabad": _c("Ahmedabad", "Gujarat", "India", "INR", 23.0225, 72.5714),
    "surat": _c("Surat", "Gujarat", "India", "INR", 21.1702, 72.8311),
    "vadodara": _c("Vadodara", "Gujarat", "India", "INR", 22.3072, 73.1812),
    "rajkot": _c("Rajkot", "Gujarat", "India", "INR", 22.3039, 70.8022),
    "jaipur": _c("Jaipur", "Rajasthan", "India", "INR", 26.9124, 75.7873),
    "ludhiana": _c("Ludhiana", "Punjab", "India", "INR", 30.9010, 75.8573),
    "delhi": _c("Delhi", "Delhi", "India", "INR", 28.6139, 77.2090),
    "noida": _c("Noida", "Uttar Pradesh", "India", "INR", 28.5355, 77.3910),
    "kanpur": _c("Kanpur", "Uttar Pradesh", "India", "INR", 26.4499, 80.3319),
    "lucknow": _c("Lucknow", "Uttar Pradesh", "India", "INR", 26.8467, 80.9462),
    "kolkata": _c("Kolkata", "West Bengal", "India", "INR", 22.5726, 88.3639),
    "chennai": _c("Chennai", "Tamil Nadu", "India", "INR", 13.0827, 80.2707),
    "coimbatore": _c("Coimbatore", "Tamil Nadu", "India", "INR", 11.0168, 76.9558),
    "tiruppur": _c("Tiruppur", "Tamil Nadu", "India", "INR", 11.1085, 77.3411),
    "bengaluru": _c("Bengaluru", "Karnataka", "India", "INR", 12.9716, 77.5946),
    "bangalore": _c("Bengaluru", "Karnataka", "India", "INR", 12.9716, 77.5946),
    "hyderabad": _c("Hyderabad", "Telangana", "India", "INR", 17.3850, 78.4867),
    "kochi": _c("Kochi", "Kerala", "India", "INR", 9.9312, 76.2673),

    # --- China ---------------------------------------------------------------
    "shenzhen": _c("Shenzhen", "Guangdong", "China", "CNY", 22.5431, 114.0579),
    "guangzhou": _c("Guangzhou", "Guangdong", "China", "CNY", 23.1291, 113.2644),
    "dongguan": _c("Dongguan", "Guangdong", "China", "CNY", 23.0207, 113.7518),
    "shanghai": _c("Shanghai", "Shanghai", "China", "CNY", 31.2304, 121.4737),
    "ningbo": _c("Ningbo", "Zhejiang", "China", "CNY", 29.8683, 121.5440),
    "yiwu": _c("Yiwu", "Zhejiang", "China", "CNY", 29.3069, 120.0759),
    "qingdao": _c("Qingdao", "Shandong", "China", "CNY", 36.0671, 120.3826),

    # --- Vietnam / SE Asia ---------------------------------------------------
    "ho chi minh city": _c("Ho Chi Minh City", "Ho Chi Minh", "Vietnam", "VND", 10.8231, 106.6297),
    "hanoi": _c("Hanoi", "Hanoi", "Vietnam", "VND", 21.0278, 105.8342),
    "bangkok": _c("Bangkok", "Bangkok", "Thailand", "THB", 13.7563, 100.5018),
    "jakarta": _c("Jakarta", "Jakarta", "Indonesia", "IDR", -6.2088, 106.8456),
    "penang": _c("Penang", "Penang", "Malaysia", "MYR", 5.4164, 100.3327),
    "singapore": _c("Singapore", "Singapore", "Singapore", "SGD", 1.3521, 103.8198),

    # --- Middle East ---------------------------------------------------------
    "dubai": _c("Dubai", "Dubai", "United Arab Emirates", "AED", 25.2048, 55.2708),
    "sharjah": _c("Sharjah", "Sharjah", "United Arab Emirates", "AED", 25.3463, 55.4209),
    "riyadh": _c("Riyadh", "Riyadh", "Saudi Arabia", "SAR", 24.7136, 46.6753),
    "istanbul": _c("Istanbul", "Istanbul", "Turkey", "TRY", 41.0082, 28.9784),
    "izmir": _c("Izmir", "Izmir", "Turkey", "TRY", 38.4237, 27.1428),

    # --- Europe --------------------------------------------------------------
    "hamburg": _c("Hamburg", "Hamburg", "Germany", "EUR", 53.5511, 9.9937),
    "munich": _c("Munich", "Bavaria", "Germany", "EUR", 48.1351, 11.5820),
    "rotterdam": _c("Rotterdam", "South Holland", "Netherlands", "EUR", 51.9244, 4.4777),
    "milan": _c("Milan", "Lombardy", "Italy", "EUR", 45.4642, 9.1900),
    "barcelona": _c("Barcelona", "Catalonia", "Spain", "EUR", 41.3874, 2.1686),
    "lodz": _c("Lodz", "Lodz", "Poland", "PLN", 51.7592, 19.4560),
    "warsaw": _c("Warsaw", "Masovia", "Poland", "PLN", 52.2297, 21.0122),
    "manchester": _c("Manchester", "England", "United Kingdom", "GBP", 53.4808, -2.2426),
    "london": _c("London", "England", "United Kingdom", "GBP", 51.5074, -0.1278),

    # --- Americas ------------------------------------------------------------
    "chicago": _c("Chicago", "Illinois", "United States", "USD", 41.8781, -87.6298),
    "los angeles": _c("Los Angeles", "California", "United States", "USD", 34.0522, -118.2437),
    "newark": _c("Newark", "New Jersey", "United States", "USD", 40.7357, -74.1724),
    "houston": _c("Houston", "Texas", "United States", "USD", 29.7604, -95.3698),
    "toronto": _c("Toronto", "Ontario", "Canada", "CAD", 43.6532, -79.3832),
    "monterrey": _c("Monterrey", "Nuevo Leon", "Mexico", "MXN", 25.6866, -100.3161),
    "sao paulo": _c("Sao Paulo", "Sao Paulo", "Brazil", "BRL", -23.5558, -46.6396),

    # --- Africa / Oceania ----------------------------------------------------
    "cairo": _c("Cairo", "Cairo", "Egypt", "EGP", 30.0444, 31.2357),
    "nairobi": _c("Nairobi", "Nairobi", "Kenya", "KES", -1.2864, 36.8172),
    "johannesburg": _c("Johannesburg", "Gauteng", "South Africa", "ZAR", -26.2041, 28.0473),
    "sydney": _c("Sydney", "New South Wales", "Australia", "AUD", -33.8688, 151.2093),
}

# Longest first, so "ho chi minh city" wins over any shorter substring.
_CITY_KEYS = sorted(CITIES, key=len, reverse=True)


def find_city(text: str) -> Optional[City]:
    """First known city mentioned anywhere in the text."""
    lowered = f" {text.lower()} "
    for key in _CITY_KEYS:
        if f" {key} " in lowered or f" {key}." in lowered or f" {key}," in lowered:
            return CITIES[key]
    return None


def as_decimal(value: float) -> Decimal:
    return Decimal(str(value))


# Real international dialling codes, so a seeded phone number looks like a phone
# number. The seed used to emit "+5 742229893", and "+5" is not a country.
DIALLING_CODES: dict[str, str] = {
    "Australia": "61",
    "Brazil": "55",
    "Canada": "1",
    "China": "86",
    "Egypt": "20",
    "Germany": "49",
    "India": "91",
    "Indonesia": "62",
    "Italy": "39",
    "Kenya": "254",
    "Malaysia": "60",
    "Mexico": "52",
    "Netherlands": "31",
    "Poland": "48",
    "Saudi Arabia": "966",
    "Singapore": "65",
    "South Africa": "27",
    "Spain": "34",
    "Thailand": "66",
    "Turkey": "90",
    "United Arab Emirates": "971",
    "United Kingdom": "44",
    "United States": "1",
    "Vietnam": "84",
}


def dialling_code(country: Optional[str]) -> str:
    """Country calling code, defaulting to India's for anywhere unlisted."""
    return DIALLING_CODES.get(country or "", "91")

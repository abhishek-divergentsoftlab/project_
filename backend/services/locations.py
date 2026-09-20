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
    "amritsar": _c("Amritsar", "Punjab", "India", "INR", 31.6340, 74.8723),
    "jalandhar": _c("Jalandhar", "Punjab", "India", "INR", 31.3260, 75.5762),
    "mohali": _c("Mohali", "Punjab", "India", "INR", 30.7046, 76.7179),
    "chandigarh": _c("Chandigarh", "Chandigarh", "India", "INR", 30.7333, 76.7794),
    "gurgaon": _c("Gurgaon", "Haryana", "India", "INR", 28.4595, 77.0266),
    "gurugram": _c("Gurugram", "Haryana", "India", "INR", 28.4595, 77.0266),
    "faridabad": _c("Faridabad", "Haryana", "India", "INR", 28.4089, 77.3178),
    "panipat": _c("Panipat", "Haryana", "India", "INR", 29.3909, 76.9635),
    "ambala": _c("Ambala", "Haryana", "India", "INR", 30.3782, 76.7767),
    "delhi": _c("Delhi", "Delhi", "India", "INR", 28.6139, 77.2090),
    "noida": _c("Noida", "Uttar Pradesh", "India", "INR", 28.5355, 77.3910),
    "ghaziabad": _c("Ghaziabad", "Uttar Pradesh", "India", "INR", 28.6692, 77.4538),
    "kanpur": _c("Kanpur", "Uttar Pradesh", "India", "INR", 26.4499, 80.3319),
    "lucknow": _c("Lucknow", "Uttar Pradesh", "India", "INR", 26.8467, 80.9462),
    "kolkata": _c("Kolkata", "West Bengal", "India", "INR", 22.5726, 88.3639),
    "chennai": _c("Chennai", "Tamil Nadu", "India", "INR", 13.0827, 80.2707),
    "coimbatore": _c("Coimbatore", "Tamil Nadu", "India", "INR", 11.0168, 76.9558),
    "tiruppur": _c("Tiruppur", "Tamil Nadu", "India", "INR", 11.1085, 77.3411),
    "bengaluru": _c("Bengaluru", "Karnataka", "India", "INR", 12.9716, 77.5946),
    "bangalore": _c("Bengaluru", "Karnataka", "India", "INR", 12.9716, 77.5946),
    "hyderabad": _c("Hyderabad", "Telangana", "India", "INR", 17.3850, 78.4867),
    "visakhapatnam": _c("Visakhapatnam", "Andhra Pradesh", "India", "INR", 17.6868, 83.2185),
    "vizag": _c("Visakhapatnam", "Andhra Pradesh", "India", "INR", 17.6868, 83.2185),
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

class Location(NamedTuple):
    name: str
    city: Optional[str]
    state: Optional[str]
    country: str
    currency: str
    latitude: float
    longitude: float
    is_city: bool = False
    is_state: bool = False
    is_country: bool = False
    matched_key: str = ""


def _loc(
    name: str,
    city: Optional[str],
    state: Optional[str],
    country: str,
    currency: str,
    lat: float,
    lng: float,
    is_city: bool = False,
    is_state: bool = False,
    is_country: bool = False,
    matched_key: str = "",
) -> Location:
    return Location(
        name=name,
        city=city,
        state=state,
        country=country,
        currency=currency,
        latitude=lat,
        longitude=lng,
        is_city=is_city,
        is_state=is_state,
        is_country=is_country,
        matched_key=matched_key,
    )


# Indian states, territories, nationwide markets, and countries.
REGIONS: dict[str, Location] = {
    # --- Indian States & Territories -----------------------------------------
    "haryana": _loc("Haryana", None, "Haryana", "India", "INR", 29.0588, 76.0856, is_state=True),
    "hariyana": _loc("Haryana", None, "Haryana", "India", "INR", 29.0588, 76.0856, is_state=True),
    "haryanvi": _loc("Haryana", None, "Haryana", "India", "INR", 29.0588, 76.0856, is_state=True),
    "punjab": _loc("Punjab", None, "Punjab", "India", "INR", 31.1471, 75.3412, is_state=True),
    "panjab": _loc("Punjab", None, "Punjab", "India", "INR", 31.1471, 75.3412, is_state=True),
    "maharashtra": _loc("Maharashtra", None, "Maharashtra", "India", "INR", 19.7515, 75.7139, is_state=True),
    "gujarat": _loc("Gujarat", None, "Gujarat", "India", "INR", 22.2587, 71.1924, is_state=True),
    "gujrat": _loc("Gujarat", None, "Gujarat", "India", "INR", 22.2587, 71.1924, is_state=True),
    "rajasthan": _loc("Rajasthan", None, "Rajasthan", "India", "INR", 27.0238, 74.2179, is_state=True),
    "madhya pradesh": _loc("Madhya Pradesh", None, "Madhya Pradesh", "India", "INR", 22.9734, 78.6569, is_state=True),
    "in mp": _loc("Madhya Pradesh", None, "Madhya Pradesh", "India", "INR", 22.9734, 78.6569, is_state=True),
    "uttar pradesh": _loc("Uttar Pradesh", None, "Uttar Pradesh", "India", "INR", 26.8467, 80.9462, is_state=True),
    "in up": _loc("Uttar Pradesh", None, "Uttar Pradesh", "India", "INR", 26.8467, 80.9462, is_state=True),
    "karnataka": _loc("Karnataka", None, "Karnataka", "India", "INR", 15.3173, 75.7139, is_state=True),
    "tamil nadu": _loc("Tamil Nadu", None, "Tamil Nadu", "India", "INR", 11.1271, 78.6569, is_state=True),
    "tamilnadu": _loc("Tamil Nadu", None, "Tamil Nadu", "India", "INR", 11.1271, 78.6569, is_state=True),
    "telangana": _loc("Telangana", None, "Telangana", "India", "INR", 18.1124, 79.0193, is_state=True),
    "andhra pradesh": _loc("Andhra Pradesh", None, "Andhra Pradesh", "India", "INR", 15.9129, 79.7400, is_state=True),
    "andhra": _loc("Andhra Pradesh", None, "Andhra Pradesh", "India", "INR", 15.9129, 79.7400, is_state=True),
    "west bengal": _loc("West Bengal", None, "West Bengal", "India", "INR", 22.9868, 87.8550, is_state=True),
    "kerala": _loc("Kerala", None, "Kerala", "India", "INR", 10.8505, 76.2711, is_state=True),
    "bihar": _loc("Bihar", None, "Bihar", "India", "INR", 25.0961, 85.3131, is_state=True),
    "odisha": _loc("Odisha", None, "Odisha", "India", "INR", 20.9517, 85.0985, is_state=True),
    "orissa": _loc("Odisha", None, "Odisha", "India", "INR", 20.9517, 85.0985, is_state=True),
    "assam": _loc("Assam", None, "Assam", "India", "INR", 26.2006, 92.9376, is_state=True),
    "goa": _loc("Goa", None, "Goa", "India", "INR", 15.2993, 74.1240, is_state=True),
    "himachal pradesh": _loc("Himachal Pradesh", None, "Himachal Pradesh", "India", "INR", 31.1048, 77.1734, is_state=True),
    "himachal": _loc("Himachal Pradesh", None, "Himachal Pradesh", "India", "INR", 31.1048, 77.1734, is_state=True),
    "uttarakhand": _loc("Uttarakhand", None, "Uttarakhand", "India", "INR", 30.0668, 79.0193, is_state=True),
    "uttaranchal": _loc("Uttarakhand", None, "Uttarakhand", "India", "INR", 30.0668, 79.0193, is_state=True),
    "jharkhand": _loc("Jharkhand", None, "Jharkhand", "India", "INR", 23.6102, 85.2799, is_state=True),
    "chhattisgarh": _loc("Chhattisgarh", None, "Chhattisgarh", "India", "INR", 21.2787, 81.8661, is_state=True),
    "jammu and kashmir": _loc("Jammu & Kashmir", None, "Jammu & Kashmir", "India", "INR", 33.7782, 76.5762, is_state=True),
    "delhi ncr": _loc("Delhi NCR", "Delhi", "Delhi", "India", "INR", 28.6139, 77.2090, is_city=True, is_state=True),
    "new delhi": _loc("Delhi", "Delhi", "Delhi", "India", "INR", 28.6139, 77.2090, is_city=True, is_state=True),

    # --- Pan-National / Countries --------------------------------------------
    "pan india": _loc("India", None, None, "India", "INR", 20.5937, 78.9629, is_country=True),
    "all india": _loc("India", None, None, "India", "INR", 20.5937, 78.9629, is_country=True),
    "across india": _loc("India", None, None, "India", "INR", 20.5937, 78.9629, is_country=True),
    "nationwide": _loc("India", None, None, "India", "INR", 20.5937, 78.9629, is_country=True),
    "india": _loc("India", None, None, "India", "INR", 20.5937, 78.9629, is_country=True),
    "china": _loc("China", None, None, "China", "CNY", 35.8617, 104.1954, is_country=True),
    "united states": _loc("United States", None, None, "United States", "USD", 37.0902, -95.7129, is_country=True),
    "usa": _loc("United States", None, None, "United States", "USD", 37.0902, -95.7129, is_country=True),
    "united kingdom": _loc("United Kingdom", None, None, "United Kingdom", "GBP", 55.3781, -3.4360, is_country=True),
    "germany": _loc("Germany", None, None, "Germany", "EUR", 51.1657, 10.4515, is_country=True),
    "united arab emirates": _loc("United Arab Emirates", None, None, "United Arab Emirates", "AED", 23.4241, 53.8478, is_country=True),
    "uae": _loc("United Arab Emirates", None, None, "United Arab Emirates", "AED", 23.4241, 53.8478, is_country=True),
    "vietnam": _loc("Vietnam", None, None, "Vietnam", "VND", 14.0583, 108.2772, is_country=True),
}

# Longest first, so "ho chi minh city" wins over any shorter substring.
_CITY_KEYS = sorted(CITIES, key=len, reverse=True)

# Build unified location lookup: city matches map to Location with is_city=True.
_UNIFIED_LOCATIONS: dict[str, Location] = {}
for _k, _city in CITIES.items():
    _UNIFIED_LOCATIONS[_k] = _loc(
        name=_city.name,
        city=_city.name,
        state=_city.region,
        country=_city.country,
        currency=_city.currency,
        lat=_city.latitude,
        lng=_city.longitude,
        is_city=True,
        matched_key=_k,
    )
for _k, _reg in REGIONS.items():
    if _k not in _UNIFIED_LOCATIONS:
        _UNIFIED_LOCATIONS[_k] = _reg._replace(matched_key=_k)

_LOCATION_KEYS = sorted(_UNIFIED_LOCATIONS, key=len, reverse=True)


def find_city(text: str) -> Optional[City]:
    """First known city mentioned anywhere in the text."""
    lowered = f" {text.lower()} "
    for key in _CITY_KEYS:
        if f" {key} " in lowered or f" {key}." in lowered or f" {key}," in lowered:
            return CITIES[key]
    return None


def find_location(text: str) -> Optional[Location]:
    """First known city, state, territory or country mentioned anywhere in the text."""
    # Normalize punctuation to spaces for clean token matching
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower())
    padded = f" {' '.join(cleaned.split())} "
    for key in _LOCATION_KEYS:
        target = key.replace("-", " ")
        if f" {target} " in padded:
            return _UNIFIED_LOCATIONS[key]
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

"""Known cities, shared by the seeder and the query parser.

Coordinates matter: location scoring prefers real distance over string equality
whenever both sides have them, so a city recognised here is worth far more to
matching than a free-text string.

Each city also carries its country's usual trading currency, which is what makes
a multi-country corpus generate believable prices.
"""

import re
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
    "gurgaon": _c("Gurugram", "Haryana", "India", "INR", 28.4595, 77.0266),
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
    # Additional Top Indian Industrial & Manufacturing Hubs
    "raipur": _c("Raipur", "Chhattisgarh", "India", "INR", 21.2514, 81.6296),
    "jamshedpur": _c("Jamshedpur", "Jharkhand", "India", "INR", 22.8046, 86.2029),
    "bhiwadi": _c("Bhiwadi", "Rajasthan", "India", "INR", 28.2104, 76.8606),
    "morbi": _c("Morbi", "Gujarat", "India", "INR", 22.8120, 70.8236),
    "vapi": _c("Vapi", "Gujarat", "India", "INR", 20.3714, 72.9043),
    "ankleshwar": _c("Ankleshwar", "Gujarat", "India", "INR", 21.6264, 73.0033),
    "pithampur": _c("Pithampur", "Madhya Pradesh", "India", "INR", 22.6144, 75.6822),
    "dhanbad": _c("Dhanbad", "Jharkhand", "India", "INR", 23.7957, 86.4304),
    "bokaro": _c("Bokaro", "Jharkhand", "India", "INR", 23.6693, 86.1511),
    "rourkela": _c("Rourkela", "Odisha", "India", "INR", 22.2604, 84.8536),
    "bhilai": _c("Bhilai", "Chhattisgarh", "India", "INR", 21.1938, 81.3509),
    "durgapur": _c("Durgapur", "West Bengal", "India", "INR", 23.5204, 87.3119),
    "asansol": _c("Asansol", "West Bengal", "India", "INR", 23.6739, 86.9524),
    "salem": _c("Salem", "Tamil Nadu", "India", "INR", 11.6643, 78.1460),
    "madurai": _c("Madurai", "Tamil Nadu", "India", "INR", 9.9252, 78.1198),
    "tiruchirappalli": _c("Tiruchirappalli", "Tamil Nadu", "India", "INR", 10.7905, 78.7047),
    "hosur": _c("Hosur", "Tamil Nadu", "India", "INR", 12.7409, 77.8253),
    "belagavi": _c("Belagavi", "Karnataka", "India", "INR", 15.8497, 74.4977),
    "hubballi": _c("Hubballi", "Karnataka", "India", "INR", 15.3647, 75.1240),
    "baddi": _c("Baddi", "Himachal Pradesh", "India", "INR", 30.9578, 76.7914),
    "rudrapur": _c("Rudrapur", "Uttarakhand", "India", "INR", 28.9800, 79.4000),
    "haridwar": _c("Haridwar", "Uttarakhand", "India", "INR", 29.9457, 78.1642),
    "agra": _c("Agra", "Uttar Pradesh", "India", "INR", 27.1767, 78.0081),
    "meerut": _c("Meerut", "Uttar Pradesh", "India", "INR", 28.9845, 77.7064),
    "gwalior": _c("Gwalior", "Madhya Pradesh", "India", "INR", 26.2183, 78.1828),
    "jabalpur": _c("Jabalpur", "Madhya Pradesh", "India", "INR", 23.1815, 79.9864),

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

    # --- Top Global Industrial & Manufacturing Hubs ---------------------------
    "stuttgart": _c("Stuttgart", "Baden-Württemberg", "Germany", "EUR", 48.7758, 9.1829),
    "frankfurt": _c("Frankfurt", "Hesse", "Germany", "EUR", 50.1109, 8.6821),
    "dusseldorf": _c("Dusseldorf", "North Rhine-Westphalia", "Germany", "EUR", 51.2277, 6.7735),
    "paris": _c("Paris", "Île-de-France", "France", "EUR", 48.8566, 2.3522),
    "lyon": _c("Lyon", "Auvergne-Rhône-Alpes", "France", "EUR", 45.7640, 4.8357),
    "antwerp": _c("Antwerp", "Flanders", "Belgium", "EUR", 51.2194, 4.4025),
    "kyoto": _c("Kyoto", "Kansai", "Japan", "JPY", 35.0116, 135.7681),
    "tokyo": _c("Tokyo", "Kanto", "Japan", "JPY", 35.6762, 139.6503),
    "osaka": _c("Osaka", "Kansai", "Japan", "JPY", 34.6937, 135.5023),
    "seoul": _c("Seoul", "Seoul", "South Korea", "KRW", 37.5665, 126.9780),
    "busan": _c("Busan", "Yeongnam", "South Korea", "KRW", 35.1796, 129.0756),
    "detroit": _c("Detroit", "Michigan", "United States", "USD", 42.3314, -83.0458),
    "pittsburgh": _c("Pittsburgh", "Pennsylvania", "United States", "USD", 40.4406, -79.9959),
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

# ---------------------------------------------------------------------------
# Enhancement #2: Historical / Regional City Alias Resolution
#
# Colloquial and historical Indian city names mapped to modern canonical keys.
# Wired into find_city and find_location so "Bombay", "Calcutta", "Madras"
# resolve transparently. Also adds fuzzy matching via Jaro-Winkler so
# "Mumbay" or "Banglore" are caught without hardcoded typo dictionaries.
# ---------------------------------------------------------------------------

CITY_ALIASES: dict[str, str] = {
    # Historical Indian city names
    "bombay": "mumbai",
    "calcutta": "kolkata",
    "madras": "chennai",
    "bangalore": "bengaluru",  # already exists, but alias ensures both paths work
    "baroda": "vadodara",
    "cochin": "kochi",
    "poona": "pune",
    "banaras": "varanasi",
    "benares": "varanasi",
    "kashi": "varanasi",
    "gurgaon": "gurugram",  # already exists, alias ensures reverse lookup
    "vizag": "visakhapatnam",  # already exists
    "trivandrum": "thiruvananthapuram",
    "pondicherry": "puducherry",
    "simla": "shimla",
    "ooty": "udhagamandalam",
    "mangalore": "mangaluru",
    "mysore": "mysuru",
    "hubli": "hubballi",
    "belgaum": "belagavi",
    "gulbarga": "kalaburagi",
    # Common typos / short forms
    "blr": "bengaluru",
    "del": "delhi",
    "mum": "mumbai",
    "hyd": "hyderabad",
    "chn": "chennai",
    "ahd": "ahmedabad",
    "jp": "jaipur",
    "lko": "lucknow",
    "kol": "kolkata",
    # Global trade hub aliases
    "canton": "guangzhou",
    "peking": "beijing",
    "saigon": "ho chi minh city",
    "bombai": "mumbai",
    "new york": "newark",  # closest in our DB
    "la": "los angeles",
}

# Add missing cities that aliases reference
_ALIAS_CITIES_EXTRA: dict[str, City] = {
    "varanasi": _c("Varanasi", "Uttar Pradesh", "India", "INR", 25.3176, 82.9739),
    "shimla": _c("Shimla", "Himachal Pradesh", "India", "INR", 31.1048, 77.1734),
    "mangaluru": _c("Mangaluru", "Karnataka", "India", "INR", 12.9141, 74.8560),
    "mysuru": _c("Mysuru", "Karnataka", "India", "INR", 12.2958, 76.6394),
}
CITIES.update(_ALIAS_CITIES_EXTRA)

# Register aliases into the CITIES dict pointing to the canonical city
for _alias, _canonical_key in CITY_ALIASES.items():
    if _canonical_key in CITIES and _alias not in CITIES:
        CITIES[_alias] = CITIES[_canonical_key]

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


def _jw_city(s1: str, s2: str) -> float:
    """Jaro-Winkler similarity for city name fuzzy matching."""
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0
    match_dist = max(len1, len2) // 2 - 1
    if match_dist < 0:
        match_dist = 0
    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0
    transpositions = 0
    for i in range(len1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    jaro = (matches / len1 + matches / len2 + (matches - transpositions / 2) / matches) / 3
    prefix = 0
    for i in range(min(4, len1, len2)):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break
    return jaro + prefix * 0.1 * (1 - jaro)


def fuzzy_find_city(word: str, threshold: float = 0.85) -> Optional[City]:
    """Fuzzy match a city name against all known cities.

    Uses Jaro-Winkler similarity so typos like "Mumbay", "Banglore",
    "Kolkatta" resolve without static dictionaries. Minimum 4 characters
    to avoid false positives on short words.
    """
    if not word or len(word) < 4:
        return None
    w = word.lower().strip()
    # Exact/alias lookup first
    if w in CITIES:
        return CITIES[w]
    best_score = 0.0
    best_key: Optional[str] = None
    for key in CITIES:
        if len(key) < 3 or ' ' in key:  # skip short codes and multi-word
            continue
        sim = _jw_city(w, key)
        if sim > best_score:
            best_score = sim
            best_key = key
    if best_score >= threshold and best_key is not None:
        return CITIES[best_key]
    return None


_FUZZY_MAX_WORDS = 3


def find_city(text: str) -> Optional[City]:
    """First known city mentioned anywhere in the text.

    Checks exact matches first, then historical aliases, then fuzzy."""
    lowered = f" {text.lower()} "
    for key in _CITY_KEYS:
        if f" {key} " in lowered or f" {key}." in lowered or f" {key}," in lowered:
            return CITIES[key]
    # Fuzzy fallback: try each word token. Only for a short place phrase -- on a whole chat
    # message any foreign-sounding word ("unjha") fuzzy-matches some far-away city.
    words = text.lower().split()
    if len(words) > _FUZZY_MAX_WORDS:
        return None
    for word in words:
        clean = word.strip(".,;:!?\"'")
        if len(clean) >= 4:
            hit = fuzzy_find_city(clean)
            if hit is not None:
                return hit
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
    cached = _location_from_place(_ai_lexicon().find_cached_place(text))
    if cached is not None:
        return cached
    return _fuzzy_location(cleaned)


def exact_location(text: str) -> Optional[Location]:
    """Like ``find_location`` but only exact gazetteer names and aliases: no learned places, no typo-fuzzy.

    For filters, where a wrong fuzzy guess would silently widen or move the result set."""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in (text or "").lower())
    padded = f" {' '.join(cleaned.split())} "
    for key in _LOCATION_KEYS:
        if f" {key.replace('-', ' ')} " in padded:
            return _UNIFIED_LOCATIONS[key]
    return None


def city_spellings(canonical_city: str) -> set[str]:
    """Every lower-case gazetteer spelling of one canonical city ("Mumbai" -> {"mumbai", "bombay", ...})."""
    target = (canonical_city or "").strip().lower()
    out = {target} if target else set()
    for key, loc in _UNIFIED_LOCATIONS.items():
        if loc.is_city and loc.city and loc.city.lower() == target:
            out.add(key.replace("-", " "))
    return out


# India Post: the first three digits of a PIN code name a sorting district. Metro prefixes and the larger
# trading cities only; a PIN outside the table simply does not resolve.
PIN_PREFIX_CITY: dict[str, str] = {
    "110": "delhi", "400": "mumbai", "401": "mumbai", "560": "bengaluru", "600": "chennai", "700": "kolkata",
    "500": "hyderabad", "411": "pune", "380": "ahmedabad", "122": "gurugram", "201": "noida", "121": "faridabad",
    "302": "jaipur", "226": "lucknow", "395": "surat", "390": "vadodara", "452": "indore", "462": "bhopal",
    "440": "nagpur", "141": "ludhiana", "160": "chandigarh", "208": "kanpur", "530": "visakhapatnam",
    "682": "kochi", "641": "coimbatore",
}
_PIN_RE = re.compile(r"^\s*(\d{3})\s?(\d{3})\s*$")


def is_pin(text: str) -> bool:
    return bool(_PIN_RE.match(text or ""))


def resolve_pin(text: str) -> Optional[Location]:
    """The city a 6-digit PIN code belongs to, by its 3-digit prefix (None when the prefix is not in the table)."""
    m = _PIN_RE.match(text or "")
    key = PIN_PREFIX_CITY.get(m.group(1)) if m else None
    return _UNIFIED_LOCATIONS.get(key) if key else None


def _ai_lexicon():
    from services import ai_lexicon
    return ai_lexicon


def _location_from_place(p) -> Optional[Location]:
    """A learned (model-given) place as a Location."""
    if p is None:
        return None
    return _loc(
        name=p.name, city=p.city, state=p.state, country=p.country, currency=p.currency,
        lat=p.latitude, lng=p.longitude,
        is_city=bool(p.city), is_state=bool(p.state) and not p.city,
        is_country=not p.city and not p.state, matched_key=p.name.lower(),
    )


def _fuzzy_location(cleaned: str) -> Optional[Location]:
    """Typo recovery for an explicit place phrase (<= 3 words), never a whole message."""
    words = cleaned.split()
    if len(words) > _FUZZY_MAX_WORDS:
        return None
    for word in words:
        if len(word) >= 4:
            city = fuzzy_find_city(word)
            if city is not None:
                return _UNIFIED_LOCATIONS.get(city.name.lower()) or _loc(
                    name=city.name, city=city.name, state=city.region,
                    country=city.country, currency=city.currency,
                    lat=city.latitude, lng=city.longitude,
                    is_city=True, matched_key=city.name.lower(),
                )
    return None


async def resolve_location(text: str) -> Optional[Location]:
    """Like ``find_location`` for a place phrase, but may ask the model about an unknown place.

    Order: verified static tables, cached AI place, one model call (remembered on
    disk), then typo-fuzzy on the static cities. The model comes before fuzzy so
    "unjha" is Unjha, not its nearest-sounding known city.
    """
    if not text or not text.strip():
        return None
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower())
    padded = f" {' '.join(cleaned.split())} "
    for key in _LOCATION_KEYS:
        if f" {key.replace('-', ' ')} " in padded:
            return _UNIFIED_LOCATIONS[key]
    lex = _ai_lexicon()
    cached = _location_from_place(lex.find_cached_place(text))
    if cached is not None:
        return cached
    if len(cleaned.split()) <= 6:
        fetched = _location_from_place(await lex.resolve_place(text))
        if fetched is not None:
            return fetched
    return _fuzzy_location(cleaned)


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


# Spellings of the same country, so "IN", "Bharat" and "India" compare equal
# when deciding whether two listings are domestic to each other.
_COUNTRY_ALIASES: dict[str, str] = {
    "in": "india", "ind": "india", "bharat": "india", "republic of india": "india",
    "us": "united states", "usa": "united states", "u.s.": "united states",
    "u.s.a.": "united states", "united states of america": "united states", "america": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom", "gb": "united kingdom",
    "great britain": "united kingdom", "britain": "united kingdom", "england": "united kingdom",
    "uae": "united arab emirates", "u.a.e.": "united arab emirates", "emirates": "united arab emirates",
    "ksa": "saudi arabia", "prc": "china", "people's republic of china": "china",
    "pk": "pakistan", "bd": "bangladesh", "cn": "china", "de": "germany",
    "viet nam": "vietnam", "turkiye": "turkey", "türkiye": "turkey",
}


def normalise_country(country: Optional[object]) -> Optional[str]:
    """Lowercase canonical country name, or None when not given."""
    if country is None:
        return None
    cleaned = " ".join(str(country).strip().lower().split())
    if not cleaned:
        return None
    return _COUNTRY_ALIASES.get(cleaned, cleaned)


def resolve_coordinates(
    city: Optional[str] = None,
    state: Optional[str] = None,
    country: Optional[str] = None,
) -> tuple[Optional[float], Optional[float]]:
    """Resolve coordinates hierarchically: city -> state -> country.

    Guarantees that distance calculation / Haversine never drops to None when
    at least one level of location hierarchy is recognised.
    """
    if city:
        c_clean = str(city).strip().lower()
        if c_clean in CITIES:
            c = CITIES[c_clean]
            return c.latitude, c.longitude
        try:
            learned = _ai_lexicon().place(c_clean)
        except Exception:
            learned = None
        if learned is not None:
            return learned.latitude, learned.longitude
        fuz = fuzzy_find_city(c_clean) if len(c_clean.split()) <= _FUZZY_MAX_WORDS else None
        if fuz:
            return fuz.latitude, fuz.longitude
    if state:
        s_clean = str(state).strip().lower()
        if s_clean in REGIONS:
            r = REGIONS[s_clean]
            return r.latitude, r.longitude
    if country:
        co_clean = normalise_country(country)
        if co_clean and co_clean in REGIONS:
            r = REGIONS[co_clean]
            return r.latitude, r.longitude
    return None, None


def distance_between(
    loc_a: dict[str, Optional[object]],
    loc_b: dict[str, Optional[object]],
) -> Optional[float]:
    """Great-circle distance between two locations with automatic coordinate fallback."""
    from services import match_scoring
    return match_scoring.distance_km(loc_a, loc_b, fallback=True)

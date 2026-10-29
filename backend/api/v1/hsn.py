"""HSN & SAC master classification endpoints.

Provides hierarchical navigation and instant full-text search across all 22,616+
official tariff codes extracted from HSN_SAC.xlsx.
"""

import asyncio
import os
import re
import sqlite3
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from api.deps import CurrentUser
from services.hsn_service import (
    HS_CHAPTERS,
    classify_shared,
    confirm_learned,
    vote_eligible,
    enrich_search_results,
    is_code_query,
    is_plausible_query,
    keyword_hint,
    learned_get,
    lexical_rows,
    normalise_trade_text,
    norm_query,
    query_terms,
    resolve_code,
    validate_master,
    warm_in_background,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hsn", tags=["hsn"])

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "hsn_master.db",
)


_PIPE_HEADINGS = frozenset({"7303", "7304", "7305", "7306"})
_PIPE_PROCESS_RE = re.compile(r"\b(?:seamless|welded|erw|hfw|saw|ssaw|lsaw|cdw|extruded|cast)\b")
_db_validated = False


def _get_db():
    global _db_validated
    if not os.path.exists(DB_PATH):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HSN master database is not built. Run scripts/build_hsn_database.py",
        )
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not _db_validated:  # once per process: log rows whose chapter is not their code's first two digits
        _db_validated = True
        validate_master(conn)
    return conn


# Known friendly overrides for commercial Indian trade commodities
# Known friendly overrides for commercial Indian trade commodities across multiple categories
_TRADE_TITLE_OVERRIDES: dict[str, str] = {
    # Eyewear, Optics & Protective Instruments (Chapter 90 & 70)
    "90041000": "Sunglasses (Finished Protective Eyewear)",
    "900410": "Sunglasses & Tinted Protective Eyewear",
    "900490": "Spectacles, Corrective Eyeglasses & Protective Goggles",
    "9004": "Spectacles, Goggles & Sunglasses (Protective Eyewear)",
    "70159020": "Glass for Sun Glasses (Uncut Glass Blanks/Parts)",
    "7015": "Clock, Watch & Spectacle Glasses (Uncut Glass Blanks)",
    "900311": "Frames and Mountings for Spectacles (Plastic)",
    "900319": "Frames and Mountings for Spectacles (Other Materials)",
    "9003": "Frames & Mountings for Spectacles or Goggles",
    "900130": "Contact Lenses",
    "9001": "Optical Fibres & Lenses (Unmounted)",

    # Beverages, Soft Drinks & Mineral Water (Chapter 22 & 20)
    "2202": "Non-Alcoholic Aerated Beverages & Soft Drinks",
    "220210": "Aerated Waters with Sugar/Flavor (Soft Drinks, Cola, Soda)",
    "220290": "Fruit-Based Drinks & Other Non-Alcoholic Beverages",
    "220299": "Energy Drinks, Sparkling Fruit Beverages & Tonics",
    "2201": "Packaged Mineral & Purified Drinking Water",
    "220110": "Mineral Waters and Aerated Waters",
    "220190": "Packaged Drinking Water, Ordinary Natural Waters",
    "2009": "Fruit Juices & Vegetable Juices",
    "200971": "Apple Juice (Brix value <= 20)",
    "200989": "Mango & Other Single Fruit Juices",

    # Footwear, Shoes & Apparel (Chapter 64, 61, 62)
    "640411": "Sports Footwear, Tennis Shoes, Basketball Shoes & Sneakers",
    "640419": "Casual Footwear with Rubber Soles & Textile Uppers",
    "6404": "Footwear with Outer Soles of Rubber/Plastics & Textile Uppers",
    "640359": "Leather Formal Shoes & Footwear",
    "6403": "Footwear with Outer Soles of Rubber, Plastics, Leather",
    "640220": "Footwear with Upper Straps / Sandals / Flip Flops",
    "640299": "Casual Slippers & Chappals",
    "6402": "Footwear with Outer Soles and Uppers of Rubber/Plastics",
    "610910": "Cotton T-Shirts, Singlets & Vests (Knitted)",
    "6109": "T-Shirts, Singlets & Other Vests (Knitted or Crocheted)",
    "610510": "Men's Knitted Cotton Polo Shirts",
    "620520": "Men's Woven Cotton Shirts",
    "620342": "Men's Cotton Denim Jeans & Trousers",
    "6203": "Men's Suits, Jackets, Blazers, Trousers & Shorts",
    "620442": "Women's Cotton Dresses, Kurtis & Tunics",
    "611020": "Cotton Knitted Sweatshirts, Sweaters & Hoodies",
    "620130": "Men's Overcoats, Car-Coats & Jackets",

    # Bags, Luggage & Travel Accessories (Chapter 42)
    "420212": "Executive Cases, Briefcases, Suitcases & Laptop Bags",
    "420292": "Backpacks, School Bags & Travel Pouches",
    "420222": "Handbags with Outer Surface of Plastic Sheeting / Textiles",
    "420231": "Leather Wallets, Billfolds & Purses",
    "4202": "Trunks, Suitcases, Backpacks, Laptop Bags & Handbags",

    # Consumer Electronics & Telecom (Chapter 84 & 85)
    "851713": "Smartphones & Mobile Handsets",
    "851714": "Other Telephones for Cellular Networks",
    "851762": "Smartwatches, Routers, Modems & Wireless Connected Devices",
    "8517": "Telephone Sets, Smartphones & Network Telecommunication Apparatus",
    "847130": "Laptops & Portable Automatic Data Processing Machines",
    "847141": "Desktop Computers & Data Processing Units",
    "8471": "Automatic Data Processing Machines (Computers & Laptops)",
    "851830": "Headphones, Earphones & Wireless Bluetooth Earbuds",
    "851821": "Single Loudspeakers & Bluetooth Speakers",
    "8518": "Microphones, Loudspeakers, Headphones & Audio Amplifiers",
    "852872": "Colour Smart TVs, LED Televisions & Monitors",
    "8528": "Monitors, Projectors & Television Reception Apparatus",
    "850440": "Static Power Converters, Inverters & Phone Chargers",
    "850760": "Lithium-Ion Storage Batteries & Power Banks",
    "850710": "Lead-Acid Automotive Storage Batteries",
    "852589": "Digital Cameras & Surveillance CCTV Cameras",
    "85414011": "Solar Photovoltaic Cells (Unassembled)",
    "85414012": "Solar PV Modules / Solar Panels",
    "8541": "Solar Cells & Semiconductor Devices",

    # Home & Kitchen Appliances (Chapter 84 & 85)
    "841510": "Window or Split System Air Conditioners",
    "8415": "Air Conditioning Machines & Units",
    "841821": "Domestic Compression-Type Refrigerators",
    "8418": "Refrigerators, Freezers & Refrigerating Equipment",
    "845011": "Fully-Automatic Household Washing Machines",
    "8450": "Household or Laundry-Type Washing Machines",
    "851650": "Microwave Ovens",
    "851610": "Electric Water Heaters & Geysers",
    "841451": "Ceiling, Table & Floor Electric Fans",
    "842121": "Water Purifiers & Filtering Machinery",

    # Agriculture, Dairy & Food Staples (Chapter 04, 07, 10, 11, 15, 17, 09)
    "0401": "Fresh Milk & Cream (Pasteurized / Unsweetened)",
    "0402": "Milk Powder & Condensed Milk",
    "0403": "Curd, Yogurt & Buttermilk",
    "0405": "Butter, Ghee & Dairy Spreads",
    "0406": "Cheese & Paneer",
    "07133190": "Sabut Moong / Green Gram (Vigna Radiata)",
    "07133110": "Urad Dal / Black Gram (Vigna Mungo)",
    "071331": "Moong / Urad Beans (Vigna Mungo / Radiata)",
    "07132010": "Kabuli Chana (White Chickpeas)",
    "07132020": "Bengal Gram / Desi Chana (Chickpeas)",
    "071320": "Chickpeas (Garbanzo / Chana)",
    "071333": "Rajma / Kidney Beans",
    "071340": "Masoor Dal / Red Lentils",
    "071360": "Toor / Arhar Dal (Pigeon Peas)",
    "071310": "Dry Peas / Matar / White Vatana",
    "0713": "Pulses, Dals & Legumes (Shelled/Dried)",
    "10063010": "Parboiled Basmati Rice",
    "10063020": "Basmati Rice (Non-Parboiled)",
    "1006": "Rice & Paddy",
    "1001": "Wheat & Meslin (Gehun)",
    "1005": "Maize / Corn (Makka)",

    # FMCG, Personal Care & Hygiene (Chapter 19, 21, 25, 33, 34, 38, 96, 40)
    "1905": "Bread, Pastry, Cakes, Biscuits & Bakery Wares",
    "1902": "Pasta, Noodles & Instant Macaroni",
    "2501": "Common Salt & Pure Sodium Chloride",
    "340111": "Toilet & Bathing Soap Bars",
    "340130": "Liquid Soaps, Handwash & Skin Cleansers",
    "3401": "Soap, Organic Surface-Active Washing Bars & Preparations",
    "330510": "Shampoos & Hair Cleansers",
    "330590": "Hair Oils, Conditioners & Styling Preparations",
    "330610": "Dentifrices & Toothpastes",
    "330720": "Personal Deodorants & Antiperspirants",
    "330499": "Sunscreen Lotions & Skincare Creams",
    "330300": "Perfumes & Toilet Waters",
    "961900": "Sanitary Towels, Pads, Napkins & Diapers",
    "380894": "Disinfectants & Hand Sanitizers",
    "392620": "Nitrile Examination Gloves",
    "401511": "Latex Surgical Gloves",
    "630790": "PPE Face Masks (N95 / 3-Ply)",
    "252329": "Portland Cement & Construction Binders",
    "2523": "Portland Cement, Aluminous Cement & Slag Cement",
    "401110": "Passenger Motor Car Pneumatic Tyres",
    "401120": "Bus & Lorry Pneumatic Tyres",
    "4011": "New Pneumatic Rubber Tyres",
    "650610": "Safety Helmets for Two-Wheelers & Industrial Use",
    "72142090": "TMT Rebars / Sariya (Fe 500D / 550D)",
    "7214": "TMT Bars & Deformed Steel Rods",
    "7210": "Galvanized Steel Sheets & GI Coils",
    "5201": "Raw Cotton / Cotton Bales (Kapas)",
    "5205": "Cotton Yarn & Ring Spun Yarn",
    "4819": "Corrugated Cartons & Packaging Boxes",
    "9954": "Construction & Infrastructure Services",
    "9983": "Information Technology (IT) & Professional Services",
    "9965": "Goods Transport & Logistics Services",
}

# Fast instant semantic mapping for popular consumer brands & commodities
_POPULAR_COMMERCIAL_BRANDS: dict[str, tuple[str, str]] = {
    # Eyewear & Optics (Chapter 90 & 70)
    "sunglasses": ("90041000", "Finished protective & tinted sunglasses (Chapter 90)"),
    "sun glasses": ("90041000", "Finished protective & tinted sunglasses (Chapter 90)"),
    "sunglass": ("90041000", "Finished protective sunglasses (Chapter 90)"),
    "sun glass": ("90041000", "Finished protective sunglasses (Chapter 90)"),
    "spectacles": ("900490", "Corrective spectacles and eyeglasses"),
    "goggles": ("900490", "Protective eyewear and goggles"),
    "reading glasses": ("900490", "Corrective reading glasses"),
    "glass for sun glasses": ("70159020", "Uncut curved glass blanks for sunglasses (Chapter 70)"),

    # Beverages & Soft Drinks (Chapter 22 & 20)
    "soft drink": ("2202", "Aerated non-alcoholic soft drinks & beverages"),
    "soft drinks": ("2202", "Aerated non-alcoholic soft drinks & beverages"),
    "softdrink": ("2202", "Aerated non-alcoholic soft drinks & beverages"),
    "cold drink": ("2202", "Aerated non-alcoholic cold drinks & beverages"),
    "cold drinks": ("2202", "Aerated non-alcoholic cold drinks & beverages"),
    "coca cola": ("220210", "Carbonated soft drink / flavoured aerated water"),
    "cococola": ("220210", "Carbonated cola soft drink"),
    "coco cola": ("220210", "Carbonated cola soft drink"),
    "coke": ("220210", "Carbonated soft drink / flavoured aerated water"),
    "diet coke": ("220210", "Sugar-free carbonated cola beverage"),
    "pepsi": ("220210", "Carbonated cola soft drink"),
    "thums up": ("220210", "Carbonated cola soft drink"),
    "sprite": ("220210", "Lemon-lime flavoured aerated water"),
    "7up": ("220210", "Lemon-lime flavoured aerated water"),
    "limca": ("220210", "Lemon flavoured aerated soft drink"),
    "mirinda": ("220210", "Orange flavoured aerated water"),
    "fanta": ("220210", "Orange flavoured aerated water"),
    "mountain dew": ("220210", "Citrus flavoured aerated soft drink"),
    "appy fizz": ("2202", "Aerated sparkling non-alcoholic apple fruit beverage"),
    "red bull": ("2202", "Caffeinated energy drink / non-alcoholic beverage"),
    "monster energy": ("2202", "Caffeinated energy drink / non-alcoholic beverage"),
    "sting": ("2202", "Caffeinated energy drink / soft drink"),
    "tropicana": ("2009", "100% natural fruit juice"),
    "real juice": ("2009", "Packaged fruit juice beverage"),
    "bisleri": ("2201", "Packaged mineral drinking water"),
    "kinley": ("2201", "Packaged drinking water / soda"),
    "aquafina": ("2201", "Packaged purified drinking water"),
    "mineral water": ("2201", "Packaged mineral drinking water"),
    "drinking water": ("2201", "Packaged purified drinking water"),

    # Footwear & Apparel (Chapter 64, 61, 62)
    "sneakers": ("640411", "Sports footwear, tennis shoes, basketball shoes & sneakers"),
    "sneaker": ("640411", "Sports footwear & sneakers"),
    "sports shoes": ("640411", "Athletic & sports footwear (tennis/running shoes)"),
    "running shoes": ("640411", "Athletic running shoes & sneakers"),
    "shoes": ("6403", "Footwear with outer soles of rubber/leather"),
    "leather shoes": ("640359", "Formal leather footwear"),
    "sandals": ("640220", "Casual footwear and sandals"),
    "slippers": ("640299", "Casual slippers / chappals"),
    "boots": ("640391", "Ankle boots and safety footwear"),
    "t-shirt": ("610910", "Cotton knitted T-shirts and singlets"),
    "t shirt": ("610910", "Cotton knitted T-shirts and singlets"),
    "tshirt": ("610910", "Cotton knitted T-shirts and singlets"),
    "polo shirt": ("610510", "Men's knitted polo shirts"),
    "shirt": ("620520", "Men's woven cotton shirts"),
    "jeans": ("620342", "Cotton denim jeans and trousers"),
    "denim": ("620342", "Cotton denim garments and trousers"),
    "hoodie": ("611020", "Knitted sweatshirts and hoodies"),
    "jacket": ("620130", "Men's woven jackets and overcoats"),
    "saree": ("520859", "Woven cotton & silk traditional sarees"),
    "kurti": ("620442", "Women's ethnic tunics and kurtis"),
    "tracksuit": ("611212", "Synthetic athletic tracksuits"),
    "socks": ("611595", "Cotton socks and hosiery"),

    # Bags & Luggage (Chapter 42)
    "laptop bag": ("420212", "Carrying bags, briefcases & laptop sleeves"),
    "laptopbag": ("420212", "Carrying bags, briefcases & laptop sleeves"),
    "backpack": ("420292", "Travel backpacks and school bags"),
    "school bag": ("420292", "Student satchels and backpacks"),
    "luggage": ("420212", "Suitcases, executive cases & travel luggage"),
    "suitcase": ("420212", "Travel suitcases and trolleys"),
    "handbag": ("420222", "Fashion handbags and totes"),
    "wallet": ("420231", "Leather pocket wallets and purses"),

    # Consumer Electronics & Telecom (Chapter 84 & 85)
    "smartphone": ("851713", "Smartphones and mobile cellular devices"),
    "smart phone": ("851713", "Smartphones and mobile devices"),
    "mobile phone": ("851713", "Mobile phones and cellular handsets"),
    "cell phone": ("851713", "Cellular telephones and smartphones"),
    "iphone": ("851713", "Apple iPhone smartphones"),
    "samsung galaxy": ("851713", "Samsung Galaxy smartphones"),
    "laptop": ("847130", "Portable laptops and notebook computers"),
    "macbook": ("847130", "Apple MacBook laptops"),
    "tablet": ("847130", "Touchscreen tablets and portable computers"),
    "ipad": ("847130", "Apple iPad tablet computers"),
    "smartwatch": ("851762", "Smartwatches and wearable connected devices"),
    "apple watch": ("851762", "Smartwatches and wearable devices"),
    "headphones": ("851830", "Over-ear headphones and headsets"),
    "headphone": ("851830", "Headphones and audio headsets"),
    "earphones": ("851830", "In-ear earphones and wired earbuds"),
    "earphone": ("851830", "In-ear earphones and earbuds"),
    "earbuds": ("851830", "Wireless Bluetooth earbuds and headsets"),
    "airpods": ("851830", "Wireless Apple AirPods earbuds"),
    "bluetooth speaker": ("851821", "Portable Bluetooth loudspeakers"),
    "television": ("852872", "Colour television monitors and Smart TVs"),
    "smart tv": ("852872", "Smart LED / OLED televisions"),
    "tv": ("852872", "Colour television receivers and monitors"),
    "mobile charger": ("850440", "USB phone chargers and power adapters"),
    "charger": ("850440", "Static electrical power chargers"),
    "power bank": ("850760", "Portable lithium-ion power banks"),
    "powerbank": ("850760", "Portable lithium-ion power banks"),
    "cctv camera": ("852589", "Surveillance and security CCTV cameras"),
    "solar panel": ("85414012", "Solar PV Modules / Photovoltaic panels"),
    "solar inverter": ("850440", "Solar static power inverters"),
    "lithium battery": ("850760", "Lithium-ion storage battery cells"),

    # Home & Kitchen Appliances (Chapter 84 & 85)
    "air conditioner": ("841510", "Split and window air conditioners"),
    "ac": ("841510", "Split and window air conditioners"),
    "refrigerator": ("841821", "Domestic compression-type refrigerators"),
    "fridge": ("841821", "Domestic refrigerators and freezers"),
    "washing machine": ("845011", "Automatic household washing machines"),
    "microwave": ("851650", "Domestic microwave ovens"),
    "ceiling fan": ("841451", "Electric ceiling and table fans"),
    "water purifier": ("842121", "RO and UV domestic water purifiers"),
    "geyser": ("851610", "Electric water geysers and storage heaters"),
    "water heater": ("851610", "Electric water geysers and immersion heaters"),
    "mixer grinder": ("850940", "Food grinders, blenders and mixers"),
    "vacuum cleaner": ("850811", "Electric domestic vacuum cleaners"),

    # Dairy, Grains & Food Staples (Chapter 04, 07, 10, 11, 15, 17, 09)
    "milk": ("0401", "Fresh pasteurized milk and cream"),
    "cow milk": ("0401", "Fresh cow milk"),
    "buffalo milk": ("0401", "Fresh buffalo milk"),
    "butter": ("0405", "Dairy butter and spreads"),
    "paneer": ("0406", "Fresh cottage cheese / paneer"),
    "cheese": ("0406", "Processed and cheddar cheese"),
    "ghee": ("0405", "Clarified butter / desi ghee"),
    "amul butter": ("0405", "Pasteurized table butter"),
    "amul milk": ("0401", "Toned and full-cream pasteurized milk"),
    "amul curd": ("0403", "Probiotic curd and dahi"),
    "mother dairy milk": ("0401", "Pasteurized cow and buffalo milk"),
    "rice": ("1006", "Rice, paddy and husked grains"),
    "basmati rice": ("10063020", "Basmati non-parboiled milled rice"),
    "wheat": ("1001", "Wheat and meslin grain"),
    "atta": ("1101", "Wheat flour (whole wheat atta)"),
    "maida": ("1101", "Refined wheat flour (maida)"),
    "sugar": ("1701", "Cane or beet sugar and refined sucrose"),
    "tea": ("0902", "Black and green tea leaves"),
    "coffee": ("0901", "Roasted and raw coffee beans"),
    "black pepper": ("090411", "Whole black pepper (kali mirch)"),
    "turmeric": ("091030", "Turmeric rhizomes and powder (haldi)"),
    "haldi": ("091030", "Turmeric powder and rhizomes"),
    "cumin": ("090931", "Cumin seeds (jeera)"),
    "jeera": ("090931", "Cumin seeds (jeera)"),
    "red chilli": ("090422", "Crushed and ground red chillies"),
    "mustard oil": ("1514", "Mustard seed edible oil"),
    "sunflower oil": ("1512", "Refined sunflower seed cooking oil"),

    # Biscuits, Bakery & FMCG (Chapter 19, 21, 25)
    "parle g": ("1905", "Glucose biscuits and bakery products"),
    "parleg": ("1905", "Glucose biscuits and bakery products"),
    "oreo": ("1905", "Cream biscuits and cookies"),
    "biscuit": ("1905", "Biscuits, cookies and bakery products"),
    "biscuits": ("1905", "Biscuits, cookies and bakery products"),
    "maggi": ("1902", "Instant noodles and pasta"),
    "noodles": ("1902", "Instant noodles and pasta"),
    "tata salt": ("2501", "Iodized vacuum evaporated salt"),
    "salt": ("2501", "Common salt and iodized salt"),

    # Personal Care & Pharma (Chapter 33, 34, 38, 96)
    "soap": ("340111", "Toilet soaps and bathing cleansing bars"),
    "bathing soap": ("340111", "Toilet bathing soap bars"),
    "body wash": ("340130", "Liquid soaps and body wash cleansers"),
    "shampoo": ("330510", "Hair shampoos and cleansers"),
    "toothpaste": ("330610", "Dental toothpastes and dentifrices"),
    "colgate": ("3306", "Toothpaste and oral hygiene dental creams"),
    "perfume": ("330300", "Perfumes and toilet waters"),
    "deodorant": ("330720", "Personal deodorants and anti-perspirants"),
    "sunscreen": ("330499", "Sun protection lotions and sunscreen creams"),
    "sanitary pad": ("961900", "Sanitary napkins and menstrual hygiene pads"),
    "sanitary pads": ("961900", "Sanitary napkins and menstrual hygiene pads"),
    "diaper": ("961900", "Baby and adult diapers and disposable pads"),
    "diapers": ("961900", "Baby and adult diapers and disposable pads"),
    "dettol": ("3808", "Antiseptic disinfectant liquid and soaps"),
    "lifebuoy": ("3401", "Soap and cleansing bars"),
    "hand sanitizer": ("380894", "Alcohol-based antiseptic hand disinfectants"),

    # Building Materials & Hardware
    "cement": ("252329", "Portland cement and building binders"),
    "tmt bars": ("72142090", "TMT construction rebars and steel rods"),
    "sariya": ("72142090", "TMT steel sariya for concrete reinforcement"),
    "tiles": ("6907", "Ceramic and vitrified paving tiles"),
    "plywood": ("441233", "Hardwood and softwood interior plywood"),
    "paint": ("320910", "Acrylic emulsion wall paints"),

    # Automotive & Tyres
    "car tyre": ("401110", "Pneumatic rubber tyres for passenger motor cars"),
    "tyre": ("4011", "New pneumatic rubber tyres"),
    "tire": ("4011", "New pneumatic rubber tyres"),
    "car battery": ("850710", "Lead-acid starting automotive storage batteries"),
    "helmet": ("650610", "Protective safety helmets for two-wheelers"),

    # Indian food, grocery & beverages
    "samosa": ("21069099", "Indian snacks and namkeen (food preparations n.e.s.)"),
    "kachori": ("21069099", "Indian snacks and namkeen (food preparations n.e.s.)"),
    "namkeen": ("21069099", "Indian snacks and namkeen (food preparations n.e.s.)"),
    "bhujia": ("21069099", "Indian snacks and namkeen (food preparations n.e.s.)"),
    "kurkure": ("21069099", "Indian snacks and namkeen (food preparations n.e.s.)"),
    "haldiram": ("21069099", "Indian snacks and namkeen (food preparations n.e.s.)"),
    "haldirams": ("21069099", "Indian snacks and namkeen (food preparations n.e.s.)"),
    "mixture": ("21069099", "Indian snacks and namkeen (food preparations n.e.s.)"),
    "chakli": ("21069099", "Indian snacks and namkeen (food preparations n.e.s.)"),
    "mathri": ("21069099", "Indian snacks and namkeen (food preparations n.e.s.)"),
    "pav": ("19059090", "Bread, buns and bakery products"),
    "bun": ("19059090", "Bread, buns and bakery products"),
    "bread": ("19059090", "Bread, buns and bakery products"),
    "toast": ("19059090", "Bread, buns and bakery products"),
    "khari": ("19059090", "Bread, buns and bakery products"),
    "rusk": ("19054000", "Rusks, toasted bread and similar toasted products"),
    "papad": ("19059040", "Papad"),
    "papads": ("19059040", "Papad"),
    "papadum": ("19059040", "Papad"),
    "chips": ("20052000", "Potato chips and wafers"),
    "lays": ("20052000", "Potato chips and wafers"),
    "wafers": ("20052000", "Potato chips and wafers"),
    "potato chips": ("20052000", "Potato chips and wafers"),
    "pickle": ("20019000", "Pickles and vegetables preserved in vinegar"),
    "achar": ("20019000", "Pickles and vegetables preserved in vinegar"),
    "mithai": ("21069099", "Indian sweets and food preparations n.e.s."),
    "sweets": ("21069099", "Indian sweets and food preparations n.e.s."),
    "rasgulla": ("21069099", "Indian sweets and food preparations n.e.s."),
    "gulab jamun": ("21069099", "Indian sweets and food preparations n.e.s."),
    "ladoo": ("21069099", "Indian sweets and food preparations n.e.s."),
    "laddu": ("21069099", "Indian sweets and food preparations n.e.s."),
    "soan papdi": ("21069099", "Indian sweets and food preparations n.e.s."),
    "paan masala": ("21069099", "Indian sweets and food preparations n.e.s."),
    "toffee": ("17049090", "Sugar confectionery not containing cocoa"),
    "candy": ("17049090", "Sugar confectionery not containing cocoa"),
    "curd": ("040390", "Curd, lassi, buttermilk and fermented milk"),
    "dahi": ("040390", "Curd, lassi, buttermilk and fermented milk"),
    "lassi": ("040390", "Curd, lassi, buttermilk and fermented milk"),
    "buttermilk": ("040390", "Curd, lassi, buttermilk and fermented milk"),
    "chaas": ("040390", "Curd, lassi, buttermilk and fermented milk"),
    "chhaas": ("040390", "Curd, lassi, buttermilk and fermented milk"),
    "besan": ("110620", "Gram flour (besan)"),
    "poha": ("190410", "Flattened/beaten rice (poha)"),
    "dal": ("0713", "Dried shelled pulses (dal)"),
    "daal": ("0713", "Dried shelled pulses (dal)"),
    "jaggery": ("1701", "Jaggery and cane sugar"),
    "gur": ("1701", "Jaggery and cane sugar"),
    "frooti": ("220299", "Mango/fruit pulp based non-alcoholic beverages"),
    "slice": ("220299", "Mango/fruit pulp based non-alcoholic beverages"),
    "appy": ("220299", "Mango/fruit pulp based non-alcoholic beverages"),
    "maaza": ("220299", "Mango/fruit pulp based non-alcoholic beverages"),
    "chai": ("0902", "Tea"),
    "ice cream": ("210500", "Ice cream and edible ice"),
    "kulfi": ("210500", "Ice cream and edible ice"),
    "chocolate": ("1806", "Chocolate and cocoa food preparations"),
    "dairy milk": ("1806", "Chocolate and cocoa food preparations"),
    "kitkat": ("1806", "Chocolate and cocoa food preparations"),
    "ketchup": ("2103", "Sauces and ketchup"),
    "sauce": ("2103", "Sauces and ketchup"),
    "pasta": ("1902", "Instant noodles and pasta"),
}



_SYNONYMS: dict[str, str] = {
    "cococola": "coca cola", "coco cola": "coca cola", "softdrink": "soft drink", "sunglass": "sunglasses",
    "sun glasses": "sunglasses", "sun glass": "sunglasses", "laptopbag": "laptop bag", "tshirt": "t-shirt",
    "smart phone": "smartphone", "cellphone": "mobile phone", "cell phone": "mobile phone",
    "ear phones": "earphones", "ear phone": "earphones", "head phone": "headphones", "powerbank": "power bank",
    "motor oil": "engine oil",
}


def _resolve_commercial_intent(text: str) -> tuple[Optional[str], Optional[str]]:
    """A HINT for a phrase: brand / compound spelling variants / keyword-table heading. It is only one candidate
    among several that the classifier weighs (it used to be the final answer, which let one word hijack a phrase)."""
    if not text:
        return None, None
    clean_lower = normalise_trade_text(text.strip().lower())  # SS304 -> stainless steel, chawal -> rice
    for syn_k, syn_v in sorted(_SYNONYMS.items(), key=lambda x: len(x[0]), reverse=True):
        clean_lower = re.sub(rf"\b{re.escape(syn_k)}\b", syn_v, clean_lower)
    for brand_key in sorted(_POPULAR_COMMERCIAL_BRANDS.keys(), key=len, reverse=True):
        if re.search(rf"\b{re.escape(brand_key)}\b", clean_lower):
            return _POPULAR_COMMERCIAL_BRANDS[brand_key]
    kw = keyword_hint(clean_lower)
    if kw:
        return kw, f"Trade keyword for '{text}'"
    return None, None


def format_title(code: str, desc: str, aliases: str = "", heading_desc: str = "") -> str:
    """Display title from a bare description (used only when a row has no hierarchy title)."""
    c = code.strip()
    if c in _TRADE_TITLE_OVERRIDES:
        return _TRADE_TITLE_OVERRIDES[c]
    clean = (desc or "").strip()
    if ":" in clean:
        clean = clean.split(":")[-1].strip() or clean.replace(":", "")
    if clean.isupper() and len(clean) > 3:
        clean = clean.title()
    return clean[:100]


_SEL = (
    "c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases, "
    "c.is_group, c.title, c.path, c.parent, "
    "EXISTS(SELECT 1 FROM hsn_codes s WHERE s.parent = c.code) AS has_kids"
)


def _title(r: sqlite3.Row) -> str:
    """formatted_title: built from the hierarchy at build time (a fragment such as "Other" carries its parents)."""
    if r["code"] in _TRADE_TITLE_OVERRIDES:
        return _TRADE_TITLE_OVERRIDES[r["code"]]
    return (r["title"] or format_title(r["code"], r["description"]))[:200]


def _item(r: sqlite3.Row, aliases: Optional[str] = None) -> "HsnItemOut":
    return HsnItemOut(
        code=r["code"],
        description=r["description"],
        formatted_title=_title(r),
        level=r["level"],
        chapter=r["chapter"],
        heading=r["heading"],
        subheading=r["subheading"],
        category=r["category"],
        is_service=bool(r["is_service"]),
        aliases=aliases if aliases is not None else (r["aliases"] or ""),
        has_children=bool(r["has_kids"]),
        path=r["path"] or None,
    )


class HsnItemOut(BaseModel):
    code: str
    description: str
    formatted_title: str
    level: int
    chapter: str
    heading: str
    subheading: str
    category: str
    is_service: bool = False
    aliases: str = ""
    has_children: bool = False
    path: Optional[str] = None
    # Search-only extras (optional, so every other endpoint keeps returning the same shape).
    display_title: Optional[str] = None
    context: Optional[str] = None
    parent_description: Optional[str] = None
    also_codes: Optional[list[str]] = None
    recommended: Optional[bool] = None


class HsnAlternative(BaseModel):
    code: str
    title: str


class HsnSuggestionOut(BaseModel):
    code: str
    description: str
    formatted_title: str
    category: str
    chapter: str
    chapter_name: str
    confidence: str = "high"  # high | medium | low
    reason: Optional[str] = None
    path: Optional[str] = None
    source: Optional[str] = None  # code | cache | ai | lexical
    # low confidence = a suggestion for the user to confirm, not an answer
    needs_confirmation: bool = False
    alternatives: list[HsnAlternative] = []


@router.get("/suggest", response_model=Optional[HsnSuggestionOut])
async def suggest_hsn_for_product(
    text: Annotated[str, Query(min_length=2, max_length=150)],
) -> Optional[HsnSuggestionOut]:
    """Classification assistant: retrieves candidate tariff lines for a product phrase (full phrase, head noun,
    learned cache), lets the AI choose among them with their full hierarchy, then validates the pick."""
    return await suggest_hsn(text)


async def suggest_hsn(text: str, use_ai: bool = True) -> Optional[HsnSuggestionOut]:
    """Shared implementation of GET /hsn/suggest (also used by the AI chat).

    ``use_ai=False`` skips the LLM (text matching only, always ``low`` confidence)."""
    clean = (text or "").strip().lower()
    if not clean:
        return None
    code, reason = _resolve_commercial_intent(clean)
    hints = [(code, reason or "")] if code else None
    res = await classify_shared(clean, hints, use_ai=use_ai)
    if not res.code:
        return None

    conn = _get_db()
    try:
        row = conn.execute(f"SELECT {_SEL} FROM hsn_codes c WHERE c.code = ? AND c.is_group = 0", (res.code,)).fetchone()
        if row is None:
            return None
        ch_code = row["chapter"]
        confidence, why = res.confidence, res.reason
        if confidence == "high" and row["code"][:4] in _PIPE_HEADINGS and not _PIPE_PROCESS_RE.search(clean):
            # 7304 (seamless) vs 7306 (welded) is a different tariff line and rate: do not claim certainty.
            confidence = "medium"
            why = f"{why}; seamless or welded is not stated, please confirm the pipe type"
        if confidence == "low":
            why = f"Unconfirmed suggestion, please check: {why}"
        alts = []
        if res.alternatives:
            codes = [a["code"] for a in res.alternatives if a["code"] != row["code"]][:3]
            if codes:
                ph = ", ".join("?" for _ in codes)
                titles = {r["code"]: r for r in conn.execute(f"SELECT {_SEL} FROM hsn_codes c WHERE c.code IN ({ph})", codes)}
                alts = [HsnAlternative(code=c, title=_title(titles[c])) for c in codes if c in titles]
        return HsnSuggestionOut(
            code=row["code"],
            description=row["description"],
            formatted_title=_title(row),
            category=row["category"],
            chapter=ch_code,
            chapter_name=HS_CHAPTERS.get(ch_code, row["category"]),
            confidence=confidence,
            reason=why,
            path=row["path"] or None,
            source=res.source,
            needs_confirmation=confidence == "low",
            alternatives=alts,
        )
    finally:
        conn.close()


def _enrich_text_search(
    cur: sqlite3.Cursor, items: list[HsnItemOut], query: str, suggested: Optional[str] = None
) -> list[HsnItemOut]:
    """Collapse 6/8-digit duplicates, label look-alike rows ("Apples - fresh" vs "Apples - dried"), recommend one."""
    if not items:
        return items
    prefixes = {c[:n] for it in items for c in [it.code] for n in (2, 4, 6) if len(c) > n}
    rows = [it.model_dump() for it in items]
    for r in rows:
        r["display_title"] = r["formatted_title"]
    placeholders = ", ".join("?" for _ in prefixes)
    descriptions = {
        r["code"]: r["description"]
        for r in cur.execute(
            f"SELECT code, description FROM hsn_codes WHERE code IN ({placeholders})", tuple(prefixes)
        ).fetchall()
    } if prefixes else {}
    try:
        enriched = enrich_search_results(rows, descriptions, query, suggested)
    except Exception as e:
        logger.warning("HSN search enrichment failed, returning plain results: %s", e)
        return items
    return [HsnItemOut(**r) for r in enriched]


def _fetch_items(cur: sqlite3.Cursor, codes: list[str], aliases: Optional[dict[str, str]] = None) -> list[HsnItemOut]:
    """Items for ``codes`` in the given order; unknown codes and group rows are dropped."""
    if not codes:
        return []
    ph = ", ".join("?" for _ in codes)
    by_code = {r["code"]: r for r in cur.execute(f"SELECT {_SEL} FROM hsn_codes c WHERE c.code IN ({ph}) AND c.is_group = 0", codes)}
    return [_item(by_code[c], (aliases or {}).get(c)) for c in codes if c in by_code]


def _flatten_children(cur: sqlite3.Cursor, parent: str, limit: int) -> list[sqlite3.Row]:
    """Direct children of ``parent`` with group rows looked through (their children are listed instead)."""
    return cur.execute(
        f"""
        WITH RECURSIVE d(code, grp) AS (
            SELECT code, is_group FROM hsn_codes WHERE parent = ?
            UNION ALL
            SELECT c.code, c.is_group FROM hsn_codes c JOIN d ON c.parent = d.code WHERE d.grp = 1
        )
        SELECT {_SEL} FROM hsn_codes c WHERE c.code IN (SELECT code FROM d WHERE grp = 0)
        ORDER BY c.code ASC LIMIT ?
        """,
        (parent, limit),
    ).fetchall()


def _search_impl(q: str, limit: int) -> tuple[list[HsnItemOut], bool]:
    """The synchronous search: (items, weak) where ``weak`` means text matching found nothing usable on its own
    (the caller may warm the learned cache in the background). Never calls the model."""
    clean = q.strip().replace("'", "").replace('"', "")
    if not clean:
        return [], False
    conn = _get_db()
    cur = conn.cursor()
    try:
        # Code browsing only for a query that IS a code ("0304", "0304 10", "7306.30").
        if is_code_query(clean):
            digits = "".join(c for c in clean if c.isdigit())
            rows = cur.execute(
                f"SELECT {_SEL} FROM hsn_codes c WHERE c.code LIKE ? AND c.is_group = 0 "
                "ORDER BY c.level ASC, c.code ASC LIMIT ?",
                (digits + "%", limit),
            ).fetchall()
            if len(digits) in (5, 7, 8):  # a group row itself is not selectable: show the lines under it
                exact = cur.execute("SELECT code FROM hsn_codes WHERE code = ? AND is_group = 1", (digits,)).fetchone()
                if exact:
                    rows = _flatten_children(cur, digits, limit)
            items = [_item(r) for r in rows]
            for it in items:
                it.display_title = it.formatted_title
            return items, False
        if not is_plausible_query(clean):
            return [], False

        key = norm_query(clean)
        terms = query_terms(key) or query_terms(clean)
        ordered: list[str] = []
        alias_note: dict[str, str] = {}

        def add(codes: list[str]) -> None:
            for c in codes:
                if c not in ordered:
                    ordered.append(c)

        cached = learned_get(key)
        suggested = cached["code"] if cached and cached.get("code") else None
        if suggested:
            add([suggested])
        # The lexicon (synchronous, no model call) knows many Indian trade names: search its English name too.
        lex_phrase: list[str] = []
        try:
            from services import ai_lexicon

            canon = ai_lexicon.canonical(clean)
            if canon and canon.strip().lower() != clean.lower():
                lex_phrase = [r["code"] for r in lexical_rows(query_terms(canon), limit, prefix_last=True)]
            lt = ai_lexicon.term(clean)
            lex_hsn = resolve_code(lt.hsn) if lt is not None and lt.hsn else None
            if lex_hsn and not suggested:
                add([lex_hsn])
        except Exception as exc:  # noqa: BLE001
            logger.debug("lexicon lookup failed: %s", exc)
        phrase = [r["code"] for r in lexical_rows(terms, limit * 2, prefix_last=True)]
        phrase = list(dict.fromkeys(lex_phrase + phrase))
        # rows whose own title is the query rank first, then rows that start with it, then the rest (bm25 order)
        qn = " ".join(terms)
        if phrase:
            ph = ", ".join("?" for _ in phrase)
            meta = {r["code"]: (r["label"] or "", r["path"] or "") for r in cur.execute(f"SELECT code, label, path FROM hsn_codes WHERE code IN ({ph})", phrase)}
            titles = {c: m[0].lower() for c, m in meta.items()}
            qwords = set(terms)
            def tier(c: str) -> tuple[int, int]:
                t = re.sub(r"[^\w\s]", " ", titles.get(c, "")).split()
                t = " ".join(_stem_word(w) for w in t)
                qs = " ".join(_stem_word(w) for w in terms)
                # unless the query names a processed form, the unprocessed line is the likelier one
                processed = {w for w in _PROCESSED_WORDS if w in meta[c][1].lower()} - qwords
                return (0 if t == qs else 1 if t.startswith(qs) else 2, 1 if processed else 0)
            phrase.sort(key=tier)  # stable: keeps bm25 order inside a tier
        add(phrase)
        hint_code, hint_reason = _resolve_commercial_intent(clean)
        if hint_code and len(hint_code) in (4, 6, 8):
            kids = [r["code"] for r in cur.execute(
                "SELECT code FROM hsn_codes WHERE code LIKE ? AND is_group = 0 ORDER BY level ASC, code ASC LIMIT 12",
                (hint_code + "%",))]
            add(kids)
            if hint_reason:
                for c in kids:
                    alias_note[c] = f"Trade match: {clean} ({hint_reason})"
        weak = not phrase and not suggested
        items = _fetch_items(cur, ordered[:limit], alias_note)
        return _enrich_text_search(cur, items, qn or key, suggested=suggested), weak
    finally:
        conn.close()


_PROCESSED_WORDS = ("dried", "dehydrated", "powder", "flour", "prepared", "preserved", "frozen", "extract", "oil", "seeds", "waste", "roasted", "juice", "sauce", "paste")


def _stem_word(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def search_hsn(
    q: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[HsnItemOut]:
    """Instant search across all HSN/SAC codes, descriptions and the learned cache (sync; no model call)."""
    return _search_impl(q, limit)[0]


@router.get("/search", response_model=list[HsnItemOut])
async def search_hsn_endpoint(
    q: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[HsnItemOut]:
    """Instant search over descriptions, hierarchy and learned results. Never waits for the model; when the text
    matches nothing it starts a background classification so the next search for the phrase is meaningful."""
    items, weak = _search_impl(q, limit)
    if weak:
        try:
            warm_in_background(q)
        except Exception as exc:  # noqa: BLE001
            logger.debug("HSN warm-up not started: %s", exc)
    return items


class HsnConfirmIn(BaseModel):
    text: Annotated[str, Field(min_length=2, max_length=150)]
    code: Annotated[str, Field(min_length=4, max_length=8)]


@router.post("/confirm")
async def confirm_hsn_choice(body: HsnConfirmIn, user: CurrentUser) -> dict[str, Any]:
    """The user picked ``code`` for ``text``. This is one VOTE (one per user per phrase), not a global answer: it
    becomes a high-confidence result only with enough distinct established accounts and no model contradiction."""
    return await confirm_learned(body.text, body.code, user.id, eligible=vote_eligible(user))


@router.get("/children", response_model=list[HsnItemOut])
def get_hsn_children(
    parent: Annotated[str, Query(min_length=2, max_length=8)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[HsnItemOut]:
    """Direct children of a chapter, heading, subheading or tariff line (group rows are looked through)."""
    p = parent.strip()
    conn = _get_db()
    cur = conn.cursor()
    try:
        return [_item(r) for r in _flatten_children(cur, p, limit)]
    finally:
        conn.close()


@router.get("/categories")
def get_hsn_categories() -> list[dict[str, Any]]:
    """Return top-level categories with their chapters and total items count."""
    conn = _get_db()
    cur = conn.cursor()
    try:
        rows = cur.execute(
            """
            SELECT c.chapter, c.category, c.description,
                   (SELECT count(*) FROM hsn_codes total WHERE total.chapter = c.chapter AND total.is_group = 0) as item_count
            FROM hsn_codes c
            WHERE c.level = 2
            ORDER BY c.chapter ASC
            """
        ).fetchall()

        categories_map: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            categories_map.setdefault(r["category"], []).append(
                {"chapter": r["chapter"], "description": r["description"], "total_items": r["item_count"]}
            )
        return [
            {
                "category": cat,
                "chapters": chapters,
                "count": len(chapters),
                "total_items": sum(ch["total_items"] for ch in chapters),
            }
            for cat, chapters in categories_map.items()
        ]
    finally:
        conn.close()


@router.get("/code/{code}", response_model=HsnItemOut)
def get_hsn_by_code(code: str) -> HsnItemOut:
    """Fetch details of a specific HSN code."""
    c = code.strip()
    item = lookup_hsn_item(c)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"HSN code {c} not found in master database",
        )
    return item


def lookup_hsn_item(c: str) -> Optional[HsnItemOut]:
    """Shared implementation of GET /hsn/code/{code}; None when the code is unknown or only a group row."""
    conn = _get_db()
    try:
        row = conn.execute(f"SELECT {_SEL} FROM hsn_codes c WHERE c.code = ? AND c.is_group = 0 LIMIT 1", (c,)).fetchone()
        return _item(row) if row else None
    finally:
        conn.close()

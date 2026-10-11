"""HSN & SAC master classification endpoints.

Provides hierarchical navigation and instant full-text search across all 22,616+
official tariff codes extracted from HSN_SAC.xlsx.
"""

import os
import re
import sqlite3
import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from services.hsn_service import (
    HS_CHAPTERS,
    _COMMON_HEADING_KEYWORDS,
    classify_hsn_with_ai,
    normalize_hsn,
    predict_hsn_from_text,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hsn", tags=["hsn"])

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "hsn_master.db",
)


def _get_db():
    if not os.path.exists(DB_PATH):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HSN master database is not built. Run scripts/build_hsn_database.py",
        )
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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


def _resolve_commercial_intent(text: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve consumer search phrases, brands, compound terms, and common typos into HSN codes."""
    if not text:
        return None, None
    clean_lower = text.strip().lower()

    # 1. Normalization of colloquial typos and compound variants
    synonyms: dict[str, str] = {
        "cococola": "coca cola",
        "coco cola": "coca cola",
        "softdrink": "soft drink",
        "sunglass": "sunglasses",
        "sun glasses": "sunglasses",
        "sun glass": "sunglasses",
        "laptopbag": "laptop bag",
        "tshirt": "t-shirt",
        "smart phone": "smartphone",
        "cellphone": "mobile phone",
        "cell phone": "mobile phone",
        "ear phones": "earphones",
        "ear phone": "earphones",
        "head phone": "headphones",
        "powerbank": "power bank",
        "motor oil": "engine oil",
    }
    for syn_k, syn_v in sorted(synonyms.items(), key=lambda x: len(x[0]), reverse=True):
        clean_lower = re.sub(rf"\b{re.escape(syn_k)}\b", syn_v, clean_lower)

    # 2. Exact or substring match in commercial brands dictionary
    for brand_key in sorted(_POPULAR_COMMERCIAL_BRANDS.keys(), key=len, reverse=True):
        if re.search(rf"\b{re.escape(brand_key)}\b", clean_lower):
            return _POPULAR_COMMERCIAL_BRANDS[brand_key]

    # 3. Deterministic keyword table match
    predicted = predict_hsn_from_text(clean_lower)
    if predicted:
        return predicted, f"Trade classification for '{text}'"

    # 4. Typo tolerance: fuzzy match tokens / full query against brand and keyword keys
    from difflib import get_close_matches

    brand_keys = list(_POPULAR_COMMERCIAL_BRANDS.keys())
    kw_keys = list(_COMMON_HEADING_KEYWORDS.keys())
    candidates = [clean_lower] + [t for t in re.findall(r"[a-z0-9]+", clean_lower) if len(t) >= 4]
    for cand in dict.fromkeys(candidates):
        for key in get_close_matches(cand, brand_keys, n=1, cutoff=0.8):
            return _POPULAR_COMMERCIAL_BRANDS[key][0], f"Closest match '{key}' for '{text}'"
        for key in get_close_matches(cand, kw_keys, n=1, cutoff=0.8):
            return _COMMON_HEADING_KEYWORDS[key], f"Closest match '{key}' for '{text}'"

    return None, None



def format_title(code: str, desc: str, aliases: str = "", heading_desc: str = "") -> str:
    """Format clean commercial trade title.

    ``heading_desc`` (optional) is the parent 4-digit heading description, used when the
    code's own description is a meaningless "Other".
    """
    c = code.strip()
    if c in _TRADE_TITLE_OVERRIDES:
        return _TRADE_TITLE_OVERRIDES[c]

    _own = desc.split(":")[-1].strip(" :.-").lower() if desc else ""
    if _own in ("", "other", "others") and len(c) > 4:
        parent = _TRADE_TITLE_OVERRIDES.get(c[:4]) or (heading_desc or "").strip()
        if parent:
            if parent.isupper():
                parent = parent.title()
            return f"Other ({parent})"[:100]

    # If heading matches override
    if len(c) > 4 and c[:4] in _TRADE_TITLE_OVERRIDES:
        parent_name = _TRADE_TITLE_OVERRIDES[c[:4]]
        clean = desc.strip()
        if ":" in clean:
            clean = clean.split(":")[-1].strip()
        if clean.isupper() and len(clean) > 3:
            clean = clean.title()
        if clean and clean.lower() not in parent_name.lower():
            return f"{clean} ({parent_name})"

    clean = desc.strip()
    if ":" in clean:
        clean = clean.split(":")[-1].strip() or clean.replace(":", "")
    if clean.isupper() and len(clean) > 3:
        clean = clean.title()
    return clean[:100]


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


class HsnSuggestionOut(BaseModel):
    code: str
    description: str
    formatted_title: str
    category: str
    chapter: str
    chapter_name: str
    confidence: str = "high"
    reason: Optional[str] = None


@router.get("/suggest", response_model=Optional[HsnSuggestionOut])
async def suggest_hsn_for_product(
    text: Annotated[str, Query(min_length=2, max_length=150)],
) -> Optional[HsnSuggestionOut]:
    """Smart classification assistant: maps consumer products & brands (e.g. 'sun glasses', 'soft drink', 'cococola')
    to standard HSN codes & categories using AI and semantic trade dictionaries."""
    return await suggest_hsn(text)


async def suggest_hsn(text: str) -> Optional[HsnSuggestionOut]:
    """Shared implementation of GET /hsn/suggest (also used by the AI chat)."""
    clean = text.strip().lower()
    if not clean:
        return None

    conn = _get_db()
    cur = conn.cursor()
    code: Optional[str] = None
    reason: Optional[str] = None

    try:
        # Step 1: Check known commercial intent, synonyms & deterministic trade keywords
        code, reason = _resolve_commercial_intent(clean)

        # Step 2: De-spaced exact compound phrase match in FTS (e.g. 'sun glasses' -> 'sunglasses')
        # CRITICAL: Require c.level >= 4 so broad 2-digit chapters (e.g. Chapter 66) never intercept!
        if not code:
            despaced = clean.replace(" ", "")
            if despaced != clean and len(despaced) > 3:
                try:
                    row = cur.execute(
                        """
                        SELECT c.code, c.description, c.category, c.chapter
                        FROM hsn_codes c
                        JOIN hsn_fts f ON c.rowid = f.rowid
                        WHERE hsn_fts MATCH ? AND c.level >= 4
                        ORDER BY c.level DESC
                        LIMIT 1
                        """,
                        (f"{despaced}*",),
                    ).fetchone()
                    if row:
                        code = row["code"]
                        reason = f"Tariff commodity match for '{despaced}'"
                except Exception as e:
                    logger.debug("De-spaced FTS search error: %s", e)

        # Step 3: AI LLM Classification fallback (Ollama)
        if not code:
            try:
                ai_code = await classify_hsn_with_ai(clean)
                if ai_code:
                    code = ai_code
                    reason = f"AI classified '{clean}' into international customs nomenclature"
            except Exception as e:
                logger.debug("AI classification fallback failed: %s", e)

        # Step 4: Strict token-level fallback: ALL tokens must match (AND) with level >= 4
        if not code:
            clean_alnum = re.sub(r"[^\w\s]", " ", clean).strip()
            tokens = [t for t in clean_alnum.split() if len(t) > 2]
            if len(tokens) >= 2:
                token_query = " AND ".join(f"{t}*" for t in tokens[:3])
                try:
                    row = cur.execute(
                        """
                        SELECT c.code, c.description, c.category, c.chapter
                        FROM hsn_codes c
                        JOIN hsn_fts f ON c.rowid = f.rowid
                        WHERE hsn_fts MATCH ? AND c.level >= 4
                        ORDER BY c.level DESC
                        LIMIT 1
                        """,
                        (token_query,),
                    ).fetchone()
                    if row:
                        code = row["code"]
                        reason = f"Tariff match for '{clean}'"
                except Exception as e:
                    logger.debug("Token fallback search error: %s", e)

        if not code:
            return None

        # Step 5: Fetch official database metadata for the code
        norm_code = normalize_hsn(code) or code
        row = cur.execute(
            """
            SELECT code, description, level, chapter, heading, subheading, category, is_service, aliases
            FROM hsn_codes
            WHERE code = ?
            LIMIT 1
            """,
            (norm_code,),
        ).fetchone()

        # If not found directly, walk down prefixes (8 -> 6 -> 4)
        if not row and len(norm_code) > 4:
            for plen in (8, 6, 4):
                if plen >= len(norm_code):
                    continue
                row = cur.execute(
                    """
                    SELECT code, description, level, chapter, heading, subheading, category, is_service, aliases
                    FROM hsn_codes
                    WHERE code = ?
                    LIMIT 1
                    """,
                    (norm_code[:plen],),
                ).fetchone()
                if row:
                    break

        if not row:
            return None

        ch_code = row["chapter"]
        ch_name = HS_CHAPTERS.get(ch_code, row["category"])
        head_row = cur.execute(
            "SELECT description FROM hsn_codes WHERE code = ? LIMIT 1", (row["code"][:4],)
        ).fetchone()
        head_desc = head_row["description"] if head_row else ""

        return HsnSuggestionOut(
            code=row["code"],
            description=row["description"],
            formatted_title=format_title(row["code"], row["description"], row["aliases"] or "", head_desc),
            category=row["category"],
            chapter=ch_code,
            chapter_name=ch_name,
            confidence="high" if reason and "AI" not in reason else "ai_suggested",
            reason=reason,
        )
    finally:
        conn.close()


@router.get("/search", response_model=list[HsnItemOut])
def search_hsn(
    q: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[HsnItemOut]:
    """Instant search across all 22,616 HSN/SAC codes, descriptions, and commercial trade terms."""
    clean = q.strip().replace("'", "").replace('"', "")
    if not clean:
        return []

    conn = _get_db()
    cur = conn.cursor()
    digits = "".join(c for c in clean if c.isdigit())
    clean_lower = clean.lower()

    try:
        # Step 1: Check known commercial intent (e.g. 'sun glasses', 'soft drink', 'cococola', 'sneakers')
        matched_brand_code, matched_brand_reason = _resolve_commercial_intent(clean_lower)

        if matched_brand_code:
            norm_code = normalize_hsn(matched_brand_code) or matched_brand_code
            # Also include any directly related complementary codes (e.g. if searching sunglasses, include raw glass blanks 70159020)
            extra_codes = []
            if norm_code.startswith("9004"):
                extra_codes = ["70159020"]
            elif norm_code == "70159020":
                extra_codes = ["90041000"]

            placeholders = ", ".join("?" for _ in [norm_code, *extra_codes])
            rows = cur.execute(
                f"""
                SELECT c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases,
                       (SELECT count(*) FROM hsn_codes sub WHERE sub.code LIKE c.code || "%" AND sub.code != c.code LIMIT 1) as child_cnt
                FROM hsn_codes c
                WHERE c.code IN ({placeholders}) OR c.code LIKE ? OR c.heading = ?
                ORDER BY (c.code = ?) DESC, (c.code IN ({placeholders})) DESC, c.level ASC, c.code ASC
                LIMIT ?
                """,
                (*[norm_code, *extra_codes], norm_code + "%", norm_code[:4], norm_code, *[norm_code, *extra_codes], limit),
            ).fetchall()

            return [
                HsnItemOut(
                    code=r["code"],
                    description=r["description"],
                    formatted_title=format_title(r["code"], r["description"], r["aliases"]),
                    level=r["level"],
                    chapter=r["chapter"],
                    heading=r["heading"],
                    subheading=r["subheading"],
                    category=r["category"],
                    is_service=bool(r["is_service"]),
                    aliases=f"Trade match: {clean} ({matched_brand_reason})" if matched_brand_reason else (r["aliases"] or ""),
                    has_children=bool(r["child_cnt"] > 0),
                )
                for r in rows
            ]

        # Step 2: Code prefix match if query is numeric digits
        if len(digits) >= 2:
            rows = cur.execute(
                """
                SELECT c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases,
                       (SELECT count(*) FROM hsn_codes sub WHERE sub.code LIKE c.code || "%" AND sub.code != c.code LIMIT 1) as child_cnt
                FROM hsn_codes c
                WHERE c.code LIKE ?
                ORDER BY c.level ASC, c.code ASC
                LIMIT ?
                """,
                (digits + "%", limit),
            ).fetchall()
        else:
            # Step 3: Full text search over descriptions and aliases with compound de-spacing
            despaced = clean.replace(" ", "")
            if despaced != clean and len(despaced) > 3:
                fts_query = f'"{clean}"* OR {despaced}*'
            else:
                fts_query = f"{clean}*"

            try:
                rows = cur.execute(
                    """
                    SELECT c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases,
                           (SELECT count(*) FROM hsn_codes sub WHERE sub.code LIKE c.code || "%" AND sub.code != c.code LIMIT 1) as child_cnt
                    FROM hsn_codes c
                    JOIN hsn_fts f ON c.rowid = f.rowid
                    WHERE hsn_fts MATCH ?
                    ORDER BY (c.level >= 4) DESC, c.level ASC
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
            except Exception:
                rows = []

            # Step 4: Token-level fallback if full phrase gave 0 results (using strict AND with level >= 4)
            if not rows:
                clean_alnum = re.sub(r"[^\w\s]", " ", clean).strip()
                tokens = [t for t in clean_alnum.split() if len(t) > 2]
                if tokens:
                    token_query = " AND ".join(f"{t}*" for t in tokens[:3])
                    try:
                        rows = cur.execute(
                            """
                            SELECT c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases,
                                   (SELECT count(*) FROM hsn_codes sub WHERE sub.code LIKE c.code || "%" AND sub.code != c.code LIMIT 1) as child_cnt
                            FROM hsn_codes c
                            JOIN hsn_fts f ON c.rowid = f.rowid
                            WHERE hsn_fts MATCH ? AND c.level >= 4
                            ORDER BY c.level DESC
                            LIMIT ?
                            """,
                            (token_query, limit),
                        ).fetchall()
                    except Exception:
                        rows = []

        return [
            HsnItemOut(
                code=r["code"],
                description=r["description"],
                formatted_title=format_title(r["code"], r["description"], r["aliases"]),
                level=r["level"],
                chapter=r["chapter"],
                heading=r["heading"],
                subheading=r["subheading"],
                category=r["category"],
                is_service=bool(r["is_service"]),
                aliases=r["aliases"] or "",
                has_children=bool(r["child_cnt"] > 0),
            )
            for r in rows
        ]
    finally:
        conn.close()


@router.get("/children", response_model=list[HsnItemOut])
def get_hsn_children(
    parent: Annotated[str, Query(min_length=2, max_length=8)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[HsnItemOut]:
    """Fetch direct children for a chapter (2 digits), heading (4 digits), or subheading (6 digits)."""
    p = parent.strip()
    conn = _get_db()
    cur = conn.cursor()

    try:
        if len(p) == 2:
            # Headings under chapter (4 digits)
            rows = cur.execute(
                """
                SELECT c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases,
                       (SELECT count(*) FROM hsn_codes sub WHERE sub.heading = c.code AND sub.level > 4 LIMIT 1) as child_cnt
                FROM hsn_codes c
                WHERE c.chapter = ? AND c.level = 4
                ORDER BY c.code ASC
                LIMIT ?
                """,
                (p, limit),
            ).fetchall()
        elif len(p) == 4:
            # Subheadings (6 digits) or direct tariff items (8 digits)
            l6_count = cur.execute(
                "SELECT count(*) FROM hsn_codes WHERE heading = ? AND level = 6", (p,)
            ).fetchone()[0]

            if l6_count > 0:
                rows = cur.execute(
                    """
                    SELECT c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases,
                           (SELECT count(*) FROM hsn_codes sub WHERE sub.code LIKE c.code || "%" AND sub.level = 8 LIMIT 1) as child_cnt
                    FROM hsn_codes c
                    WHERE c.heading = ? AND c.level = 6
                    ORDER BY c.code ASC
                    LIMIT ?
                    """,
                    (p, limit),
                ).fetchall()
            else:
                rows = cur.execute(
                    """
                    SELECT c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases,
                           0 as child_cnt
                    FROM hsn_codes c
                    WHERE c.heading = ? AND c.level = 8
                    ORDER BY c.code ASC
                    LIMIT ?
                    """,
                    (p, limit),
                ).fetchall()
        elif len(p) == 6:
            # 8-digit tariff items under 6-digit subheading
            rows = cur.execute(
                """
                SELECT c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases,
                       0 as child_cnt
                FROM hsn_codes c
                WHERE c.code LIKE ? AND c.level = 8
                ORDER BY c.code ASC
                LIMIT ?
                """,
                (p + "%", limit),
            ).fetchall()
        else:
            rows = cur.execute(
                """
                SELECT c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases,
                       0 as child_cnt
                FROM hsn_codes c
                WHERE c.code LIKE ? AND c.code != ?
                ORDER BY c.level ASC, c.code ASC
                LIMIT ?
                """,
                (p + "%", p, limit),
            ).fetchall()

        return [
            HsnItemOut(
                code=r["code"],
                description=r["description"],
                formatted_title=format_title(r["code"], r["description"], r["aliases"]),
                level=r["level"],
                chapter=r["chapter"],
                heading=r["heading"],
                subheading=r["subheading"],
                category=r["category"],
                is_service=bool(r["is_service"]),
                aliases=r["aliases"] or "",
                has_children=bool(r["child_cnt"] > 0),
            )
            for r in rows
        ]
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
                   (SELECT count(*) FROM hsn_codes total WHERE total.chapter = c.chapter) as item_count
            FROM hsn_codes c
            WHERE c.level = 2
            ORDER BY c.chapter ASC
            """
        ).fetchall()

        categories_map: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            cat = r["category"]
            categories_map.setdefault(cat, []).append(
                {
                    "chapter": r["chapter"],
                    "description": r["description"],
                    "total_items": r["item_count"],
                }
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
    """Shared implementation of GET /hsn/code/{code}; None when the code is unknown."""
    conn = _get_db()
    cur = conn.cursor()
    try:
        row = cur.execute(
            """
            SELECT c.code, c.description, c.level, c.chapter, c.heading, c.subheading, c.category, c.is_service, c.aliases,
                   (SELECT count(*) FROM hsn_codes sub WHERE sub.code LIKE c.code || "%" AND sub.code != c.code LIMIT 1) as child_cnt
            FROM hsn_codes c
            WHERE c.code = ?
            LIMIT 1
            """,
            (c,),
        ).fetchone()

        if not row:
            return None

        return HsnItemOut(
            code=row["code"],
            description=row["description"],
            formatted_title=format_title(row["code"], row["description"], row["aliases"]),
            level=row["level"],
            chapter=row["chapter"],
            heading=row["heading"],
            subheading=row["subheading"],
            category=row["category"],
            is_service=bool(row["is_service"]),
            aliases=row["aliases"] or "",
            has_children=bool(row["child_cnt"] > 0),
        )
    finally:
        conn.close()

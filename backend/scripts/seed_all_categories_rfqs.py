"""Seed 10,000 realistic RFQs spanning all 15 marketplace categories with HSN codes.

Features complete coverage across all commercial sectors:
1. Electronics (power banks, type-c cables, GaN chargers, 18650 cells, microcontrollers, BT modules)
2. Packaging (corrugated boxes, FIBC bulk bags, stretch cling film, bubble wrap, kraft sacks)
3. Textiles (combed cotton t-shirts, raw denim fabrics, cotton yarn, spun poly, mulberry silk)
4. Agriculture (1121 basmati rice, fuji apples, durum wheat, chia seeds, chickpeas, cardamom, coffee)
5. Industrial & Machinery (ball bearings, stainless steel sheets, brass valves, carbide end mills, hydraulic pumps)
6. Furniture (ergonomic mesh chairs, solid dining tables, electric standing desks, modular workstations)
7. Chemicals (titanium dioxide, citric acid, caustic soda flakes, potassium sorbate, glacial acetic acid)
8. Construction (vitrified floor tiles, portland cement OPC 53, TMT rebars Fe550D, uPVC profiles, float glass)
9. Stationery (A4 copier paper reams, fine gel pens, hardbound notebooks, magnetic whiteboards)
10. Learning & Education (digital biological microscopes, STEM robotics kits, physics optics benches)
11. Beauty & Personal Care (firm hold hair sprays, permanent hair color, argan keratin shampoo, foaming wash)
12. Food & Beverages (sweet corn kernels, fresh baby corn, extra virgin olive oil, matcha green tea)
13. Healthcare & Medical (nitrile examination gloves, surgical 3-ply masks, N95 respirators, IR thermometers)
14. Automotive & Spares (ceramic brake pads, ventilated rotors, synthetic engine oil 5W-30, LED headlights)
15. Solar & Clean Energy (monocrystalline 550W solar PV, hybrid 10kW inverters, LiFePO4 battery banks)

Both Buyer and Seller counterparties are generated with compatible prices, quantities, and HSN codes.
"""

import asyncio
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://marketplace:marketplace@localhost:5433/marketplace"
)
DSN = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

CITIES = [
    {"city": "Mumbai", "state": "Maharashtra", "country": "India", "lat": 19.0760, "lng": 72.8777, "currency": "INR", "rate": 86.5},
    {"city": "Delhi", "state": "Delhi", "country": "India", "lat": 28.6139, "lng": 77.2090, "currency": "INR", "rate": 86.5},
    {"city": "Bengaluru", "state": "Karnataka", "country": "India", "lat": 12.9716, "lng": 77.5946, "currency": "INR", "rate": 86.5},
    {"city": "Ahmedabad", "state": "Gujarat", "country": "India", "lat": 23.0225, "lng": 72.5714, "currency": "INR", "rate": 86.5},
    {"city": "Surat", "state": "Gujarat", "country": "India", "lat": 21.1702, "lng": 72.8311, "currency": "INR", "rate": 86.5},
    {"city": "Chennai", "state": "Tamil Nadu", "country": "India", "lat": 13.0827, "lng": 80.2707, "currency": "INR", "rate": 86.5},
    {"city": "Chicago", "state": "Illinois", "country": "United States", "lat": 41.8781, "lng": -87.6298, "currency": "USD", "rate": 1.0},
    {"city": "New York", "state": "New York", "country": "United States", "lat": 40.7128, "lng": -74.0060, "currency": "USD", "rate": 1.0},
    {"city": "Hamburg", "state": "Hamburg", "country": "Germany", "lat": 53.5511, "lng": 9.9937, "currency": "EUR", "rate": 0.92},
    {"city": "Dubai", "state": "Dubai", "country": "United Arab Emirates", "lat": 25.2048, "lng": 55.2708, "currency": "AED", "rate": 3.67},
    {"city": "London", "state": "Greater London", "country": "United Kingdom", "lat": 51.5074, "lng": -0.1278, "currency": "GBP", "rate": 0.79},
    {"city": "Singapore", "state": "Central Region", "country": "Singapore", "lat": 1.3521, "lng": 103.8198, "currency": "USD", "rate": 1.0},
]

# Comprehensive Catalog across 15 Canonical Categories
COMPREHENSIVE_CATALOG = [
    # 1. Electronics
    {
        "category": "Electronics",
        "templates": [
            ("20000mAh 65W Fast Charging Power Bank", "power bank", "pcs", (12.0, 22.0), "850760",
             {"capacity": "20000 mAh", "output_power": "65W", "cell_chemistry": "lithium polymer", "ports": "2x USB-C + 1x USB-A"}),
            ("Heavy Duty Braided USB-C to USB-C 100W Cable 2m", "USB Type-C cable", "pcs", (1.8, 3.8), "854442",
             {"connector": "USB-C to USB-C", "power_delivery": "100W", "length": "2m", "jacket": "braided nylon"}),
            ("GaN III 65W Dual Port Fast Wall Charger", "GaN wall charger", "pcs", (9.5, 18.0), "850440",
             {"output_power": "65W", "ports": "2x USB-C", "semiconductor": "GaN III", "fast_charge": "USB PD 3.0"}),
            ("Grade-A 18650 3200mAh Lithium Rechargeable Cells", "18650 lithium cell", "pcs", (1.4, 2.8), "850760",
             {"capacity": "3200 mAh", "chemistry": "NMC", "nominal_voltage": "3.7V", "grade": "Grade-A"}),
            ("ESP32 Dual-Core WiFi BLE Microcontroller Board", "microcontroller board", "pcs", (2.8, 6.5), "854231",
             {"architecture": "ESP32 dual core", "clock_speed": "240MHz", "wireless": "WiFi + BLE 5.0"}),
        ]
    },
    # 2. Packaging
    {
        "category": "Packaging",
        "templates": [
            ("Heavy Duty 5-Ply Corrugated Shipping Cartons", "corrugated box", "pcs", (0.35, 0.75), "481910",
             {"ply": "5-ply", "burst_strength": "16 kgf/cm2", "gsm": "180 GSM", "flute": "BC double wall"}),
            ("1 Tonne Heavy Duty FIBC Jumbo Bulk Bags", "FIBC bulk bag", "pcs", (4.2, 7.8), "630532",
             {"capacity": "1000 kg", "safe_working_load": "5:1", "discharge": "spout bottom", "uv_stabilised": True}),
            ("Machine Grade Transparent Stretch Wrap Film 23 Micron", "stretch cling film", "roll", (6.5, 12.0), "392010",
             {"width": "500 mm", "thickness": "23 micron", "elongation": "300%", "weight": "4 kg"}),
            ("Antistatic Protective Bubble Wrap Roll 100m", "bubble wrap", "roll", (8.0, 16.5), "392390",
             {"bubble_diameter": "10 mm", "length": "100 m", "width": "1.2 m", "antistatic": True}),
        ]
    },
    # 3. Textiles
    {
        "category": "Textiles",
        "templates": [
            ("100% Combed Cotton Bio-Washed Round Neck T-Shirts", "cotton t-shirt", "pcs", (3.2, 6.8), "610910",
             {"fabric": "100% combed cotton", "gsm": "180 GSM", "knit": "single jersey", "pre_shrunk": True}),
            ("Raw Indigo Twill Denim Fabric 12oz Non-Stretch", "denim fabric", "m", (2.4, 4.5), "520942",
             {"weight": "12 oz", "weave": "3x1 right hand twill", "stretch": False, "color": "raw indigo"}),
            ("100% Ring Spun Combed Cotton Yarn 30s Ne", "cotton yarn", "kg", (3.2, 5.5), "520523",
             {"count": "30s Ne", "spinning": "ring spun", "application": "knitting", "purity": "100% cotton"}),
            ("100% Pure Mulberry Silk Crepe 19 Momme", "mulberry silk", "m", (14.0, 26.0), "500720",
             {"momme_weight": "19 mm", "grade": "6A export", "width": "44 inch", "purity": "100% pure silk"}),
        ]
    },
    # 4. Agriculture
    {
        "category": "Agriculture",
        "templates": [
            ("Aged Traditional 1121 Golden Sella Basmati Rice", "basmati rice", "tonne", (950.0, 1350.0), "100630",
             {"variety": "1121 basmati", "grain_length": "8.4 mm", "processing": "golden sella", "broken": "1%"}),
            ("Fresh Royal Gala Grade-A Apples 75-80mm", "fuji apples", "kg", (1.6, 2.9), "080810",
             {"grade": "Extra Class", "calibre": "75-80 mm", "storage": "cold store 0-2C", "brix": "14 degrees"}),
            ("High Protein Durum Milling Wheat 13.5%", "durum wheat", "tonne", (280.0, 390.0), "100199",
             {"protein": "13.5%", "moisture": "11.5%", "hectolitre_weight": "80 kg/hl", "purity": "99%"}),
            ("Certified Organic Black Chia Seeds 99.9% Purity", "chia seeds", "tonne", (2900.0, 4400.0), "120799",
             {"purity": "99.9%", "moisture": "7%", "organic": True, "grade": "export"}),
            ("Bold Kabuli Chickpeas 12mm Size Export Grade", "chickpeas", "tonne", (1150.0, 1550.0), "071320",
             {"calibre": "12 mm", "count": "42-44 count", "moisture": "10%", "purity": "99%"}),
        ]
    },
    # 5. Industrial & Machinery
    {
        "category": "Industrial",
        "templates": [
            ("Deep Groove Chrome Steel Ball Bearings 6205-2RS", "industrial ball bearing", "pcs", (2.8, 6.2), "848210",
             {"bore_diameter": "25 mm", "seal": "2RS rubber", "clearance": "C3", "material": "chrome steel GCr15"}),
            ("Cold Rolled Stainless Steel Sheet SS 304 2B Finish", "stainless steel sheet", "tonne", (2100.0, 2900.0), "721933",
             {"grade": "SS 304", "thickness": "1.5 mm", "finish": "2B", "standard": "ASTM A240"}),
            ("Forged Brass Ball Valve Full Port PN25 1 Inch", "brass ball valve", "pcs", (4.5, 9.8), "848180",
             {"size": "1 inch", "pressure_rating": "PN25", "port": "full bore", "thread": "BSP"}),
            ("Solid Carbide 4-Flute End Mill CNC AlTiN Coated", "solid carbide end mill", "pcs", (9.5, 24.0), "820770",
             {"diameter": "10 mm", "flutes": "4 flute", "coating": "AlTiN", "hardness": "HRC 55"}),
        ]
    },
    # 6. Furniture
    {
        "category": "Furniture",
        "templates": [
            ("Ergonomic High-Back Breathable Mesh Office Chair", "ergonomic office chair", "pcs", (75.0, 145.0), "940130",
             {"back_material": "breathable mesh", "lumbar_support": "adjustable 2D", "gas_lift": "Class 4", "certification": "BIFMA"}),
            ("Solid Sheesham Wood 6-Seater Dining Table Walnut Finish", "dining table", "pcs", (140.0, 280.0), "940360",
             {"wood": "sheesham wood", "seats": 6, "finish": "walnut stain", "dimensions": "6ft x 3ft"}),
            ("Dual Motor Electric Height Adjustable Standing Desk Frame", "standing desk", "pcs", (150.0, 290.0), "940310",
             {"motor": "dual motor", "height_range": "65-125 cm", "load_capacity": "120 kg", "memory_presets": 4}),
        ]
    },
    # 7. Chemicals
    {
        "category": "Chemicals",
        "templates": [
            ("Rutile Titanium Dioxide White Pigment TiO2 99%", "titanium dioxide", "tonne", (2300.0, 3100.0), "320611",
             {"crystal_form": "rutile", "purity": "99%", "oil_absorption": "19 g/100g", "application": "coatings & plastics"}),
            ("Anhydrous Food Grade Citric Acid Monohydrate 99.5%", "citric acid", "tonne", (920.0, 1450.0), "291814",
             {"grade": "USP food grade", "purity": "99.5%", "mesh": "30-100 mesh", "packaging": "25kg bags"}),
            ("Industrial Grade Caustic Soda Flakes NaOH 99%", "caustic soda", "tonne", (450.0, 680.0), "281511",
             {"purity": "99% NaOH", "form": "white flakes", "packaging": "25kg HDPE bags"}),
            ("Pure Potassium Sorbate Granular Food Preservative", "potassium sorbate", "tonne", (3100.0, 4500.0), "291619",
             {"grade": "FCC food grade", "purity": "99.2%", "form": "granular", "solubility": "complete"}),
        ]
    },
    # 8. Construction
    {
        "category": "Construction",
        "templates": [
            ("Polished Glazed Vitrified Porcelain Floor Tiles 600x600", "vitrified floor tile", "m", (6.5, 13.5), "690721",
             {"size": "600x600 mm", "finish": "glossy polished", "water_absorption": "0.08%", "thickness": "9 mm"}),
            ("Grade 53 Ordinary Portland Cement OPC Bulk Bags", "portland cement", "tonne", (75.0, 115.0), "252329",
             {"grade": "OPC 53 Grade", "compressive_strength": "53 MPa", "packaging": "50kg HDPE bags"}),
            ("Fe550D High Ductility TMT Steel Rebars IS 1786", "TMT steel rebar", "tonne", (590.0, 780.0), "721420",
             {"grade": "Fe 550D", "diameter": "16 mm", "standard": "IS 1786", "elongation": "14.5%"}),
            ("Toughened Clear Architectural Float Glass 10mm", "float glass", "m", (14.0, 28.0), "700529",
             {"thickness": "10 mm", "type": "clear tempered", "edge": "flat polished"}),
        ]
    },
    # 9. Stationery
    {
        "category": "Stationery",
        "templates": [
            ("A4 Multipurpose Copier Paper Reams 75 GSM 500 Sheets", "A4 copier paper", "ream", (2.6, 4.2), "480256",
             {"gsm": "75 GSM", "sheets": 500, "brightness": "100%", "opacity": "94%"}),
            ("Quick-Dry Waterproof Gel Ink Pens 0.5mm Tip", "gel ink pen", "pcs", (0.22, 0.55), "960810",
             {"tip_size": "0.5 mm", "ink_color": "blue", "write_length": "1000 m", "type": "retractable"}),
            ("Executive Hardbound Ruled A5 Notebooks 192 Pages", "notebook", "pcs", (1.6, 3.8), "482010",
             {"pages": 192, "paper_gsm": "80 GSM", "cover": "PU leatherette", "ruling": "single ruled"}),
            ("Aluminium Frame Magnetic Dry Erase Whiteboard 4x3ft", "whiteboard", "pcs", (14.0, 28.0), "961000",
             {"size": "4x3 ft", "frame": "anodized aluminium", "magnetic": True, "surface": "resin coated"}),
        ]
    },
    # 10. Learning & Educational
    {
        "category": "Learning",
        "templates": [
            ("Compound Biological Student Microscope 1000x LED", "digital microscope", "pcs", (95.0, 220.0), "901180",
             {"magnification": "40x-1000x", "head": "binocular", "illumination": "LED", "optics": "achromatic"}),
            ("STEM Programmable Educational Robotics Kit with Sensors", "STEM robotics kit", "kit", (32.0, 75.0), "950300",
             {"controller": "Arduino compatible", "sensors": "ultrasonic + IR", "programming": "Blockly & Python"}),
            ("Precision Physics Optics Optical Laser Bench Rail Kit", "optics bench", "kit", (65.0, 160.0), "903180",
             {"track_length": "1 m", "laser": "650nm diode", "components": "lenses, prism, grating"}),
        ]
    },
    # 11. Beauty & Personal Care
    {
        "category": "Beauty & Personal Care",
        "templates": [
            ("Salon Grade Firm Hold Aerosol Hair Spray 400ml", "hair spray", "pcs", (3.2, 7.5), "330530",
             {"volume": "400 ml", "hold": "Extra Firm Hold", "finish": "natural shine", "aerosol": True}),
            ("Ammonia-Free Permanent Hair Color Cream Natural Black", "hair color", "pcs", (2.2, 5.4), "330590",
             {"shade": "natural black", "formula": "ammonia-free", "active": "keratin + argan oil"}),
            ("Sulfate-Free Moroccan Argan Keratin Shampoo 500ml", "shampoo", "bottle", (3.4, 8.5), "330510",
             {"volume": "500 ml", "formula": "sulfate-free", "active": "pure Moroccan argan oil"}),
            ("Gentle Foaming Tea Tree Herbal Face Wash 150ml", "face wash", "bottle", (2.4, 5.8), "330499",
             {"volume": "150 ml", "active": "tea tree oil + salicylic acid", "pump": "foaming pump"}),
        ]
    },
    # 12. Food & Beverages
    {
        "category": "Food & Beverages",
        "templates": [
            ("Bulk Grade-A Yellow Sweet Corn Kernels Frozen", "sweet corn", "tonne", (260.0, 440.0), "100590",
             {"variety": "sweet corn", "moisture": "12%", "purity": "99%", "cut": "whole kernel"}),
            ("Fresh Tender Baby Corn Spears for Export", "baby corn", "kg", (0.7, 1.5), "070999",
             {"cut": "whole spears", "grade": "export", "freshness": "farm-fresh"}),
            ("First Cold Pressed Extra Virgin Spanish Olive Oil", "olive oil", "liter", (6.8, 12.5), "150910",
             {"acidity": "< 0.4%", "extraction": "first cold pressed", "origin": "Spain"}),
            ("Ceremonial Grade Organic Uji Matcha Green Tea Powder", "matcha powder", "kg", (28.0, 55.0), "090210",
             {"grade": "ceremonial", "origin": "Uji, Japan", "organic": True, "harvest": "first flush"}),
        ]
    },
    # 13. Healthcare & Medical
    {
        "category": "Healthcare & Medical",
        "templates": [
            ("Medical Powder-Free Nitrile Examination Gloves 100/Box", "nitrile gloves", "box", (3.6, 6.8), "392620",
             {"count": "100 pcs/box", "thickness": "4.0 mil", "standard": "ASTM D6319", "powder_free": True}),
            ("Certified 3-Ply Earloop Medical Surgical Face Mask 50/Box", "surgical mask", "box", (1.6, 3.5), "630790",
             {"count": "50 pcs/box", "bfe": "BFE >= 98%", "standard": "EN 14683 Type IIR"}),
            ("NIOSH Certified N95 Particulate Respirator 20/Box", "n95 mask", "box", (7.5, 16.0), "630790",
             {"count": "20 pcs/box", "standard": "NIOSH N95", "filtration": ">= 95% at 0.3 micron"}),
            ("Non-Contact Digital Infrared Medical Forehead Thermometer", "thermometer", "pcs", (6.5, 14.5), "902519",
             {"distance": "3-5 cm", "response_time": "1 sec", "accuracy": "+/- 0.2C"}),
        ]
    },
    # 14. Automotive & Spares
    {
        "category": "Automotive",
        "templates": [
            ("Low-Dust Ceramic Front Disc Brake Pads Set", "brake pads", "set", (14.0, 32.0), "870830",
             {"formulation": "low-dust ceramic", "axle": "front axle", "hardware": "anti-squeal shims included"}),
            ("Precision Vented Front Brake Rotors Geomet Coated", "brake rotors", "pcs", (19.0, 42.0), "870830",
             {"design": "vented", "coating": "geomet anti-corrosion", "diameter": "300 mm"}),
            ("Full Synthetic 5W-30 SN/CF Engine Motor Oil 4L Can", "engine oil", "can", (16.0, 34.0), "271019",
             {"viscosity": "5W-30", "standard": "API SP, ILSAC GF-6A", "base": "fully synthetic"}),
            ("Automotive High-Lumen H7 LED Headlight Bulbs 16000LM", "led headlight", "pair", (18.0, 38.0), "851220",
             {"socket": "H7", "luminous_flux": "16000 lm", "color_temp": "6000K", "cooling": "turbofan"}),
        ]
    },
    # 15. Solar & Clean Energy
    {
        "category": "Solar & Energy",
        "templates": [
            ("550W Half-Cell Monocrystalline Solar PV Module Tier-1", "solar panel", "pcs", (58.0, 95.0), "854143",
             {"power": "550W", "type": "monocrystalline half-cut", "efficiency": "21.8%", "warranty": "25 years"}),
            ("10kW Three-Phase Hybrid Solar Inverter Dual MPPT", "solar inverter", "pcs", (880.0, 1450.0), "850440",
             {"power": "10 kW", "type": "hybrid on/off grid", "mppt": "dual MPPT", "efficiency": "98.2%"}),
            ("48V 100Ah 5.12kWh LiFePO4 Lithium Solar Battery Storage", "solar battery", "pcs", (680.0, 1150.0), "850760",
             {"capacity": "100 Ah", "voltage": "51.2V", "energy": "5.12 kWh", "cycle_life": "6000 cycles"}),
        ]
    },
]


async def seed_all_categories(target_dsn: str = None, num_rfqs: int = 10000):
    dsn = target_dsn or os.getenv("DATABASE_URL", DSN).replace("postgresql+asyncpg://", "postgresql://")
    print(f"Connecting to database: {dsn.split('@')[-1]}...")
    conn = await asyncpg.connect(dsn)

    # 1. Fetch all existing users from PostgreSQL, or seed base users if empty
    user_records = await conn.fetch("SELECT id, email, role FROM users")
    if not user_records:
        print("No users found. Creating base demo, buyer1-10, seller1-10 users and profiles...")
        now = datetime.now(timezone.utc)
        from core.security import hash_password
        demo_hash = hash_password("trade2026demo")

        user_rows = []
        profile_rows = []

        # Demo user
        demo_uid = uuid.uuid4()
        user_rows.append((demo_uid, "demo@marketplace.dev", hash_password("demo password 123"), "both", "active", now, None, now, now))
        profile_rows.append((uuid.uuid4(), demo_uid, "Demo Admin", "Global Trading Demo Ltd", "+1 555 0199", "5th Ave", "New York", "New York", "USA", Decimal("40.7128"), Decimal("-74.0060"), now, now, "GST-DEMO", "Global Trading Demo Ltd", "Wholesale Trader", "REG-2026-0001", 2018, "https://demo.trade", "PAN-DEMO", "Demo Chief", "verified", 95, Decimal("4.85"), 25, json.dumps({})))

        for i in range(1, 11):
            b_uid = uuid.uuid4()
            user_rows.append((b_uid, f"buyer{i}@marketplace.dev", demo_hash, "buyer", "active", now, None, now, now))
            profile_rows.append((uuid.uuid4(), b_uid, f"Buyer {i} Officer", f"Buyer {i} Enterprise Inc", f"+1 555 010{i}", "Commerce St", "Chicago", "Illinois", "United States", Decimal("41.8781"), Decimal("-87.6298"), now, now, f"GST-B{i}", f"Buyer {i} Enterprise Inc", "Corporate Buyer", f"REG-2026-B{i}", 2015, f"https://buyer{i}.com", f"PAN-B{i}", f"Buyer {i}", "verified", 90, None, 0, json.dumps({})))

            s_uid = uuid.uuid4()
            user_rows.append((s_uid, f"seller{i}@marketplace.dev", demo_hash, "seller", "active", now, None, now, now))
            profile_rows.append((uuid.uuid4(), s_uid, f"Seller {i} Officer", f"Seller {i} Manufacturing Co", f"+1 555 020{i}", "Industrial Pkwy", "Houston", "Texas", "United States", Decimal("29.7604"), Decimal("-95.3698"), now, now, f"GST-S{i}", f"Seller {i} Manufacturing Co", "Manufacturer", f"REG-2026-S{i}", 2012, f"https://seller{i}.com", f"PAN-S{i}", f"Seller {i}", "verified", 92, Decimal("4.75"), 15, json.dumps({})))

        await conn.copy_records_to_table(
            "users",
            records=user_rows,
            columns=["id", "email", "password_hash", "role", "status", "email_verified_at", "last_login_at", "created_at", "updated_at"]
        )
        await conn.copy_records_to_table(
            "user_profiles",
            records=profile_rows,
            columns=[
                "id", "user_id", "name", "company_name", "phone", "address",
                "city", "state", "country", "latitude", "longitude",
                "created_at", "updated_at", "gst_number", "legal_business_name",
                "business_type", "registration_number", "year_established",
                "website", "pan_number", "signatory_name", "kyc_status",
                "trust_score", "average_rating", "total_reviews", "matching_preferences"
            ]
        )
        user_records = await conn.fetch("SELECT id, email, role FROM users")

    print(f"Loaded {len(user_records)} existing users from database.")
    users_by_role = {"buyer": [], "seller": [], "both": []}
    demo_user_id = None
    buyer_ids = []
    seller_ids = []

    for r in user_records:
        role = r["role"].lower()
        uid = r["id"]
        email = r["email"]
        if role in users_by_role:
            users_by_role[role].append(uid)
        if email == "demo@marketplace.dev":
            demo_user_id = uid
        elif email.startswith("buyer"):
            buyer_ids.append(uid)
        elif email.startswith("seller"):
            seller_ids.append(uid)

    buyer_pool = users_by_role["buyer"] + users_by_role["both"]
    seller_pool = users_by_role["seller"] + users_by_role["both"]

    # 2. Clear previous RFQs and dependent tables
    print("Clearing previous RFQ and deal tables...")
    await conn.execute("TRUNCATE TABLE match_results, saved_rfqs, quotations, connections, rfqs CASCADE;")

    # 3. Build template pool across all 15 categories
    all_templates = []
    for cat_group in COMPREHENSIVE_CATALOG:
        cat_name = cat_group["category"]
        for tmpl in cat_group["templates"]:
            all_templates.append((cat_name, tmpl))

    print(f"Loaded {len(all_templates)} distinct product templates across {len(COMPREHENSIVE_CATALOG)} categories.")

    total_rfqs = max(num_rfqs, 60)
    now = datetime.now(timezone.utc)
    rfq_rows = []

    # 4. Generate pairs for demo@marketplace.dev across all 15 categories (30 RFQs)
    if demo_user_id:
        print(f"Generating 30 dedicated listings for demo@marketplace.dev across all categories...")
        for i, (cat_name, tmpl) in enumerate(all_templates[:15]):
            title_base, prod_name, unit, (p_min, p_max), hsn, specs = tmpl
            loc = CITIES[i % len(CITIES)]
            rate = loc["rate"]
            mid_price = (p_min + p_max) / 2.0 * rate

            # Demo Buyer RFQ
            b_id = uuid.uuid4()
            b_details = dict(specs)
            b_details["product"] = prod_name
            b_details["hsn_code"] = hsn
            b_qty = 1000.0 if unit in ("pcs", "kg", "m", "box", "roll") else (20.0 if unit == "tonne" else 100.0)
            b_price = round(mid_price * 1.05, 2)
            b_deadline = now + timedelta(days=21)
            b_search_text = f"BUYER: Looking for {title_base}. Product: {prod_name}. Category: {cat_name}. Specs: {json.dumps(specs)}. Delivery to {loc['city']}, {loc['country']}."
            b_tags = [prod_name, cat_name.lower(), loc["city"].lower(), unit.lower(), hsn, "demo", "buyer"]

            rfq_rows.append((
                b_id, demo_user_id, "buyer", "active", cat_name,
                f"Urgent Requirement: {title_base}",
                f"Seeking immediate delivery of verified {title_base} with full quality certificates.",
                Decimal(str(b_qty)), unit, Decimal(str(b_price)), loc["currency"], unit,
                loc["city"], loc["state"], loc["country"],
                Decimal(str(loc["lat"])), Decimal(str(loc["lng"])),
                f"{loc['city']}, {loc['state']}, {loc['country']}",
                b_deadline, b_deadline.strftime("%Y-%m-%d"), now + timedelta(days=60),
                json.dumps(b_details), b_tags, b_search_text, "pending", None, None, now, now,
                Decimal(str(round(b_qty * 0.1, 1))), unit
            ))

            # Demo Seller RFQ (Different category product)
            s_cat_name, s_tmpl = all_templates[(i + 15) % len(all_templates)]
            s_title_base, s_prod_name, s_unit, (sp_min, sp_max), s_hsn, s_specs = s_tmpl
            s_id = uuid.uuid4()
            s_details = dict(s_specs)
            s_details["product"] = s_prod_name
            s_details["hsn_code"] = s_hsn
            s_qty = 5000.0 if s_unit in ("pcs", "kg", "m", "box", "roll") else (50.0 if s_unit == "tonne" else 500.0)
            s_price = round(((sp_min + sp_max) / 2.0 * rate) * 0.95, 2)
            s_deadline = now + timedelta(days=14)
            s_search_text = f"SELLER: Ready stock available for {s_title_base}. Product: {s_prod_name}. Category: {s_cat_name}. Specs: {json.dumps(s_specs)}. Origin: {loc['city']}, {loc['country']}."
            s_tags = [s_prod_name, s_cat_name.lower(), loc["city"].lower(), s_unit.lower(), s_hsn, "demo", "seller"]

            rfq_rows.append((
                s_id, demo_user_id, "seller", "active", s_cat_name,
                f"Wholesale Supply Available: {s_title_base}",
                f"Direct factory supply for certified {s_title_base}. Export grade, fast shipping.",
                Decimal(str(s_qty)), s_unit, Decimal(str(s_price)), loc["currency"], s_unit,
                loc["city"], loc["state"], loc["country"],
                Decimal(str(loc["lat"])), Decimal(str(loc["lng"])),
                f"{loc['city']}, {loc['state']}, {loc['country']}",
                s_deadline, s_deadline.strftime("%Y-%m-%d"), now + timedelta(days=60),
                json.dumps(s_details), s_tags, s_search_text, "pending", None, None, now, now,
                Decimal(str(round(s_qty * 0.05, 1))), s_unit
            ))

    # 5. Generate Remaining Listings across all 15 categories (~9,970 RFQs)
    print(f"Generating remaining {total_rfqs - len(rfq_rows)} RFQs evenly distributed across 15 categories...")
    remaining_count = total_rfqs - len(rfq_rows)

    for idx in range(remaining_count):
        rfq_id = uuid.uuid4()
        is_buyer = (idx % 2 == 0)
        role_val = "buyer" if is_buyer else "seller"

        # Rotate through all users
        if is_buyer:
            user_id = buyer_ids[idx % len(buyer_ids)] if (buyer_ids and idx < len(buyer_ids) * 10) else random.choice(buyer_pool)
        else:
            user_id = seller_ids[idx % len(seller_ids)] if (seller_ids and idx < len(seller_ids) * 10) else random.choice(seller_pool)

        # Select template
        cat_name, tmpl = all_templates[idx % len(all_templates)]
        title_base, prod_name, unit, (p_min, p_max), hsn, specs = tmpl

        loc = CITIES[random.randint(0, len(CITIES) - 1)]
        rate = loc["rate"]
        curr = loc["currency"]

        # Base pricing with slight variation
        base_p = random.uniform(p_min, p_max) * rate
        # Buyers offer +/- 8%, sellers ask +/- 8%
        price_factor = random.uniform(0.95, 1.08) if is_buyer else random.uniform(0.92, 1.05)
        price = round(base_p * price_factor, 2)

        # Realistic Quantities
        if unit in ("tonne",):
            qty = round(random.uniform(5, 500), 1)
        elif unit in ("pcs", "kg", "m"):
            qty = float(random.choice([500, 1000, 2500, 5000, 10000, 25000, 50000]))
        elif unit in ("box", "roll", "can", "bottle", "ream", "pair"):
            qty = float(random.choice([100, 250, 500, 1000, 2500, 5000]))
        elif unit in ("kit", "set"):
            qty = float(random.choice([50, 100, 250, 500, 1000]))
        else:
            qty = 1000.0

        moq = round(qty * random.uniform(0.05, 0.20), 1)

        deadline_days = random.randint(7, 45)
        deadline_dt = now + timedelta(days=deadline_days)
        expires_dt = now + timedelta(days=random.randint(60, 120))

        details = dict(specs)
        details["product"] = prod_name
        details["hsn_code"] = hsn
        # Slightly vary an attribute for diversity
        if "capacity" in details and unit == "pcs":
            details["batch_no"] = f"BT-{2026}-{idx % 500}"

        prefix = "Procurement Request" if is_buyer else "Direct Factory Offer"
        title = f"{prefix}: {title_base} ({loc['city']})"
        desc = (
            f"{'Buyer looking to procure' if is_buyer else 'Verified supplier offering'} {title_base}. "
            f"Quantity: {qty} {unit}. Quoted at {price} {curr}/{unit}. "
            f"Specifications: {', '.join(f'{k}: {v}' for k, v in list(specs.items())[:3])}. "
            f"Location: {loc['city']}, {loc['state']}, {loc['country']}. HSN/HS Code: {hsn}."
        )

        search_text = (
            f"{role_val.upper()}: {title_base}. Product: {prod_name}. Category: {cat_name}. "
            f"Quantity: {qty} {unit}. Price: {price} {curr}. HSN: {hsn}. "
            f"Location: {loc['city']}, {loc['country']}. Specs: {json.dumps(details)}."
        )

        search_tags = [
            prod_name,
            cat_name.lower(),
            loc["city"].lower(),
            unit.lower(),
            hsn,
            role_val,
            "b2b"
        ]

        rfq_rows.append((
            rfq_id,
            user_id,
            role_val,
            "active",
            cat_name,
            title,
            desc,
            Decimal(str(qty)),
            unit,
            Decimal(str(price)),
            curr,
            unit,
            loc["city"],
            loc["state"],
            loc["country"],
            Decimal(str(loc["lat"])),
            Decimal(str(loc["lng"])),
            f"{loc['city']}, {loc['state']}, {loc['country']}",
            deadline_dt,
            deadline_dt.strftime("%Y-%m-%d"),
            expires_dt,
            json.dumps(details),
            search_tags,
            search_text,
            "pending",
            None,
            None,
            now,
            now,
            Decimal(str(moq)),
            unit,
        ))

    print(f"Bulk inserting {len(rfq_rows)} RFQs into PostgreSQL 'rfqs' table...")
    await conn.copy_records_to_table(
        "rfqs",
        records=rfq_rows,
        columns=[
            "id", "user_id", "role", "status", "category", "title", "description",
            "quantity_value", "quantity_unit", "price_amount", "price_currency", "price_per_unit",
            "location_city", "location_state", "location_country", "latitude", "longitude",
            "location_raw", "deadline_at", "deadline_raw", "expires_at", "product_details",
            "search_tags", "search_text", "embedding_status", "embedded_at", "embedding_error",
            "created_at", "updated_at", "min_order_value", "min_order_unit"
        ]
    )

    print("\nVerifying category counts in database:")
    cat_counts = await conn.fetch("SELECT category, role, count(*) FROM rfqs GROUP BY category, role ORDER BY category, role;")
    for row in cat_counts:
        print(f"  {row['category']:24s} | {row['role']:6s} | {row['count']} listings")

    total_inserted = await conn.fetchval("SELECT count(*) FROM rfqs;")
    print(f"\n[SUCCESS] Successfully seeded {total_inserted} active RFQs across all 15 categories!")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(seed_all_categories())

"""Seed 100 brand-new users and 10,000 out-of-the-box RFQs into PostgreSQL.

Features completely new varieties never seen before in the platform:
- Corn & Maize varieties (sweet corn, baby corn, dent corn, popping corn, corn starch)
- Hair spray & Hair sprey (extra hold aerosol, anti-humidity mist, salon lacquer)
- Hair color & Hair colour (ammonia-free dye, burgundy cream, ash blonde toner)
- Hair care & Shampoos (sulfate-free keratin, Moroccan argan, anti-dandruff)
- Automotive & Spares (ceramic brake pads, ventilated rotors, LED headlights)
- Healthcare & PPE (nitrile gloves, latex exam gloves, N95 respirators)
- Clean Energy (550W monocrystalline solar panels, 10kW hybrid inverters)
- Specialty Chemicals (citric acid monohydrate, potassium sorbate, sodium benzoate)

All previous seeded data is wiped; 100 fresh users and 10,000 fresh RFQs are created.
"""

import asyncio
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
from core.security import hash_password

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://marketplace:marketplace@localhost:5433/marketplace"
)
# Convert SQLAlchemy asyncpg URL to standard asyncpg DSN
DSN = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

CITIES = [
    {"city": "Mumbai", "state": "Maharashtra", "country": "India", "lat": 19.0760, "lng": 72.8777},
    {"city": "Delhi", "state": "Delhi", "country": "India", "lat": 28.6139, "lng": 77.2090},
    {"city": "Bengaluru", "state": "Karnataka", "country": "India", "lat": 12.9716, "lng": 77.5946},
    {"city": "Ahmedabad", "state": "Gujarat", "country": "India", "lat": 23.0225, "lng": 72.5714},
    {"city": "Surat", "state": "Gujarat", "country": "India", "lat": 21.1702, "lng": 72.8311},
    {"city": "Ludhiana", "state": "Punjab", "country": "India", "lat": 30.9010, "lng": 75.8573},
    {"city": "Indore", "state": "Madhya Pradesh", "country": "India", "lat": 22.7196, "lng": 75.8577},
    {"city": "Hyderabad", "state": "Telangana", "country": "India", "lat": 17.3850, "lng": 78.4867},
    {"city": "Chennai", "state": "Tamil Nadu", "country": "India", "lat": 13.0827, "lng": 80.2707},
    {"city": "Kolkata", "state": "West Bengal", "country": "India", "lat": 22.5726, "lng": 88.3639},
    {"city": "Pune", "state": "Maharashtra", "country": "India", "lat": 18.5204, "lng": 73.8567},
    {"city": "Coimbatore", "state": "Tamil Nadu", "country": "India", "lat": 11.0168, "lng": 76.9558},
    {"city": "Chicago", "state": "Illinois", "country": "United States", "lat": 41.8781, "lng": -87.6298},
    {"city": "Hamburg", "state": "Hamburg", "country": "Germany", "lat": 53.5511, "lng": 9.9937},
    {"city": "Dubai", "state": "Dubai", "country": "United Arab Emirates", "lat": 25.2048, "lng": 55.2708},
    {"city": "London", "state": "Greater London", "country": "United Kingdom", "lat": 51.5074, "lng": -0.1278},
    {"city": "Singapore", "state": "Central Region", "country": "Singapore", "lat": 1.3521, "lng": 103.8198},
]

USER_COMPANY_NAMES = [
    "Apex Agri Grain Corp", "Velvet Strand Cosmetics", "Aura Beauty Labs",
    "Nordic Corn Millers", "Brembo Precision Auto", "BioCare Surgical Supplies",
    "Helios Solar Power Ltd", "Zenith Specialty Chemical", "Golden Harvest Maize",
    "Silk & Shine Hair Studio", "Pinnacle Auto Friction", "MedPro Global Nitrile",
    "Kavita Agri Exports", "Luxe Lock Salon Chem", "Voltech Renewable Solutions",
    "Sterling Agro Trade", "Purity Chemical Labs", "Vanguard Brake Tech",
    "EverGreen Organic Corn", "Radiant Shade Hair Colour", "Oasis Personal Care",
    "Falcon Heavy Spares", "Nova Solar Module", "Crystal Clear Additives",
    "SunState Maize Growers", "SalonSelect Aerosols", "Titan Auto Components",
    "Aegis Medical Garments", "SunRay Photovoltaics", "Imperial Food Additives"
]

VARIETY_CATALOG = [
    # 1. Corn & Maize Varieties
    {
        "category": "Food & Beverages",
        "templates": [
            ("Grade-A Yellow Sweet Corn", "sweet corn", "tonne", (18000, 32000), "INR", {"moisture": "12%", "variety": "sweet corn", "purity": "99%", "packaging": "jute bags"}),
            ("Non-GMO Yellow Dent Maize for Feed", "corn", "tonne", (16000, 26000), "INR", {"grade": "feed grade", "protein": "9%", "moisture": "14%", "form": "grain"}),
            ("Organic Popping Corn Kernels", "popping corn", "kg", (45, 95), "INR", {"variety": "butterfly popcorn", "expansion": "42x", "organic": True, "packaging": "25kg bags"}),
            ("Fresh Tender Baby Corn for Canning", "baby corn", "kg", (35, 75), "INR", {"cut": "whole spears", "grade": "export", "freshness": "farm-fresh"}),
            ("Food Grade Industrial Corn Starch Powder", "corn starch", "tonne", (28000, 48000), "INR", {"mesh": "100 mesh", "ash": "0.15%", "purity": "99.5%"}),
            ("Bulk Yellow Corn Grits Brewery Grade", "corn grits", "tonne", (22000, 36000), "INR", {"fat_content": "1%", "granulation": "coarse", "grade": "brewery"}),
        ]
    },
    # 2. Hair Spray & Aerosols
    {
        "category": "Beauty & Personal Care",
        "templates": [
            ("Professional Salon Extra Hold Hair Spray 400ml", "hair spray", "cans", (140, 380), "INR", {"volume": "400ml", "hold": "level 5 extra hold", "finish": "matte", "aerosol": True}),
            ("Anti-Humidity Strong Hold Hair Sprey 250ml", "hair sprey", "bottles", (120, 320), "INR", {"volume": "250ml", "hold": "strong hold", "anti_frizz": True, "scent": "botanical"}),
            ("Volumizing Fine Mist Hair Spray Aerosol Can", "hair spray", "cans", (160, 420), "INR", {"volume": "300ml", "hold": "medium flexible", "finish": "gloss", "residue_free": True}),
            ("Organic Argan Finishing Mist Hair Spray", "hair spray", "bottles", (220, 550), "INR", {"volume": "200ml", "organic": True, "silicone_free": True, "hold": "light hold"}),
            ("Quick Dry Extreme Hold Hair Lacquer Spray", "hair spray", "cans", (180, 460), "INR", {"volume": "500ml", "hold": "extreme lacquer", "drying_time": "30 seconds"}),
        ]
    },
    # 3. Hair Color & Dyes
    {
        "category": "Beauty & Personal Care",
        "templates": [
            ("Ammonia-Free Permanent Hair Color Cream 100ml", "hair color", "tubes", (65, 180), "INR", {"shade": "burgundy", "ammonia_free": True, "coverage": "100% grey coverage"}),
            ("Professional Salon Hair Colour Developer 20 Vol", "hair colour", "bottles", (50, 140), "INR", {"strength": "20 volume 6%", "volume": "1000ml", "texture": "creamy"}),
            ("Dark Chocolate Brown Permanent Hair Color Kit", "hair color", "boxes", (110, 290), "INR", {"shade": "dark brown", "kit_contents": "dye cream developer gloves", "enrichment": "keratin"}),
            ("Organic Herbal Henna Natural Hair Colour Powder", "hair colour", "kg", (180, 420), "INR", {"purity": "100% natural", "form": "triple sifted powder", "chemical_free": True}),
            ("Ash Blonde Bleach Powder and Hair Color Toner", "hair color", "packs", (240, 680), "INR", {"tone": "ash blonde", "lift": "up to 8 levels", "dust_free": True}),
        ]
    },
    # 4. Shampoo & Hair Cleanser
    {
        "category": "Beauty & Personal Care",
        "templates": [
            ("Sulfate-Free Moroccan Argan Oil Shampoo 500ml", "shampoo", "bottles", (150, 420), "INR", {"sulfate_free": True, "volume": "500ml", "extract": "argan oil", "paraben_free": True}),
            ("Anti-Dandruff Zinc Pyrithione Scalp Shampoo", "shampoo", "bottles", (110, 290), "INR", {"active": "1% ZPTO", "volume": "400ml", "cooling": "menthol"}),
            ("Hydrolyzed Keratin Protein Deep Repair Hair Shampoo", "shampoo", "bottles", (180, 520), "INR", {"volume": "1000ml", "formula": "keratin complex", "hair_type": "damaged"}),
        ]
    },
    # 5. Automotive Components
    {
        "category": "Automotive",
        "templates": [
            ("Premium Ceramic Disc Brake Pads Set Front", "brake pads", "sets", (450, 1600), "INR", {"material": "ceramic", "position": "front axle", "low_dust": True, "anti_noise": True}),
            ("Ventilated High-Carbon Front Brake Disc Rotors", "brake discs", "pairs", (1200, 3800), "INR", {"diameter": "280mm", "finish": "geomet coated", "material": "high carbon steel"}),
            ("Automotive Turbo LED Headlight Bulbs 120W H7", "led headlights", "pairs", (600, 2200), "INR", {"power": "120W", "lumen": "16000LM", "color_temp": "6000K cool white", "socket": "H7"}),
            ("Full Synthetic 5W-30 SN Plus Motor Engine Oil", "engine oil", "litres", (220, 650), "INR", {"viscosity": "5W-30", "grade": "API SN Plus / ILSAC GF-5", "volume": "4L can"}),
        ]
    },
    # 6. Healthcare & Medical Supplies
    {
        "category": "Healthcare & Medical",
        "templates": [
            ("Powder-Free Disposable Nitrile Examination Gloves", "nitrile gloves", "boxes", (180, 420), "INR", {"pack_size": "100 pcs per box", "color": "blue", "thickness": "4 mil", "sterile": False}),
            ("Sterile Surgical Latex Gloves Powder-Free Size 7.5", "surgical gloves", "pairs", (18, 45), "INR", {"material": "latex", "size": "7.5", "sterile": True, "standard": "EN 455"}),
            ("N95 Particulate Respirator Medical Face Masks", "n95 mask", "boxes", (320, 850), "INR", {"filtration": ">= 95% BFE/PFE", "layers": "5 ply", "certification": "NIOSH/FDA approved"}),
        ]
    },
    # 7. Solar & Renewable Energy
    {
        "category": "Solar & Energy",
        "templates": [
            ("550W Monocrystalline Half-Cut Cell Solar PV Module", "solar panel", "pcs", (6500, 13500), "INR", {"power": "550W", "cell_type": "mono PERC", "efficiency": "21.3%", "warranty": "25 years"}),
            ("10kW Three-Phase Hybrid Solar Inverter with MPPT", "solar inverter", "units", (65000, 140000), "INR", {"capacity": "10kW", "type": "hybrid on/off grid", "mppt_trackers": 2, "ip_rating": "IP65"}),
            ("48V 100Ah Lithium Iron Phosphate LiFePO4 Battery", "battery", "units", (45000, 95000), "INR", {"voltage": "48V", "capacity": "100Ah 4.8kWh", "cycles": "6000 cycles", "bms": "integrated"}),
        ]
    },
    # 8. Specialty Chemicals & Additives
    {
        "category": "Industrial Chemicals",
        "templates": [
            ("Food Grade Citric Acid Monohydrate 99.5%", "citric acid", "tonne", (75000, 135000), "INR", {"purity": "99.5%", "grade": "BP/USP/FCC food grade", "mesh": "30-100 mesh", "packaging": "25kg bags"}),
            ("Potassium Sorbate Granular Food Preservative", "potassium sorbate", "kg", (240, 520), "INR", {"purity": "99.0%", "grade": "FCCIV", "application": "beverage & food preservative"}),
            ("USP Pure Grade Sodium Benzoate Powder", "sodium benzoate", "kg", (110, 260), "INR", {"assay": "99.9%", "form": "dense powder", "heavy_metals": "< 10ppm"}),
            ("Industrial Caustic Soda Flakes 99% Sodium Hydroxide", "caustic soda", "tonne", (38000, 62000), "INR", {"purity": "99%", "form": "pure white flakes", "packaging": "50kg HDPE bags"}),
        ]
    }
]


def render_search_text(role: str, category: str, title: str, prod_name: str, qty: float, unit: str, price: float, currency: str, city: str, state: str, country: str, deadline: datetime, details: dict) -> str:
    lines = [
        f"Role: {role.title()}",
        f"Category: {category}",
        f"Product: {prod_name or title}",
        f"Quantity: {qty} {unit}",
        f"Target price: {price} {currency}/{unit}",
        f"Location: {city}, {state}, {country}",
        f"Deadline: {deadline.date().isoformat()}",
    ]
    for k, v in sorted(details.items()):
        lines.append(f"{k.replace('_', ' ').title()}: {v}")
    return "\n".join(lines)


async def main():
    print("=" * 70)
    print("STARTING OUT-OF-THE-BOX SEEDING: 100 USERS & 10,000 RFQS")
    print("=" * 70)

    print(f"Connecting to database via asyncpg: {DSN} ...")
    conn = await asyncpg.connect(DSN)

    print("Cleaning all existing marketplace records...")
    await conn.execute("""
        TRUNCATE TABLE 
            reviews, 
            connections, 
            saved_rfqs, 
            match_results, 
            match_searches, 
            quotations, 
            rfqs, 
            user_profiles, 
            users 
        CASCADE;
    """)
    print("Database cleanly truncated.")

    # 1. Generate 100 Users & Profiles
    print("\nGenerating 100 brand-new diverse users...")
    demo_password_hash = hash_password("Marketplace@2026")
    now = datetime.now(timezone.utc)

    user_rows = []
    profile_rows = []
    users_by_role = {"buyer": [], "seller": [], "both": []}

    roles_pool = ["buyer"] * 50 + ["seller"] * 45 + ["both"] * 5
    random.seed(42)
    random.shuffle(roles_pool)

    for i in range(100):
        uid = uuid.uuid4()
        role = roles_pool[i]
        company = f"{USER_COMPANY_NAMES[i % len(USER_COMPANY_NAMES)]} {i+1}"
        email = f"{role}_{i+1:03d}_{company.lower().replace(' ', '_').replace('&', 'and')[:22]}@marketb2b.io"
        city_info = CITIES[i % len(CITIES)]

        user_rows.append((
            uid,
            email,
            demo_password_hash,
            role,
            "active",
            now,
            now,
            now,
            now,
        ))

        pid = uuid.uuid4()
        profile_rows.append((
            pid,
            uid,
            f"Director {company.split()[0]}",
            company,
            f"+91 98{i:02d}1 0{i:02d}23",
            f"Suite {100 + i}, Industrial Zone, {city_info['city']}",
            city_info["city"],
            city_info["state"],
            city_info["country"],
            Decimal(str(city_info["lat"])),
            Decimal(str(city_info["lng"])),
            now,
            now,
            f"27AAAC{i:04d}A1Z{i%9}",
            f"{company} Pvt Ltd",
            "Manufacturer" if role == "seller" else ("Wholesale Trader" if role == "both" else "Corporate Buyer"),
            f"REG-{2020 + (i % 6)}-{1000 + i}",
            2010 + (i % 14),
            f"https://www.{company.lower().replace(' ', '').replace('&', '')}.com",
            f"AAAC{i:04d}P",
            f"Chief Officer {i+1}",
            "verified",
            85 + (i % 15),
            Decimal("4.65") if role in ("seller", "both") else None,
            12 + (i % 40) if role in ("seller", "both") else 0,
            json.dumps({"preferred_radius_km": 500}),
        ))

        users_by_role[role].append((uid, city_info))

    print(f"Inserting 100 users into 'users' table...")
    await conn.copy_records_to_table(
        "users",
        records=user_rows,
        columns=["id", "email", "password_hash", "role", "status", "email_verified_at", "last_login_at", "created_at", "updated_at"]
    )

    print(f"Inserting 100 profiles into 'user_profiles' table...")
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
    print("Users & Profiles seeded successfully.")

    # 2. Generate 10,000 Out-of-the-Box RFQs
    print("\nGenerating 10,000 out-of-the-box RFQs across brand-new categories...")
    rfq_rows = []

    # Flatten all catalog templates
    all_templates = []
    for cat_group in VARIETY_CATALOG:
        cat_name = cat_group["category"]
        for tmpl in cat_group["templates"]:
            all_templates.append((cat_name, tmpl))

    num_rfqs = 10000
    buyer_pool = users_by_role["buyer"] + users_by_role["both"]
    seller_pool = users_by_role["seller"] + users_by_role["both"]

    for idx in range(num_rfqs):
        rfq_id = uuid.uuid4()
        # 50% Buyer, 50% Seller
        is_buyer = (idx % 2 == 0)
        role_val = "buyer" if is_buyer else "seller"
        user_id, base_city = random.choice(buyer_pool if is_buyer else seller_pool)

        # Pick commodity template
        cat_name, tmpl = all_templates[idx % len(all_templates)]
        tmpl_title, prod_name, unit, price_range, currency, base_details = tmpl

        # Dynamic variation per RFQ
        qty = round(random.uniform(50, 5000), 1)
        base_price = random.uniform(price_range[0], price_range[1])
        # Buyer offers slightly lower or at target, seller asks slightly higher or competitive
        price = round(base_price * (0.95 if is_buyer else 1.05), 2)

        # 85% use user city, 15% use another major hub
        if random.random() < 0.85:
            loc = base_city
        else:
            loc = random.choice(CITIES)

        # Delivery deadline between 7 and 90 days out
        lead_days = random.randint(7, 90)
        deadline_dt = now + timedelta(days=lead_days)
        expires_dt = deadline_dt + timedelta(days=14)

        # Specific attributes JSON
        details = dict(base_details)
        details["product_name"] = prod_name
        details["batch_lot"] = f"LOT-{idx+1000}"
        if is_buyer:
            details["compliance_mandatory"] = True
            title = f"Requirement for {tmpl_title} - {int(qty)} {unit}"
        else:
            details["ready_to_ship"] = True
            title = f"Ready Stock Offer: {tmpl_title} - {int(qty)} {unit}"

        desc = f"Commercial B2B {'sourcing demand' if is_buyer else 'supply listing'} for {tmpl_title}. Quantity: {qty} {unit} in {loc['city']}, {loc['country']}. Specifications: {', '.join(f'{k}={v}' for k, v in base_details.items())}."

        search_text = render_search_text(
            role=role_val,
            category=cat_name,
            title=title,
            prod_name=prod_name,
            qty=qty,
            unit=unit,
            price=price,
            currency=currency,
            city=loc["city"],
            state=loc["state"],
            country=loc["country"],
            deadline=deadline_dt,
            details=details,
        )

        search_tags = [
            prod_name,
            cat_name.lower(),
            loc["city"].lower(),
            unit.lower(),
            "b2b",
            "wholesale"
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
            currency,
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
            Decimal(str(round(qty * 0.1, 1))),  # min_order_value (10% MOQ)
            unit,
        ))

    print(f"Bulk inserting {len(rfq_rows)} RFQs into 'rfqs' table...")
    await conn.copy_records_to_table(
        "rfqs",
        records=rfq_rows,
        columns=[
            "id", "user_id", "role", "status", "category", "title", "description",
            "quantity_value", "quantity_unit", "price_amount", "price_currency",
            "price_per_unit", "location_city", "location_state", "location_country",
            "latitude", "longitude", "location_raw", "deadline_at", "deadline_raw",
            "expires_at", "product_details", "search_tags", "search_text",
            "embedding_status", "embedded_at", "embedding_error", "created_at",
            "updated_at", "min_order_value", "min_order_unit"
        ]
    )

    print("Insertion complete!")

    # Verify counts in database
    user_count = await conn.fetchval("SELECT count(*) FROM users;")
    rfq_count = await conn.fetchval("SELECT count(*) FROM rfqs;")
    buyer_rfqs = await conn.fetchval("SELECT count(*) FROM rfqs WHERE role = 'buyer';")
    seller_rfqs = await conn.fetchval("SELECT count(*) FROM rfqs WHERE role = 'seller';")
    cat_distribution = await conn.fetch("SELECT category, count(*) FROM rfqs GROUP BY category ORDER BY count(*) DESC;")

    print("\n" + "=" * 70)
    print("VERIFICATION METRICS:")
    print(f"Total Users: {user_count}")
    print(f"Total RFQs: {rfq_count} (Buyer: {buyer_rfqs}, Seller: {seller_rfqs})")
    print("Category Breakdown:")
    for row in cat_distribution:
        print(f"  - {row['category']}: {row['count']} listings")
    print("=" * 70)

    await conn.close()
    print("Done! Completely fresh out-of-the-box data seeded.")


if __name__ == "__main__":
    asyncio.run(main())

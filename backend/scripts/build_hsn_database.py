"""Build and index the complete HSN/SAC master database from HSN_SAC.xlsx.

Converts all 21,935+ goods HSN codes and 681 service SAC codes into an optimized,
indexed SQLite database with full-text search (FTS5), canonical category mapping,
and commercial trade aliases (e.g., moong, chickpea, solar, basmati).
"""

import os
import re
import sqlite3
import time
import pandas as pd

def normalize_code(raw: str) -> str:
    s = str(raw).strip()
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return ""
    if len(digits) in (1, 3, 5, 7):
        digits = "0" + digits
    return digits[:8]

def map_chapter_to_category(chapter: str) -> str:
    try:
        ch = int(chapter)
    except ValueError:
        return "Industrial"
    if 1 <= ch <= 24:
        return "Agriculture"
    elif ch in (28, 29, 31, 32, 34, 38):
        return "Chemicals"
    elif ch == 30:
        return "Healthcare & Medical"
    elif ch == 33:
        return "Beauty & Personal Care"
    elif ch in (39, 48):
        return "Packaging"
    elif ch in (40, 72, 73, 74, 75, 76, 78, 79, 80, 81, 82, 83, 84):
        return "Industrial"
    elif ch in (50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65):
        return "Textiles"
    elif ch in (25, 68, 69):
        return "Construction"
    elif ch in (85,):
        return "Electronics"
    elif ch in (86, 87, 88, 89):
        return "Automotive"
    elif ch in (90,):
        return "Healthcare & Medical"
    elif ch in (94,):
        return "Furniture"
    elif ch in (49,):
        return "Stationery"
    elif ch == 99:
        return "Services"
    return "Industrial"

# Trade aliases map (code prefix -> additional search keywords)
_TRADE_ALIASES: dict[str, str] = {
    "071331": "Moong Mung Sabut Moong Green Gram Vigna Radiata Urad Black Gram Mungo Dal",
    "071320": "Kabuli Chana Desi Chana Chickpeas Garbanzo Bengal Gram Chana Dal",
    "071333": "Rajma Kidney Beans Chitra Kashmiri Rajma",
    "071340": "Masoor Dal Lentils Red Lentils Brown Lentils",
    "071310": "Matar Peas Green Peas Dry Peas White Vatana",
    "071360": "Toor Dal Tur Arhar Dal Pigeon Peas Cajanus Cajan",
    "0713": "Pulses Dals Legumes Beans Peas Grains",
    "1006": "Basmati Rice Paddy White Rice Parboiled Sona Masoori 1121 Pusa Dhan",
    "1005": "Maize Corn Sweet Corn Yellow Corn Poultry Feed Makka",
    "1001": "Wheat Gehun Sharbati Lokwan Atta Flour",
    "0910": "Ginger Turmeric Haldi Adrak Sonth Jeera Cumin Spices Masala",
    "0802": "Almonds Badam Walnuts Akhrot Dry Fruits Nuts",
    "0801": "Cashews Kaju Coconuts Nariyal",
    "1201": "Soya Beans Soybean DOC Soya Meal Oilseeds",
    "1701": "Sugar Cane Sugar M-30 S-30 Crystal Sugar Jaggery",
    "8541": "Solar Panel Solar PV Module Photovoltaic Cells Mono PERC Bifacial Solar Modules",
    "8504": "Solar Inverter Power Inverter Static Converter UPS Transformer",
    "8544": "Cables USB Cable Type-C Lightning Copper Wire Power Cable Wiring",
    "8507": "Lithium Battery LiFePO4 Storage Battery Power Bank Lead Acid",
    "8708": "Brake Pad Brake Disc Rotor Auto Parts Car Spares Shock Absorber Struts",
    "8511": "Spark Plug Glow Plug Automobile Ignition",
    "2710": "Engine Oil Motor Oil Lubricant Grease 5W-30 15W-40 Synthetic Oil",
    "5201": "Cotton Raw Cotton Cotton Bales Shankar 6 Kapas",
    "5205": "Cotton Yarn Combed Yarn 30s 40s Ring Spun Weaving",
    "6109": "T-Shirt Tshirt Polo Shirt Cotton T-Shirt Knitwear",
    "6307": "Face Mask N95 Mask Surgical Mask 3-Ply PPE Non-Woven",
    "3926": "Nitrile Gloves Blue Nitrile Examination Gloves PPE",
    "4015": "Latex Gloves Surgical Gloves Examination Rubber Gloves",
    "4819": "Corrugated Box Boxes Cartons Packaging Kraft Paper Cardboard",
    "7210": "Galvanized Steel GI Sheet Steel Coil Iron Sheet",
    "7214": "TMT Bars Sariya Steel Rebar Fe 500D Fe 550D Construction Steel",
    "7222": "Stainless Steel SS 304 SS 316L Round Bars Bright Bar",
    "7306": "Steel Pipes MS Pipes ERW Pipes Hollow Sections",
    "7408": "Copper Wire Pure Copper Winding Wire Conductor",
    "2918": "Citric Acid Food Grade Monohydrate Anhydrous Preservative",
    "2815": "Caustic Soda Sodium Hydroxide Flakes Lye NaOH",
    "3102": "Urea Fertilizer 46 Nitrogen Agricultural Fertiliser",
    "3305": "Hair Spray Shampoo Hair Dye Conditioner Hair Styling Cosmetics",
    "9018": "Syringes Needles Surgical Instruments Medical Equipment",
}

def get_aliases_for_code(code: str) -> str:
    aliases = []
    for prefix, terms in _TRADE_ALIASES.items():
        if code.startswith(prefix) or prefix.startswith(code):
            aliases.append(terms)
    return " ".join(aliases)

def build_database(excel_path: str, db_path: str):
    start_time = time.time()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE hsn_codes (
        code TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        level INTEGER NOT NULL,
        chapter TEXT NOT NULL,
        heading TEXT NOT NULL,
        subheading TEXT NOT NULL,
        category TEXT NOT NULL,
        is_service INTEGER NOT NULL DEFAULT 0,
        aliases TEXT NOT NULL DEFAULT ''
    )
    """)

    cur.execute("""
    CREATE VIRTUAL TABLE hsn_fts USING fts5(
        code,
        description,
        aliases,
        content='hsn_codes',
        content_rowid='rowid'
    )
    """)

    print(f"Loading {excel_path}...")
    xl = pd.ExcelFile(excel_path)
    rows = []

    # 1. Process Goods (HSN_MSTR)
    if "HSN_MSTR" in xl.sheet_names:
        df_hsn = xl.parse("HSN_MSTR")
        for _, r in df_hsn.iterrows():
            raw_code = r.get("HSN_CD")
            if pd.isna(raw_code):
                continue
            code = normalize_code(raw_code)
            if not code:
                continue
            desc = str(r.get("HSN_Description", "")).strip()
            level = len(code)
            chapter = code[:2]
            heading = code[:4] if level >= 4 else code
            subheading = code[:6] if level >= 6 else code
            category = map_chapter_to_category(chapter)
            aliases = get_aliases_for_code(code)
            rows.append((code, desc, level, chapter, heading, subheading, category, 0, aliases))

    # 2. Process Services (SAC_MSTR)
    if "SAC_MSTR" in xl.sheet_names:
        df_sac = xl.parse("SAC_MSTR")
        for _, r in df_sac.iterrows():
            raw_code = r.get("SAC_CD")
            if pd.isna(raw_code):
                continue
            code = normalize_code(raw_code)
            if not code:
                continue
            desc = str(r.get("SAC_Description", "")).strip()
            level = len(code)
            chapter = "99"
            heading = code[:4] if level >= 4 else "99"
            subheading = code[:6] if level >= 6 else code
            category = "Services"
            aliases = "Services GST SAC"
            rows.append((code, desc, level, chapter, heading, subheading, category, 1, aliases))

    cur.executemany(
        "INSERT OR REPLACE INTO hsn_codes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows
    )

    # Populate FTS5 index
    cur.execute("""
    INSERT INTO hsn_fts(rowid, code, description, aliases)
    SELECT rowid, code, description, aliases FROM hsn_codes
    """)

    # Indexes
    cur.execute("CREATE INDEX idx_hsn_chapter ON hsn_codes(chapter, level)")
    cur.execute("CREATE INDEX idx_hsn_heading ON hsn_codes(heading, level)")
    cur.execute("CREATE INDEX idx_hsn_subheading ON hsn_codes(subheading, level)")
    cur.execute("CREATE INDEX idx_hsn_category ON hsn_codes(category)")
    cur.execute("CREATE INDEX idx_hsn_level ON hsn_codes(level)")

    conn.commit()
    conn.close()

    total_rows = len(rows)
    elapsed = time.time() - start_time
    print(f"Successfully built {db_path} with {total_rows} codes in {elapsed:.2f}s")

if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    excel = os.path.join(os.path.dirname(base_dir), "HSN_SAC.xlsx")
    target_db = os.path.join(base_dir, "data", "hsn_master.db")
    build_database(excel, target_db)

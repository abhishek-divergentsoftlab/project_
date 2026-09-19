"""Product catalogue for seeding: deep, per-product specifications.

A listing is only as good as its attributes. "10000 mAh power bank" tells a
matcher almost nothing; cell chemistry, port count, PD wattage, output voltage
and certification are what a real buyer filters on, and what the attribute
dimension scores against.

Each product declares:
  unit        the unit it trades in
  price       a reference price in USD, converted per city at seed time
  qty         the plausible order-size range
  specs       a dict of attribute -> list of possible values; one is drawn per
              listing, so two listings of the same product differ the way real
              ones do

Values follow the normalised ``{"value", "unit"}`` form wherever a number
carries a unit, which is what later numeric matching will read.
"""

from typing import Any


def n(value: Any, unit: str) -> dict:
    """A normalised measurement."""
    return {"value": value, "unit": unit}


CATALOGUE: list[dict[str, Any]] = [
    # ======================= Electronics: power ==============================
    {
        "category": "Electronics",
        "product": "power bank",
        "unit": "pcs",
        "price_usd": 11.5,
        "qty": (500, 40000),
        "specs": {
            "capacity": [n(5000, "mAh"), n(10000, "mAh"), n(20000, "mAh"), n(27000, "mAh")],
            "output_power": [n(18, "W"), n(22.5, "W"), n(45, "W"), n(65, "W"), n(100, "W")],
            "ports": [2, 3, 4],
            "port_layout": ["2x USB-C + 1x USB-A", "1x USB-C + 1x USB-A",
                            "2x USB-C", "1x USB-C + 2x USB-A"],
            "c_to_c": [True, False],
            "cell_chemistry": ["lithium polymer", "lithium-ion 18650", "LiFePO4"],
            "output_voltage": [n(5, "V"), n(9, "V"), n(12, "V"), n(20, "V")],
            "input_voltage": [n(5, "V"), n(9, "V")],
            "fast_charge_protocol": ["USB PD 3.0", "USB PD 3.1", "Quick Charge 4+", "PPS"],
            "pass_through_charging": [True, False],
            "cycle_life": [300, 500, 800],
            "display": ["LED dots", "digital percentage", "none"],
            "casing": ["ABS plastic", "aluminium alloy", "silicone wrapped"],
            "certification": [["CE", "RoHS"], ["BIS", "CE"], ["UL", "FCC", "RoHS"], ["CE", "UN38.3"]],
            "weight": [n(120, "g"), n(220, "g"), n(410, "g"), n(560, "g")],
        },
    },
    {
        "category": "Electronics",
        "product": "USB Type-C cable",
        "unit": "pcs",
        "price_usd": 2.1,
        "qty": (1000, 80000),
        "specs": {
            "connector": ["USB-C to USB-C", "USB-C to USB-A", "USB-C to Lightning"],
            "c_to_c": [True, False],
            "current_rating": [n(2.4, "A"), n(3, "A"), n(5, "A"), n(6, "A")],
            "power_delivery": [n(60, "W"), n(100, "W"), n(240, "W")],
            "data_standard": ["USB 2.0 480Mbps", "USB 3.2 Gen1 5Gbps",
                              "USB 3.2 Gen2 10Gbps", "USB4 40Gbps"],
            "length": [n(0.5, "m"), n(1, "m"), n(1.8, "m"), n(3, "m")],
            "jacket": ["TPE", "braided nylon", "silicone", "PVC"],
            "shielding": ["foil + braid", "foil only", "double braid"],
            "e_marker_chip": [True, False],
            "color": ["white", "black", "red", "blue", "grey"],
            "bend_life": [10000, 25000, 50000],
            "certification": [["USB-IF", "RoHS"], ["CE", "RoHS"], ["UL"]],
        },
    },
    {
        "category": "Electronics",
        "product": "GaN wall charger",
        "unit": "pcs",
        "price_usd": 12.8,
        "qty": (500, 25000),
        "specs": {
            "output_power": [n(30, "W"), n(45, "W"), n(65, "W"), n(100, "W"), n(140, "W")],
            "ports": [1, 2, 3, 4],
            "port_layout": ["2x USB-C + 1x USB-A", "3x USB-C + 1x USB-A", "1x USB-C"],
            "semiconductor": ["GaN II", "GaN III", "silicon"],
            "fast_charge_protocol": ["USB PD 3.0", "USB PD 3.1 EPR", "PPS", "Quick Charge 5"],
            "input_voltage": [n("100-240", "V AC"), n("110-220", "V AC")],
            "output_voltage": [n("5/9/12/20", "V"), n("5/9/15/20", "V"), n("5/9/12/20/28", "V")],
            "plug_standard": ["Type-A US", "Type-C EU", "Type-G UK", "Type-D India", "interchangeable"],
            "foldable_pins": [True, False],
            "efficiency": ["Level VI", "Level V"],
            "certification": [["UL", "FCC", "CE"], ["BIS", "CE"], ["CE", "RoHS", "PSE"]],
        },
    },
    {
        "category": "Electronics",
        "product": "18650 lithium cell",
        "unit": "pcs",
        "price_usd": 1.9,
        "qty": (5000, 200000),
        "specs": {
            "capacity": [n(2000, "mAh"), n(2600, "mAh"), n(3000, "mAh"), n(3500, "mAh")],
            "nominal_voltage": [n(3.6, "V"), n(3.7, "V")],
            "chemistry": ["NMC", "LFP", "NCA"],
            "max_discharge": [n(10, "A"), n(20, "A"), n(30, "A")],
            "cycle_life": [500, 800, 1200],
            "internal_resistance": [n(18, "mOhm"), n(25, "mOhm"), n(35, "mOhm")],
            "tab_type": ["flat top", "button top", "spot-welded nickel"],
            "grade": ["A", "B"],
            "certification": [["UN38.3", "IEC62133"], ["CE", "UN38.3"]],
        },
    },
    # ======================= Packaging =======================================
    {
        "category": "Packaging",
        "product": "corrugated box",
        "unit": "pcs",
        "price_usd": 0.35,
        "qty": (2000, 250000),
        "specs": {
            "ply": ["3-ply", "5-ply", "7-ply"],
            "size": [
                {"value": {"length": 30, "width": 20, "height": 18}, "unit": "cm", "raw": "30x20x18"},
                {"value": {"length": 45, "width": 30, "height": 30}, "unit": "cm", "raw": "45x30x30"},
                {"value": {"length": 24, "width": 18, "height": 12}, "unit": "cm", "raw": "24x18x12"},
                {"value": {"length": 60, "width": 40, "height": 40}, "unit": "cm", "raw": "60x40x40"},
            ],
            "burst_strength": [n(12, "kgf/cm2"), n(16, "kgf/cm2"), n(20, "kgf/cm2")],
            "gsm": [120, 150, 180, 220],
            "flute": ["B flute", "C flute", "BC double wall", "E flute"],
            "printed": [True, False],
            "print_colors": [0, 1, 2, 4],
            "water_resistant_coating": [True, False],
            "recycled_content": [n(60, "%"), n(80, "%"), n(100, "%")],
            "certification": [["FSC"], ["ISO 9001"], ["FSC", "ISO 14001"]],
        },
    },
    {
        "category": "Packaging",
        "product": "FIBC bulk bag",
        "unit": "pcs",
        "price_usd": 3.7,
        "qty": (200, 20000),
        "specs": {
            "capacity": [n(500, "kg"), n(1000, "kg"), n(1500, "kg"), n(2000, "kg")],
            "safe_working_load_ratio": ["5:1", "6:1"],
            "loops": [1, 2, 4],
            "liner": [True, False],
            "fabric_weight": [n(160, "gsm"), n(200, "gsm"), n(240, "gsm")],
            "uv_stabilised": [True, False],
            "discharge": ["spout bottom", "flat bottom", "full open"],
            "filling": ["spout top", "duffle top", "open top"],
            "antistatic_type": ["Type A", "Type B", "Type C", "Type D"],
            "food_grade": [True, False],
        },
    },
    # ======================= Textiles ========================================
    {
        "category": "Textiles",
        "product": "cotton t-shirt",
        "unit": "pcs",
        "price_usd": 4.6,
        "qty": (500, 120000),
        "specs": {
            "fabric": ["100% combed cotton", "cotton-poly 60/40", "organic cotton"],
            "gsm": [140, 160, 180, 220],
            "knit": ["single jersey", "pique", "interlock"],
            "neck": ["round neck", "v-neck", "polo collar"],
            "sleeve": ["short", "long", "raglan"],
            "sizes": [["S", "M", "L", "XL"], ["M", "L", "XL", "XXL"], ["XS", "S", "M", "L", "XL", "XXL"]],
            "color": ["black", "white", "navy", "grey", "olive", "maroon"],
            "print_method": ["screen print", "DTG", "embroidery", "plain"],
            "pre_shrunk": [True, False],
            "certification": [["OEKO-TEX"], ["GOTS"], ["BCI"], []],
        },
    },
    {
        "category": "Textiles",
        "product": "denim fabric",
        "unit": "m",
        "price_usd": 3.1,
        "qty": (1000, 150000),
        "specs": {
            "gsm": [280, 320, 340, 380],
            "composition": ["100% cotton", "98% cotton 2% elastane", "cotton-poly-elastane"],
            "weave": ["3x1 right hand twill", "2x1 twill", "broken twill"],
            "stretch": [True, False],
            "width": [n(54, "inch"), n(58, "inch"), n(63, "inch")],
            "wash": ["raw", "stone wash", "enzyme wash", "bleach"],
            "color": ["indigo", "black", "grey", "ecru"],
            "shrinkage": [n(2, "%"), n(3, "%"), n(5, "%")],
        },
    },
    # ======================= Agriculture =====================================
    {
        "category": "Agriculture",
        "product": "Fuji apples",
        "unit": "kg",
        "price_usd": 2.3,
        "qty": (1000, 400000),
        "specs": {
            "variety": ["Fuji", "Royal Gala", "Granny Smith"],
            "grade": ["Extra Class", "Class I", "Class II"],
            "calibre": [n("70-75", "mm"), n("75-80", "mm"), n("80-85", "mm")],
            "brix": [n(12, "degrees"), n(14, "degrees"), n(16, "degrees")],
            "organic": [True, False],
            "packing": ["10kg carton", "18kg carton", "20kg crate"],
            "storage": ["controlled atmosphere", "cold store 0-2C", "ambient"],
            "shelf_life": [n(30, "days"), n(90, "days"), n(180, "days")],
            "certification": [["GlobalGAP"], ["GlobalGAP", "organic EU"], ["HACCP"]],
        },
    },
    {
        "category": "Agriculture",
        "product": "basmati rice",
        "unit": "tonne",
        "price_usd": 940,
        "qty": (5, 5000),
        "specs": {
            "variety": ["1121 basmati", "Pusa basmati", "traditional basmati"],
            "grain_length": [n(7.6, "mm"), n(8.3, "mm"), n(8.9, "mm")],
            "processing": ["raw", "steamed", "sella parboiled", "golden sella"],
            "aged_months": [6, 12, 24],
            "broken_percent": [n(1, "%"), n(2, "%"), n(5, "%")],
            "moisture": [n(12, "%"), n(13, "%"), n(14, "%")],
            "packing": ["25kg PP bag", "50kg jute bag", "1kg retail pack"],
            "certification": [["HACCP", "ISO 22000"], ["APEDA"], ["organic India"]],
        },
    },
    # ======================= Industrial ======================================
    {
        "category": "Industrial",
        "product": "stainless steel sheet",
        "unit": "tonne",
        "price_usd": 2150,
        "qty": (2, 3000),
        "specs": {
            "grade": ["SS 304", "SS 316L", "SS 202", "SS 430"],
            "thickness": [n(0.8, "mm"), n(1.5, "mm"), n(3, "mm"), n(6, "mm")],
            "finish": ["2B", "BA mirror", "No.4 brushed", "hot rolled"],
            "width": [n(1000, "mm"), n(1219, "mm"), n(1500, "mm")],
            "tensile_strength": [n(515, "MPa"), n(620, "MPa")],
            "form": ["coil", "cut sheet", "plate"],
            "certification": [["ASTM A240", "mill test certificate"], ["EN 10088"]],
        },
    },
    {
        "category": "Industrial",
        "product": "industrial ball bearing",
        "unit": "pcs",
        "price_usd": 4.2,
        "qty": (500, 100000),
        "specs": {
            "bore_diameter": [n(20, "mm"), n(25, "mm"), n(35, "mm"), n(50, "mm")],
            "outer_diameter": [n(47, "mm"), n(52, "mm"), n(72, "mm"), n(110, "mm")],
            "type": ["deep groove", "angular contact", "tapered roller", "self-aligning"],
            "seal": ["2RS rubber", "ZZ metal shield", "open"],
            "clearance": ["C3", "C4", "CN"],
            "max_rpm": [8000, 12000, 18000],
            "lubricant": ["lithium grease", "high-temp grease", "dry"],
            "material": ["chrome steel GCr15", "stainless 440C", "ceramic hybrid"],
        },
    },
    # ======================= Furniture =======================================
    {
        "category": "Furniture",
        "product": "dining table",
        "unit": "pcs",
        "price_usd": 150,
        "qty": (20, 4000),
        "specs": {
            "material": ["sheesham wood", "mango wood", "engineered oak", "powder-coated steel + MDF"],
            "seats": [4, 6, 8],
            "length": [n(4, "ft"), n(6, "ft"), n(8, "ft")],
            "width": [n(2.5, "ft"), n(3, "ft"), n(3.5, "ft")],
            "height": [n(30, "inch")],
            "finish": ["matte lacquer", "walnut stain", "natural oil", "high gloss"],
            "load_capacity": [n(80, "kg"), n(120, "kg")],
            "assembly": ["knock-down", "pre-assembled"],
            "edge_profile": ["straight", "bevelled", "live edge"],
        },
    },
    {
        "category": "Furniture",
        "product": "ergonomic office chair",
        "unit": "pcs",
        "price_usd": 82,
        "qty": (50, 12000),
        "specs": {
            "back_material": ["breathable mesh", "leatherette", "fabric"],
            "armrest": ["fixed", "2D", "3D", "4D"],
            "lumbar_support": ["adjustable", "fixed", "none"],
            "recline": [n(120, "degrees"), n(135, "degrees"), n(155, "degrees")],
            "gas_lift_class": ["Class 3", "Class 4"],
            "base": ["nylon 5-star", "aluminium 5-star"],
            "castor": ["PU silent", "nylon"],
            "weight_capacity": [n(110, "kg"), n(130, "kg"), n(150, "kg")],
            "headrest": [True, False],
            "certification": [["BIFMA"], ["BIFMA", "SGS"]],
        },
    },
    # ======================= Chemicals =======================================
    {
        "category": "Chemicals",
        "product": "titanium dioxide",
        "unit": "tonne",
        "price_usd": 2800,
        "qty": (1, 2000),
        "specs": {
            "crystal_form": ["rutile", "anatase"],
            "purity": [n(98, "%"), n(99, "%"), n(99.5, "%")],
            "oil_absorption": [n(18, "g/100g"), n(22, "g/100g")],
            "particle_size": [n(0.2, "micron"), n(0.3, "micron")],
            "surface_treatment": ["alumina", "alumina + silica", "untreated"],
            "application": ["coatings", "plastics", "paper", "masterbatch"],
            "packing": ["25kg bag", "500kg jumbo bag", "1000kg jumbo bag"],
        },
    },
    # ======================= Construction ====================================
    {
        "category": "Construction",
        "product": "vitrified floor tile",
        "unit": "m",
        "price_usd": 7.4,
        "qty": (500, 200000),
        "specs": {
            "size": [
                {"value": {"length": 600, "width": 600}, "unit": "mm", "raw": "600x600"},
                {"value": {"length": 800, "width": 800}, "unit": "mm", "raw": "800x800"},
                {"value": {"length": 1200, "width": 600}, "unit": "mm", "raw": "1200x600"},
            ],
            "finish": ["glossy polished", "matte", "carving", "sugar"],
            "thickness": [n(8, "mm"), n(9, "mm"), n(10, "mm")],
            "water_absorption": [n(0.08, "%"), n(0.5, "%")],
            "pei_rating": ["PEI III", "PEI IV", "PEI V"],
            "slip_resistance": ["R9", "R10", "R11"],
            "edge": ["rectified", "non-rectified"],
            "application": ["residential floor", "commercial floor", "wall"],
        },
    },
]


# --- coherence ---------------------------------------------------------------
# Attributes are drawn independently, which can produce a physically silly
# listing: a 5,000 mAh power bank that weighs 560 g, or a bearing whose outer
# diameter is smaller than its bore. These passes tie the dependent values back
# to the one they follow from. Only the combinations a buyer would notice are
# corrected -- this is demo data, not a product database.

def _cohere_power_bank(attrs: dict) -> None:
    capacity = attrs["capacity"]["value"]
    grams = {5000: 120, 10000: 220, 20000: 410, 27000: 560}
    attrs["weight"] = n(grams.get(capacity, 220), "g")
    # A 5,000 mAh cell cannot sensibly push 100 W.
    ceiling = {5000: 22.5, 10000: 45, 20000: 65, 27000: 100}[capacity]
    if attrs["output_power"]["value"] > ceiling:
        attrs["output_power"] = n(ceiling, "W")


def _cohere_bearing(attrs: dict) -> None:
    bore = attrs["bore_diameter"]["value"]
    outer = {20: 47, 25: 52, 35: 72, 50: 110}[bore]
    attrs["outer_diameter"] = n(outer, "mm")


def _cohere_cable(attrs: dict) -> None:
    # Only a C-to-C cable can carry the high-power PD profiles, and anything
    # above 60 W needs the e-marker chip.
    if attrs["connector"] != "USB-C to USB-C":
        attrs["power_delivery"] = n(60, "W")
        attrs["c_to_c"] = False
    else:
        attrs["c_to_c"] = True
    attrs["e_marker_chip"] = attrs["power_delivery"]["value"] > 60


def _cohere_box(attrs: dict) -> None:
    # Print colours only mean something on a printed box.
    if not attrs["printed"]:
        attrs["print_colors"] = 0
    elif attrs["print_colors"] == 0:
        attrs["print_colors"] = 1


COHERENCE = {
    "power bank": _cohere_power_bank,
    "industrial ball bearing": _cohere_bearing,
    "USB Type-C cable": _cohere_cable,
    "corrugated box": _cohere_box,
}

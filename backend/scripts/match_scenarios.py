#!/usr/bin/env python
"""Live matcher scenario tests: "IF a buyer wants X THEN the matcher should return Y".

Not a pytest. Run from backend/ against the dev database:

    .venv/bin/python scripts/match_scenarios.py                 # all scenarios
    .venv/bin/python scripts/match_scenarios.py --only S1,S5    # a subset
    .venv/bin/python scripts/match_scenarios.py --json out.json # machine-readable report
    .venv/bin/python scripts/match_scenarios.py --no-vector     # lexical-only fixtures (old behaviour)
    .venv/bin/python scripts/match_scenarios.py --repeat 5      # per-scenario pass rate over 5 runs

Every scenario's fixture listings are written inside ONE transaction that is
ROLLED BACK at the end: nothing is ever committed and Qdrant is never written.
The real stage 1+2 (``match_service._collect``) is run and the outcome is judged
against the scenario's expectations (include / exclude / order / top_regex+top_n).
The corpus listings in the dev DB take part too.

Retrieval (default, ``--no-vector`` to disable): in production every listing is
embedded (``rfq_indexing.build_match_text``) into Qdrant at creation, so the
matcher gets a cosine similarity for it. Fixtures are not in Qdrant, so
``match_service._vector_candidates`` is wrapped: real corpus hits come from
Qdrant, and the scenario's eligible fixtures (counterpart role, other owner,
ACTIVE, not expired, similarity above the same relevance floor) are added with
their cosine similarity to the requester (the requester's query embedding is pre-warmed so the
matcher's 1s vector timeout is not lost to a cold model), using the same embedding model and
texts as the indexer. Those fixtures are marked ``indexed`` (in the rolled-back
transaction only). With ``--no-vector`` fixtures stay ``pending`` and are found
by the SQL "unindexed" path with the lexical fallback score.

Order rule, for each (higher, lower) pair: PASS when neither is present, or only
``higher`` is present, or both are present and ``higher`` ranks above ``lower``.
FAIL when ``lower`` is present but ``higher`` is absent, or both are present in
the wrong order.

Exit code 1 if any scenario fails. Add scenarios by appending dicts to SCENARIOS.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import json
import re
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from core.config import settings  # noqa: E402
from models.enums import EmbeddingStatus, RFQRole, RFQStatus, UserRole  # noqa: E402
from models.rfq import RFQ  # noqa: E402
from models.user import User  # noqa: E402
from services import embeddings, llm_client, match_service, rfq_indexing, spec_catalog, value_canon  # noqa: E402

# Fixture words must not pollute the production canonicalizer cache: scenarios use their own file.
value_canon.reset_cache(Path(__file__).resolve().parent.parent / "data" / "value_canon_scenarios.json")
from services.categories import normalize_category  # noqa: E402

TAG = "matchscen"

CITIES = {
    "Nashik": (19.9975, 73.7898, "Maharashtra"), "Lasalgaon": (20.1500, 74.2333, "Maharashtra"),
    "Kolkata": (22.5726, 88.3639, "West Bengal"), "Chennai": (13.0827, 80.2707, "Tamil Nadu"),
    "Vapi": (20.3893, 72.9106, "Gujarat"), "Mumbai": (19.0760, 72.8777, "Maharashtra"),
    "Ludhiana": (30.9010, 75.8573, "Punjab"), "Tiruppur": (11.1085, 77.3411, "Tamil Nadu"),
    "Delhi": (28.6139, 77.2090, "Delhi"), "Indore": (22.7196, 75.8577, "Madhya Pradesh"),
    "Rajkot": (22.3039, 70.8022, "Gujarat"), "Goa": (15.4909, 73.8278, "Goa"),
}

B, S = "BUYER", "SELLER"


def L(key, role, title, cat, pd, qty, unit, price, cur, per, city, **kw):
    """Compact listing spec."""
    d = dict(key=key, role=role, title=title, cat=cat, pd=pd, qty=qty, unit=unit, price=price,
             cur=cur, per=per, city=city)
    d.update(kw)
    return d


def sc(id_, name, intent, requester, candidates, **expect):
    return dict(id=id_, name=name, intent=intent, requester=requester, candidates=candidates, **expect)


AG, IND, TXT, ELEC = "Agriculture", "Industrial", "Textiles", "Electronics"

# Requester key is always "req". Candidates default: status ACTIVE, deadline 30 days, fresh user.
SCENARIOS: list[dict[str, Any]] = [
    sc("S1", "cross-language same product", "a buyer in Nashik wants onion (50 quintal, ₹1500/quintal)",
       L("req", B, "Need onion 50 quintal", AG, {"name": "onion"}, 50, "quintal", 1500, "INR", "quintal", "Nashik"),
       [L("pyaz_lasalgaon", S, "Pyaz red onion 20 ton Lasalgaon", AG, {"name": "pyaz", "color": "red"}, 20, "tonne", 14, "INR", "kg", "Lasalgaon"),
        L("vengayam_chennai", S, "Vengayam bulk supply", AG, {"name": "vengayam"}, 10, "tonne", 16, "INR", "kg", "Chennai"),
        L("onion_kolkata", S, "Red onion stock", AG, {"name": "onion"}, 30, "tonne", 15, "INR", "kg", "Kolkata"),
        L("tomato_nashik", S, "Fresh tomato", AG, {"name": "tomato"}, 20, "tonne", 12, "INR", "kg", "Nashik"),
        L("garlic_nashik", S, "Garlic bulk", AG, {"name": "garlic"}, 5, "tonne", 60, "INR", "kg", "Nashik"),
        L("onion_seed", S, "Onion seeds hybrid", AG, {"name": "onion seeds"}, 50, "kg", 2500, "INR", "kg", "Nashik"),
        L("onion_powder", S, "Dehydrated onion powder", AG, {"name": "onion powder"}, 2, "tonne", 180, "INR", "kg", "Mumbai")],
       include=["pyaz_lasalgaon", "vengayam_chennai", "onion_kolkata"],
       exclude=["tomato_nashik", "garlic_nashik", "onion_seed"],
       order=[("pyaz_lasalgaon", "onion_kolkata"), ("onion_kolkata", "onion_powder"), ("pyaz_lasalgaon", "onion_seed")],
       top_regex="onion|pyaz|vengayam|kanda|ulli", top_n=5),
    sc("S2", "spec specificity", "a buyer in Mumbai wants SS 316L sheet 2mm (5 tonne, ₹250/kg)",
       L("req", B, "SS 316L sheet 2mm", IND, {"name": "stainless steel sheet", "grade": "316L", "thickness": "2mm"}, 5, "tonne", 250, "INR", "kg", "Mumbai"),
       [L("ss316l_vapi", S, "SS 316L sheets 2mm", IND, {"name": "stainless steel sheet", "grade": "316L", "thickness": "2mm"}, 10, "tonne", 245, "INR", "kg", "Vapi"),
        L("ss304_vapi", S, "SS 304 sheets 2mm", IND, {"name": "stainless steel sheet", "grade": "304", "thickness": "2mm"}, 10, "tonne", 190, "INR", "kg", "Vapi"),
        L("ms_sheet", S, "MS sheet 2mm", IND, {"name": "mild steel sheet", "thickness": "2mm"}, 10, "tonne", 60, "INR", "kg", "Vapi"),
        L("ss316l_pipe", S, "SS 316L pipe 2 inch", IND, {"name": "stainless steel pipe", "grade": "316L"}, 5, "tonne", 260, "INR", "kg", "Mumbai")],
       include=["ss316l_vapi"], exclude=["ms_sheet"],
       order=[("ss316l_vapi", "ss304_vapi"), ("ss304_vapi", "ms_sheet"), ("ss316l_vapi", "ss316l_pipe")]),
    sc("S3", "negation", "a buyer in Tiruppur wants cotton t-shirts, not black (2000 pcs, ₹150)",
       L("req", B, "Cotton t-shirts, not black", TXT, {"name": "cotton t-shirt", "negated_attributes": ["black"]}, 2000, "pcs", 150, "INR", "pcs", "Tiruppur"),
       [L("tshirt_white", S, "Cotton t-shirt white", TXT, {"name": "cotton t-shirt", "color": "white"}, 5000, "pcs", 140, "INR", "pcs", "Tiruppur"),
        L("tshirt_black", S, "Cotton t-shirt black", TXT, {"name": "cotton t-shirt", "color": "black"}, 5000, "pcs", 140, "INR", "pcs", "Tiruppur")],
       include=["tshirt_white"], order=[("tshirt_white", "tshirt_black")]),
    sc("S4", "price with unit conversion", "a buyer in Nashik wants onion 10 tonne at ₹20/kg",
       L("req", B, "Onion 10 tonne", AG, {"name": "onion"}, 10, "tonne", 20, "INR", "kg", "Nashik"),
       [L("onion_cheap_qtl", S, "Onion Nashik lot A", AG, {"name": "onion"}, 10, "tonne", 1800, "INR", "quintal", "Nashik"),
        L("onion_dear_kg", S, "Onion Nashik lot B", AG, {"name": "onion"}, 10, "tonne", 30, "INR", "kg", "Nashik")],
       include=["onion_cheap_qtl", "onion_dear_kg"], order=[("onion_cheap_qtl", "onion_dear_kg")]),
    sc("S5", "quantity coverage", "a buyer in Indore wants wheat 50 quintal at ₹2400/quintal",
       L("req", B, "Wheat 50 quintal", AG, {"name": "wheat"}, 50, "quintal", 2400, "INR", "quintal", "Indore"),
       [L("wheat_full", S, "Wheat sharbati 10 ton", AG, {"name": "wheat"}, 10, "tonne", 2350, "INR", "quintal", "Indore"),
        L("wheat_partial", S, "Wheat sharbati 2 ton", AG, {"name": "wheat"}, 2, "tonne", 2350, "INR", "quintal", "Indore")],
       order=[("wheat_full", "wheat_partial")]),
    sc("S6", "location", "a buyer in Rajkot wants cumin seeds 2 tonne at ₹300/kg",
       L("req", B, "Cumin seeds 2 tonne", AG, {"name": "cumin"}, 2, "tonne", 300, "INR", "kg", "Rajkot"),
       [L("cumin_near", S, "Cumin seeds jeera", AG, {"name": "cumin"}, 2, "tonne", 300, "INR", "kg", "Rajkot"),
        L("cumin_far", S, "Cumin seeds jeera", AG, {"name": "cumin"}, 2, "tonne", 300, "INR", "kg", "Kolkata")],
       order=[("cumin_near", "cumin_far")]),
    sc("S7", "role direction", "a buyer in Indore wants turmeric 5 tonne",
       L("req", B, "Turmeric 5 tonne", AG, {"name": "turmeric"}, 5, "tonne", None, None, None, "Indore"),
       [L("turmeric_seller", S, "Turmeric finger Erode", AG, {"name": "turmeric"}, 10, "tonne", None, None, None, "Chennai"),
        L("turmeric_buyer", B, "Need turmeric 3 tonne", AG, {"name": "turmeric"}, 3, "tonne", None, None, None, "Indore")],
       include=["turmeric_seller"], exclude=["turmeric_buyer"]),
    sc("S8", "own / closed / expired", "a seller in Delhi offers basmati rice 20 tonne",
       L("req", S, "Basmati rice 20 tonne", AG, {"name": "basmati rice"}, 20, "tonne", None, None, None, "Delhi", owner="U"),
       [L("rice_own", B, "Need basmati rice", AG, {"name": "basmati rice"}, 10, "tonne", None, None, None, "Delhi", owner="U"),
        L("rice_closed", B, "Need basmati rice", AG, {"name": "basmati rice"}, 10, "tonne", None, None, None, "Delhi", status="CLOSED"),
        L("rice_expired", B, "Need basmati rice", AG, {"name": "basmati rice"}, 10, "tonne", None, None, None, "Delhi", expired=True),
        L("rice_ok", B, "Need basmati rice", AG, {"name": "basmati rice"}, 10, "tonne", None, None, None, "Delhi")],
       include=["rice_ok"], exclude=["rice_own", "rice_closed", "rice_expired"]),
    sc("S9", "substitute names", "a buyer in Delhi wants hair dye 2000 bottles",
       L("req", B, "Hair dye 2000 bottles", "Beauty & Personal Care", {"name": "hair dye"}, 2000, "bottles", None, None, None, "Delhi"),
       [L("hair_colour", S, "Hair colour cream", "Beauty & Personal Care", {"name": "hair colour"}, 5000, "bottles", None, None, None, "Delhi"),
        L("hair_oil", S, "Coconut hair oil", "Beauty & Personal Care", {"name": "hair oil"}, 5000, "bottles", None, None, None, "Delhi")],
       include=["hair_colour"], exclude=["hair_oil"], order=[("hair_colour", "hair_oil")]),
    sc("S10", "distinct pulses", "a buyer in Indore wants chana 10 tonne",
       L("req", B, "Chana 10 tonne", AG, {"name": "chana"}, 10, "tonne", None, None, None, "Indore"),
       [L("chickpea", S, "Kabuli chickpea", AG, {"name": "chickpea"}, 20, "tonne", None, None, None, "Indore"),
        L("moong", S, "Green moong dal", AG, {"name": "moong"}, 20, "tonne", None, None, None, "Indore")],
       include=["chickpea"], exclude=["moong"]),
    sc("S11", "broad request", "a buyer in Mumbai wants vegetables for a hotel",
       L("req", B, "Vegetables for hotel", AG, {"name": "vegetables"}, None, None, None, None, None, "Mumbai"),
       [L("veg_onion", S, "Onion", AG, {"name": "onion"}, 5, "tonne", None, None, None, "Mumbai"),
        L("veg_tomato", S, "Tomato", AG, {"name": "tomato"}, 5, "tonne", None, None, None, "Mumbai"),
        L("veg_laptop", S, "Laptop i5", ELEC, {"name": "laptop"}, 50, "pcs", None, None, None, "Mumbai")],
       include=["veg_onion", "veg_tomato"], exclude=["veg_laptop"]),
    sc("S12", "currency", "a buyer in Mumbai wants onion 20 tonne export at $0.25/kg",
       L("req", B, "Onion 20 tonne export", AG, {"name": "onion"}, 20, "tonne", 0.25, "USD", "kg", "Mumbai"),
       [L("onion_inr_ok", S, "Onion export quality", AG, {"name": "onion"}, 20, "tonne", 18, "INR", "kg", "Mumbai"),
        L("onion_inr_high", S, "Onion premium", AG, {"name": "onion"}, 20, "tonne", 45, "INR", "kg", "Mumbai")],
       order=[("onion_inr_ok", "onion_inr_high")]),
    sc("S13", "electronics spec", "a buyer in Delhi wants USB Type-C cable 1m (1000 pcs, $2)",
       L("req", B, "USB Type-C cable 1m", ELEC, {"name": "usb type-c cable", "length": "1m"}, 1000, "pcs", 2, "USD", "pcs", "Delhi"),
       [L("typec", S, "USB Type-C cable 1m braided", ELEC, {"name": "usb type-c cable", "length": "1m"}, 5000, "pcs", None, None, None, "Delhi"),
        L("typea", S, "USB Type-A to micro USB cable", ELEC, {"name": "usb type-a cable"}, 5000, "pcs", None, None, None, "Delhi"),
        L("hdmi", S, "HDMI cable 2m", ELEC, {"name": "hdmi cable"}, 5000, "pcs", None, None, None, "Delhi")],
       include=["typec"], exclude=["hdmi"], order=[("typec", "typea"), ("typea", "hdmi")]),
    sc("S14", "local-name requester", "a buyer in Nashik says 'Tamatar chahiye' (5 tonne)",
       L("req", B, "Tamatar chahiye", AG, {"name": "tamatar"}, 5, "tonne", None, None, None, "Nashik"),
       [L("tomato_a", S, "Fresh tomato hybrid", AG, {"name": "tomato"}, 10, "tonne", None, None, None, "Nashik"),
        L("potato_a", S, "Potato", AG, {"name": "potato"}, 10, "tonne", None, None, None, "Nashik")],
       include=["tomato_a"], exclude=["potato_a"]),
    sc("S15", "derived product", "a buyer in Nashik wants fresh tomato 10 tonne",
       L("req", B, "Fresh tomato 10 tonne", AG, {"name": "tomato"}, 10, "tonne", None, None, None, "Nashik"),
       [L("tomato_fresh", S, "Tomato fresh", AG, {"name": "tomato"}, 10, "tonne", None, None, None, "Nashik"),
        L("ketchup", S, "Tomato ketchup 1kg pouch", "Food & Beverages", {"name": "tomato ketchup"}, 10, "tonne", None, None, None, "Nashik")],
       include=["tomato_fresh"], exclude=["ketchup"], order=[("tomato_fresh", "ketchup")]),
    sc("S16", "copper forms", "a buyer in Rajkot wants copper wire 8mm (2 tonne)",
       L("req", B, "Copper wire 8mm", IND, {"name": "copper wire", "diameter": "8mm"}, 2, "tonne", None, None, None, "Rajkot"),
       [L("cu_wire", S, "Copper wire rod 8mm", IND, {"name": "copper wire", "diameter": "8mm"}, 5, "tonne", None, None, None, "Rajkot"),
        L("cu_scrap", S, "Copper scrap", IND, {"name": "copper scrap"}, 5, "tonne", None, None, None, "Rajkot"),
        L("cu_pipe", S, "Copper pipe", IND, {"name": "copper pipe"}, 5, "tonne", None, None, None, "Rajkot")],
       include=["cu_wire"], order=[("cu_wire", "cu_scrap"), ("cu_wire", "cu_pipe")]),
    sc("S17", "cross-script", "a buyer in Nashik writes 'प्याज चाहिए 10 टन' (Devanagari)",
       L("req", B, "प्याज चाहिए 10 टन", AG, {"name": "प्याज"}, 10, "tonne", None, None, None, "Nashik"),
       [L("onion_en", S, "Onion red Nashik", AG, {"name": "onion"}, 20, "tonne", None, None, None, "Nashik"),
        L("potato_en", S, "Potato Agra", AG, {"name": "potato"}, 20, "tonne", None, None, None, "Delhi")],
       include=["onion_en"], exclude=["potato_en"]),
    sc("S18", "seller finds buyers", "a seller in Chennai offers turmeric finger 10 tonne",
       L("req", S, "Turmeric finger 10 tonne", AG, {"name": "turmeric"}, 10, "tonne", None, None, None, "Chennai"),
       [L("buy_haldi", B, "Haldi chahiye 5 ton", AG, {"name": "haldi"}, 5, "tonne", None, None, None, "Indore"),
        L("buy_chilli", B, "Need red chilli", AG, {"name": "red chilli"}, 5, "tonne", None, None, None, "Indore")],
       include=["buy_haldi"], exclude=["buy_chilli"]),
    sc("S19", "broad seller", "a seller in Mumbai supplies fresh vegetables",
       L("req", S, "Fresh vegetables supply", AG, {"name": "vegetables"}, None, None, None, None, None, "Mumbai"),
       [L("need_onion", B, "Need onion 2 tonne", AG, {"name": "onion"}, 2, "tonne", None, None, None, "Mumbai"),
        L("need_tomato", B, "Need tomato", AG, {"name": "tomato"}, None, None, None, None, None, "Mumbai"),
        L("need_steel", B, "Need MS pipes", IND, {"name": "ms pipes"}, None, None, None, None, None, "Mumbai")],
       include=["need_onion", "need_tomato"], exclude=["need_steel"]),
    sc("S23", "broad buyer, specific seller", "a seller in Nashik offers onion 20 tonne",
       L("req", S, "Onion 20 tonne Nashik", AG, {"name": "onion"}, 20, "tonne", None, None, None, "Nashik"),
       [L("need_veg", B, "Vegetables for hotel chain", AG, {"name": "vegetables"}, None, None, None, None, None, "Mumbai"),
        L("need_onion2", B, "Need onion 5 tonne", AG, {"name": "onion"}, 5, "tonne", None, None, None, "Mumbai"),
        L("need_laptop", B, "Need laptops i5", "Electronics", {"name": "laptop"}, 50, "pcs", None, None, None, "Mumbai")],
       include=["need_veg", "need_onion2"], exclude=["need_laptop"], order=[("need_onion2", "need_veg")]),
    sc("S20","MOQ / quantity mismatch", "a buyer in Mumbai wants cashew W320 100 kg at ₹750/kg",
       L("req", B, "Cashew W320 100 kg", AG, {"name": "cashew", "grade": "W320"}, 100, "kg", 750, "INR", "kg", "Mumbai"),
       [L("cashew_small", S, "Cashew W320 retail lot", AG, {"name": "cashew", "grade": "W320"}, 200, "kg", 760, "INR", "kg", "Goa"),
        L("cashew_moq", S, "Cashew W320 container", AG, {"name": "cashew", "grade": "W320", "moq": "10 tonne"}, 15, "tonne", 700, "INR", "kg", "Goa")],
       order=[("cashew_small", "cashew_moq")]),
    sc("S21", "deadline", "a buyer in Delhi needs basmati rice within 7 days",
       L("req", B, "Basmati rice 10 tonne", AG, {"name": "basmati rice"}, 10, "tonne", None, None, None, "Delhi", deadline_days=7),
       [L("rice_soon", S, "Basmati rice ready stock", AG, {"name": "basmati rice"}, 10, "tonne", None, None, None, "Delhi", deadline_days=5),
        L("rice_late", S, "Basmati rice ready stock", AG, {"name": "basmati rice"}, 10, "tonne", None, None, None, "Delhi", deadline_days=60)],
       order=[("rice_soon", "rice_late")]),
    sc("S22", "spelling noise", "a buyer in Nashik writes 'tamoto 5 ton'",
       L("req", B, "tamoto 5 ton", AG, {"name": "tamoto"}, 5, "tonne", None, None, None, "Nashik"),
       [L("tomato_b", S, "Tomato", AG, {"name": "tomato"}, 10, "tonne", None, None, None, "Nashik"),
        L("onion_b", S, "Onion", AG, {"name": "onion"}, 10, "tonne", None, None, None, "Nashik")],
       include=["tomato_b"], exclude=["onion_b"]),
    sc("S24", "grade written without a space", "a buyer in Mumbai wants 'SS304 cold rolled sheet 2mm' (grade SS304)",
       L("req", B, "SS304 cold rolled sheet 2mm", IND, {"name": "SS304 cold rolled sheet", "grade": "SS304"}, 2, "tonne", None, None, None, "Mumbai"),
       [L("ss304_spaced", S, "Cold Rolled Stainless Steel Sheet SS 304", IND,
          {"name": "Cold Rolled Stainless Steel Sheet", "grade": "SS 304"}, 5, "tonne", None, None, None, "Mumbai"),
        L("ss316_sheet", S, "Cold Rolled Stainless Steel Sheet SS 316", IND,
          {"name": "Cold Rolled Stainless Steel Sheet", "grade": "SS 316"}, 5, "tonne", None, None, None, "Mumbai")],
       include=["ss304_spaced"], order=[("ss304_spaced", "ss316_sheet")]),
    sc("S25", "pipe size as inch vs NB", "a buyer in Mumbai wants MS pipe 1/2 inch",
       L("req", B, "MS pipe 1/2 inch", IND, {"name": "MS pipe", "diameter": "1/2 inch"}, 500, "m", None, None, None, "Mumbai"),
       [L("pipe_15nb", S, "Mild Steel Pipe 15 NB", IND, {"name": "mild steel pipe", "diameter": "15 NB"}, 1000, "m", None, None, None, "Mumbai"),
        L("pipe_50nb", S, "Mild Steel Pipe 50 NB", IND, {"name": "mild steel pipe", "diameter": "50 NB"}, 1000, "m", None, None, None, "Mumbai")],
       include=["pipe_15nb"], order=[("pipe_15nb", "pipe_50nb")]),
    # ---- adversarial set A1-A25 (same thing written differently by buyer and seller) ----
    sc("A1", "steel grade + thickness aliases", "buyer: SS 304 sheet 2mm",
       L("req", B, "SS 304 sheet 2mm", IND, {"name": "stainless steel sheet", "grade": "304", "thickness": "2mm"}, 5, "tonne", 200, "INR", "kg", "Mumbai"),
       [L("aisi", S, "AISI 304 stainless sheet 2 mm", IND, {"name": "stainless sheet", "grade": "AISI 304", "thickness": "2 mm"}, 10, "tonne", 195, "INR", "kg", "Vapi"),
        L("cm", S, "Stainless Steel 304 plate 0.2 cm", IND, {"name": "stainless steel plate", "grade": "SS-304", "thickness": "0.2 cm"}, 10, "tonne", 195, "INR", "kg", "Vapi"),
        L("ss316", S, "SS 316 sheet 2mm", IND, {"name": "stainless steel sheet", "grade": "316", "thickness": "2mm"}, 10, "tonne", 195, "INR", "kg", "Vapi"),
        L("ss304_5mm", S, "SS 304 sheet 5mm", IND, {"name": "stainless steel sheet", "grade": "304", "thickness": "5mm"}, 10, "tonne", 195, "INR", "kg", "Vapi")],
       include=["aisi", "cm"], order=[("aisi", "ss316"), ("aisi", "ss304_5mm"), ("cm", "ss316"), ("cm", "ss304_5mm")]),
    sc("A2", "yarn count notations", "buyer: cotton yarn 30s combed",
       L("req", B, "Cotton yarn 30s combed", TXT, {"name": "cotton yarn", "count": "30s", "type": "combed"}, 5, "tonne", 280, "INR", "kg", "Tiruppur"),
       [L("ne", S, "30 Ne combed cotton yarn", TXT, {"name": "cotton yarn", "count": "30 Ne", "type": "combed"}, 10, "tonne", 275, "INR", "kg", "Tiruppur"),
        L("slash", S, "Cotton yarn 30/1 combed", TXT, {"name": "cotton yarn", "count": "30/1", "type": "combed"}, 10, "tonne", 275, "INR", "kg", "Tiruppur"),
        L("c40", S, "Cotton yarn 40s carded", TXT, {"name": "cotton yarn", "count": "40s", "type": "carded"}, 10, "tonne", 275, "INR", "kg", "Tiruppur")],
       include=["ne", "slash"], order=[("ne", "c40"), ("slash", "c40")]),
    sc("A3", "TMT grade/size spellings", "buyer: TMT bar Fe500D 12mm",
       L("req", B, "TMT bar Fe500D 12mm", IND, {"name": "tmt bar", "grade": "Fe500D", "diameter": "12mm"}, 20, "tonne", 55, "INR", "kg", "Indore"),
       [L("saria", S, "TMT saria Fe 500 D 12 mm", IND, {"name": "tmt saria", "grade": "Fe 500 D", "size": "12 mm"}, 50, "tonne", 54, "INR", "kg", "Indore"),
        L("rebar", S, "Fe-500D TMT rebar 12mm", IND, {"name": "tmt rebar", "grade": "Fe-500D", "diameter": "12mm"}, 50, "tonne", 54, "INR", "kg", "Indore"),
        L("fe415", S, "TMT bar Fe415 12mm", IND, {"name": "tmt bar", "grade": "Fe415", "diameter": "12mm"}, 50, "tonne", 54, "INR", "kg", "Indore"),
        L("d25", S, "TMT bar Fe500D 25mm", IND, {"name": "tmt bar", "grade": "Fe500D", "diameter": "25mm"}, 50, "tonne", 54, "INR", "kg", "Indore")],
       include=["saria", "rebar"], order=[("saria", "fe415"), ("saria", "d25"), ("rebar", "fe415"), ("rebar", "d25")]),
    sc("A4", "chemical synonyms + form", "buyer: caustic soda flakes 99%",
       L("req", B, "Caustic soda flakes 99%", IND, {"name": "caustic soda", "form": "flakes", "purity": "99%"}, 10, "tonne", 45, "INR", "kg", "Vapi"),
       [L("naoh_name", S, "Sodium hydroxide flakes 99 percent", IND, {"name": "sodium hydroxide", "form": "flakes", "purity": "99 percent"}, 20, "tonne", 44, "INR", "kg", "Vapi"),
        L("naoh", S, "NaOH flakes 99%", IND, {"name": "NaOH", "form": "flakes", "purity": "99%"}, 20, "tonne", 44, "INR", "kg", "Vapi"),
        L("lye", S, "Caustic soda lye 48%", IND, {"name": "caustic soda", "form": "lye", "purity": "48%"}, 20, "tonne", 20, "INR", "kg", "Vapi"),
        L("soda_ash", S, "Soda ash light", IND, {"name": "soda ash", "form": "powder"}, 20, "tonne", 30, "INR", "kg", "Vapi")],
       include=["naoh_name", "naoh"], exclude=["soda_ash"], order=[("naoh_name", "lye"), ("naoh", "lye")]),
    sc("A5", "brinjal names", "buyer: brinjal 5 tonne",
       L("req", B, "Brinjal 5 tonne", AG, {"name": "brinjal"}, 5, "tonne", 20, "INR", "kg", "Nashik"),
       [L("baingan", S, "Baingan fresh", AG, {"name": "baingan"}, 10, "tonne", 18, "INR", "kg", "Nashik"),
        L("eggplant", S, "Eggplant purple", AG, {"name": "eggplant", "color": "purple"}, 10, "tonne", 18, "INR", "kg", "Nashik"),
        L("aubergine", S, "Aubergine export quality", AG, {"name": "aubergine"}, 10, "tonne", 18, "INR", "kg", "Mumbai"),
        L("bhindi", S, "Bhindi fresh", AG, {"name": "bhindi"}, 10, "tonne", 25, "INR", "kg", "Nashik")],
       include=["baingan", "eggplant", "aubergine"], exclude=["bhindi"]),
    sc("A6", "groundnut names", "buyer: groundnut kernels 10 tonne",
       L("req", B, "Groundnut kernels", AG, {"name": "groundnut kernels"}, 10, "tonne", 90, "INR", "kg", "Rajkot"),
       [L("peanut", S, "Peanut kernels bold 40/50", AG, {"name": "peanut kernels", "size": "40/50"}, 20, "tonne", 88, "INR", "kg", "Rajkot"),
        L("moongfali", S, "Moongfali dana", AG, {"name": "moongfali"}, 20, "tonne", 88, "INR", "kg", "Rajkot"),
        L("gn_oil", S, "Groundnut oil filtered", AG, {"name": "groundnut oil"}, 20, "tonne", 160, "INR", "kg", "Rajkot")],
       include=["peanut", "moongfali"], order=[("peanut", "gn_oil"), ("moongfali", "gn_oil")]),
    sc("A7", "coriander seed vs leaf", "buyer: coriander seeds 2 tonne",
       L("req", B, "Coriander seeds", AG, {"name": "coriander seeds"}, 2, "tonne", 90, "INR", "kg", "Rajkot"),
       [L("dhaniya", S, "Dhaniya seed eagle quality", AG, {"name": "dhaniya seed"}, 10, "tonne", 88, "INR", "kg", "Rajkot"),
        L("leaves", S, "Fresh coriander leaves", AG, {"name": "coriander leaves"}, 1, "tonne", 40, "INR", "kg", "Rajkot"),
        L("cumin", S, "Cumin seeds", AG, {"name": "cumin seeds"}, 10, "tonne", 300, "INR", "kg", "Rajkot")],
       include=["dhaniya"], exclude=["cumin"], order=[("dhaniya", "leaves")]),
    sc("A8", "maize / corn / makka", "buyer: yellow maize feed grade 50 tonne",
       L("req", B, "Yellow maize feed grade", AG, {"name": "maize", "color": "yellow", "grade": "feed"}, 50, "tonne", 22, "INR", "kg", "Indore"),
       [L("makka", S, "Makka peela", AG, {"name": "makka"}, 100, "tonne", 21, "INR", "kg", "Indore"),
        L("corn", S, "Yellow corn for poultry feed", AG, {"name": "yellow corn"}, 100, "tonne", 21, "INR", "kg", "Indore"),
        L("sweet", S, "Frozen sweet corn kernels", AG, {"name": "sweet corn", "form": "frozen"}, 5, "tonne", 80, "INR", "kg", "Indore")],
       include=["makka", "corn"], order=[("makka", "sweet"), ("corn", "sweet")]),
    sc("A9", "shouty/emoji seller title", "buyer: red onion 20 tonne",
       L("req", B, "Red onion 20 tonne", AG, {"name": "onion", "color": "red"}, 20, "tonne", 15, "INR", "kg", "Nashik"),
       [L("shout", S, "🔥🔥BEST ONIONS!!! CALL NOW 9999999999🔥", AG, {"name": "ONIONS"}, 50, "tonne", 14, "INR", "kg", "Nashik"),
        L("stuffer", S, "onion potato tomato garlic ginger rice wheat dal sugar all available", AG, {"name": "vegetables"}, 50, "tonne", 14, "INR", "kg", "Nashik"),
        L("real", S, "Red onion Nashik", AG, {"name": "onion", "color": "red"}, 50, "tonne", 14, "INR", "kg", "Nashik"),
        L("junk", S, "asdf test", AG, {"name": "test"}, 1, "kg", 1, "INR", "kg", "Nashik")],
       include=["shout", "real"], exclude=["junk"], order=[("real", "stuffer"), ("shout", "stuffer")]),
    sc("A10", "keyword stuffer vs specific", "buyer: 1121 basmati sella rice",
       L("req", B, "1121 basmati sella rice", AG, {"name": "basmati rice", "variety": "1121", "type": "sella"}, 20, "tonne", 90, "INR", "kg", "Delhi"),
       [L("stuffer", S, "rice wheat onion sugar dal basmati everything available", AG, {"name": "rice"}, 100, "tonne", 50, "INR", "kg", "Delhi"),
        L("parboiled", S, "1121 parboiled basmati rice", AG, {"name": "basmati rice", "variety": "1121", "type": "parboiled"}, 50, "tonne", 88, "INR", "kg", "Delhi"),
        L("golden", S, "Pusa 1121 golden sella", AG, {"name": "basmati rice", "variety": "pusa 1121", "type": "golden sella"}, 50, "tonne", 88, "INR", "kg", "Delhi"),
        L("b1509", S, "1509 basmati steam", AG, {"name": "basmati rice", "variety": "1509", "type": "steam"}, 50, "tonne", 80, "INR", "kg", "Delhi"),
        L("sona", S, "Sona masoori rice", AG, {"name": "sona masoori rice"}, 50, "tonne", 45, "INR", "kg", "Delhi")],
       include=["parboiled", "golden"], order=[("golden", "stuffer"), ("parboiled", "stuffer"), ("golden", "b1509"), ("parboiled", "sona")]),
    sc("A11", "USB-C spellings + length units", "buyer: USB Type-C cable 1m",
       L("req", B, "USB Type-C cable 1m", ELEC, {"name": "usb type-c cable", "length": "1m"}, 1000, "pcs", 120, "INR", "pcs", "Delhi"),
       [L("cord", S, "USB-C to USB-C cord 100cm", ELEC, {"name": "usb-c cord", "length": "100cm"}, 5000, "pcs", 110, "INR", "pcs", "Delhi"),
        L("typec", S, "type c charging cable 1 metre", ELEC, {"name": "type c charging cable", "length": "1 metre"}, 5000, "pcs", 110, "INR", "pcs", "Delhi"),
        L("micro", S, "Micro USB cable 1m", ELEC, {"name": "micro usb cable", "length": "1m"}, 5000, "pcs", 60, "INR", "pcs", "Delhi"),
        L("lightning", S, "Lightning cable 1m", ELEC, {"name": "lightning cable", "length": "1m"}, 5000, "pcs", 150, "INR", "pcs", "Delhi")],
       include=["cord", "typec"], order=[("cord", "micro"), ("typec", "micro"), ("cord", "lightning"), ("typec", "lightning")]),
    sc("A12", "GSM notations", "buyer: cotton fabric 180 gsm",
       L("req", B, "Cotton fabric 180 gsm", TXT, {"name": "cotton fabric", "gsm": "180"}, 5000, "m", 120, "INR", "m", "Tiruppur"),
       [L("gm2", S, "Cotton cloth 180 g/m2", TXT, {"name": "cotton cloth", "weight": "180 g/m2"}, 10000, "m", 115, "INR", "m", "Tiruppur"),
        L("g120", S, "Cotton fabric 120 gsm", TXT, {"name": "cotton fabric", "gsm": "120"}, 10000, "m", 90, "INR", "m", "Tiruppur")],
       include=["gm2"], order=[("gm2", "g120")]),
    sc("A13", "FIBC / jumbo bag / big bag", "buyer: FIBC jumbo bag 1000 kg",
       L("req", B, "FIBC jumbo bag 1000 kg", IND, {"name": "fibc jumbo bag", "capacity": "1000 kg"}, 2000, "pcs", 450, "INR", "pcs", "Vapi"),
       [L("bulk", S, "Bulk bag 1 ton SWL", IND, {"name": "bulk bag", "capacity": "1 ton"}, 10000, "pcs", 430, "INR", "pcs", "Vapi"),
        L("big", S, "Big bag 1000kg", IND, {"name": "big bag", "capacity": "1000kg"}, 10000, "pcs", 430, "INR", "pcs", "Vapi"),
        L("sack", S, "PP woven sack 50kg", IND, {"name": "pp woven sack", "capacity": "50kg"}, 100000, "pcs", 12, "INR", "pcs", "Vapi")],
       include=["bulk", "big"], order=[("bulk", "sack"), ("big", "sack")]),
    sc("A14", "seller picked wrong category", "buyer: onion 10 tonne (Agriculture)",
       L("req", B, "Onion 10 tonne", AG, {"name": "onion"}, 10, "tonne", 15, "INR", "kg", "Nashik"),
       [L("wrongcat", S, "Onion red fresh", IND, {"name": "onion"}, 20, "tonne", 14, "INR", "kg", "Nashik"),
        L("garlic", S, "Garlic", AG, {"name": "garlic"}, 20, "tonne", 60, "INR", "kg", "Nashik")],
       include=["wrongcat"], order=[("wrongcat", "garlic")]),
    sc("A15", "price per MT vs per kg vs per quintal", "buyer: wheat 50 tonne at ₹24000/tonne",
       L("req", B, "Wheat 50 tonne", AG, {"name": "wheat"}, 50, "tonne", 24000, "INR", "tonne", "Indore"),
       [L("cheap_kg", S, "Wheat lot A", AG, {"name": "wheat"}, 100, "tonne", 23, "INR", "kg", "Indore"),
        L("dear_qtl", S, "Wheat lot B", AG, {"name": "wheat"}, 100, "tonne", 2700, "INR", "quintal", "Indore")],
       include=["cheap_kg", "dear_qtl"], order=[("cheap_kg", "dear_qtl")]),
    sc("A16", "quantity in bags", "buyer: sugar 500 bags of 50 kg (=25 t)",
       L("req", B, "Sugar 500 bags 50kg", AG, {"name": "sugar", "packing": "50 kg bag"}, 500, "bags", 40, "INR", "kg", "Mumbai"),
       [L("full", S, "Sugar S30 25 tonne", AG, {"name": "sugar"}, 30, "tonne", 39, "INR", "kg", "Mumbai"),
        L("partial", S, "Sugar S30 2 tonne", AG, {"name": "sugar"}, 2, "tonne", 39, "INR", "kg", "Mumbai")],
       include=["full", "partial"], order=[("full", "partial")]),
    sc("A17", "organic certification", "buyer: organic turmeric",
       L("req", B, "Organic turmeric finger", AG, {"name": "turmeric finger", "certification": "organic"}, 5, "tonne", 150, "INR", "kg", "Chennai"),
       [L("conv", S, "Turmeric finger", AG, {"name": "turmeric finger"}, 10, "tonne", 120, "INR", "kg", "Chennai"),
        L("org", S, "Organic turmeric finger NPOP certified", AG, {"name": "turmeric finger", "certification": "NPOP organic"}, 10, "tonne", 145, "INR", "kg", "Chennai")],
       include=["org"], order=[("org", "conv")]),
    sc("A18", "colour in Hindi", "buyer: red onion",
       L("req", B, "Red onion", AG, {"name": "onion", "color": "red"}, 10, "tonne", 15, "INR", "kg", "Nashik"),
       [L("lal", S, "Lal pyaz", AG, {"name": "pyaz", "color": "lal"}, 20, "tonne", 14, "INR", "kg", "Nashik"),
        L("white", S, "White onion", AG, {"name": "onion", "color": "white"}, 20, "tonne", 14, "INR", "kg", "Nashik")],
       include=["lal"], order=[("lal", "white")]),
    sc("A19", "misspelt requester", "buyer: 'potatos' (sic)",
       L("req", B, "potatos", AG, {"name": "potatos"}, 10, "tonne", 12, "INR", "kg", "Indore"),
       [L("potato", S, "Potato jyoti", AG, {"name": "potato"}, 20, "tonne", 11, "INR", "kg", "Indore"),
        L("aloo", S, "Aloo fresh", AG, {"name": "aloo"}, 20, "tonne", 11, "INR", "kg", "Indore"),
        L("sweet", S, "Sweet potato", AG, {"name": "sweet potato"}, 20, "tonne", 25, "INR", "kg", "Indore"),
        L("chips", S, "Potato chips 50g packs", AG, {"name": "potato chips"}, 20000, "pcs", 10, "INR", "pcs", "Indore")],
       include=["potato", "aloo"], order=[("potato", "sweet"), ("aloo", "sweet"), ("potato", "chips")]),
    sc("A20", "PVC 4 inch vs 110 mm", "buyer: PVC pipe 4 inch",
       L("req", B, "PVC pipe 4 inch", IND, {"name": "pvc pipe", "size": "4 inch"}, 1000, "m", 300, "INR", "m", "Rajkot"),
       [L("mm110", S, "PVC pipe 110 mm 6 kgf", IND, {"name": "pvc pipe", "size": "110 mm"}, 5000, "m", 290, "INR", "m", "Rajkot"),
        L("mm32", S, "PVC pipe 32 mm", IND, {"name": "pvc pipe", "size": "32 mm"}, 5000, "m", 60, "INR", "m", "Rajkot")],
       include=["mm110"], order=[("mm110", "mm32")]),
    sc("A21", "spec only in seller title", "buyer: SS 316L sheet",
       L("req", B, "SS 316L sheet", IND, {"name": "stainless steel sheet", "grade": "316L"}, 5, "tonne", 250, "INR", "kg", "Mumbai"),
       [L("titleonly", S, "SS316L 2mm sheet ready stock", IND, {"name": "sheet"}, 10, "tonne", 245, "INR", "kg", "Mumbai"),
        L("ss304", S, "Stainless steel sheet 304", IND, {"name": "stainless steel sheet", "grade": "304"}, 10, "tonne", 190, "INR", "kg", "Mumbai")],
       include=["titleonly"], order=[("titleonly", "ss304")]),
    sc("A22", "brand vs generic", "buyer: Amul butter",
       L("req", B, "Amul butter 500g", AG, {"name": "butter", "brand": "Amul", "pack": "500g"}, 1000, "pcs", 270, "INR", "pcs", "Mumbai"),
       [L("butter", S, "Salted butter 500 g pack", AG, {"name": "butter", "pack": "500 g"}, 5000, "pcs", 260, "INR", "pcs", "Mumbai"),
        L("ghee", S, "Amul ghee 1L", AG, {"name": "ghee", "brand": "Amul"}, 5000, "pcs", 600, "INR", "pcs", "Mumbai")],
       include=["butter"], order=[("butter", "ghee")]),
    sc("A23", "chilli spellings + Hindi", "buyer: red chilli whole teja",
       L("req", B, "Red chilli whole Teja", AG, {"name": "red chilli", "variety": "teja", "form": "whole"}, 5, "tonne", 200, "INR", "kg", "Chennai"),
       [L("mirch", S, "Lal mirch sabut teja", AG, {"name": "lal mirch", "variety": "teja", "form": "sabut"}, 10, "tonne", 195, "INR", "kg", "Chennai"),
        L("chili", S, "Dry red chili Teja S17", AG, {"name": "dry red chili", "variety": "Teja S17"}, 10, "tonne", 195, "INR", "kg", "Chennai"),
        L("powder", S, "Red chilli powder", AG, {"name": "red chilli powder", "form": "powder"}, 10, "tonne", 220, "INR", "kg", "Chennai"),
        L("green", S, "Green chilli fresh", AG, {"name": "green chilli"}, 10, "tonne", 40, "INR", "kg", "Chennai")],
       include=["mirch", "chili"], order=[("mirch", "powder"), ("chili", "powder"), ("mirch", "green"), ("chili", "green")]),
    sc("A24", "buyer asks for 'not 304'", "buyer: stainless sheet, not 304",
       L("req", B, "Stainless steel sheet not 304", IND, {"name": "stainless steel sheet", "negated_attributes": ["304"]}, 5, "tonne", 250, "INR", "kg", "Mumbai"),
       [L("s316", S, "SS 316 sheet", IND, {"name": "stainless steel sheet", "grade": "316"}, 10, "tonne", 240, "INR", "kg", "Mumbai"),
        L("s304", S, "SS 304 sheet", IND, {"name": "stainless steel sheet", "grade": "304"}, 10, "tonne", 190, "INR", "kg", "Mumbai")],
       include=["s316"], order=[("s316", "s304")]),
    sc("A25", "metric vs imperial weight", "buyer: basmati 20 MT",
       L("req", B, "Basmati rice 20 MT", AG, {"name": "basmati rice"}, 20, "MT", 90, "INR", "kg", "Delhi"),
       [L("lbs", S, "Basmati rice 44000 lbs", AG, {"name": "basmati rice"}, 44000, "lb", 88, "INR", "kg", "Delhi"),
        L("small", S, "Basmati rice 200 kg", AG, {"name": "basmati rice"}, 200, "kg", 88, "INR", "kg", "Delhi")],
       include=["lbs"], order=[("lbs", "small")]),
    sc("A26", "different tariff heading, disjoint names", "buyer: onion; turmeric listing must not match",
       L("req", B, "Need onion 10 tonne", AG, {"name": "onion"}, 10, "tonne", 20, "INR", "kg", "Nashik"),
       [L("onion_ok", S, "Red onion Nashik", AG, {"name": "onion"}, 10, "tonne", 18, "INR", "kg", "Nashik"),
        L("pyaz_ok", S, "Pyaz bulk", AG, {"name": "pyaz"}, 10, "tonne", 18, "INR", "kg", "Nashik"),
        L("turmeric", S, "Dried Turmeric Bulbs Haldi Sangli", AG, {"name": "turmeric"}, 10, "tonne", 100, "INR", "kg", "Sangli")],
       include=["onion_ok", "pyaz_ok"], exclude=["turmeric"]),
]


async def _user(db, role: UserRole, label: str) -> User:
    user = User(email=f"{TAG}-{label}-{uuid.uuid4().hex[:8]}@{TAG}.example.com", password_hash="x", role=role)
    db.add(user)
    await db.flush()
    return user


async def _make(db, spec: dict[str, Any], owners: dict[str, User]) -> RFQ:
    role = RFQRole.BUYER if spec["role"] == B else RFQRole.SELLER
    owner_key = spec.get("owner")
    if owner_key and owner_key in owners:
        owner = owners[owner_key]
    else:
        owner = await _user(db, UserRole.BUYER if role is RFQRole.BUYER else UserRole.SELLER, spec["key"][:20])
        if owner_key:
            owners[owner_key] = owner
    cat = normalize_category(spec["cat"])
    pd = await spec_catalog.normalize_for_rfq(db, dict(spec["pd"]), spec["title"], cat)
    lat, lon, state = CITIES[spec["city"]]
    now = datetime.now(UTC)
    rfq = RFQ(
        user_id=owner.id, role=role, status=RFQStatus[spec.get("status", "ACTIVE")], category=cat,
        title=spec["title"], product_details=pd,
        location_city=spec["city"], location_state=state, location_country="India",
        latitude=lat, longitude=lon,
    )
    if spec["qty"] is not None:
        rfq.quantity_value, rfq.quantity_unit = Decimal(str(spec["qty"])), spec["unit"]
    if spec["price"] is not None:
        rfq.price_amount, rfq.price_currency, rfq.price_per_unit = Decimal(str(spec["price"])), spec["cur"], spec["per"]
    rfq.deadline_at = now + timedelta(days=spec.get("deadline_days", 30))
    rfq.expires_at = now - timedelta(days=1) if spec.get("expired") else rfq.deadline_at
    rfq_indexing.refresh_index_fields(rfq)
    if VECTOR_MODE and spec["key"] != "req":
        rfq.embedding_status = EmbeddingStatus.INDEXED  # as in prod; its vector is injected below
    db.add(rfq)
    await db.flush()
    return rfq


VECTOR_MODE = True
_ORIG_VECTOR_CANDIDATES = match_service._vector_candidates
_CTX: dict[str, Any] = {"fixtures": {}, "sims": {}}
_VEC_CACHE: dict[str, list[float]] = {}


def _cosine(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


async def _vec(text: str) -> Optional[list[float]]:
    if text not in _VEC_CACHE:
        v = await embeddings.embed_one(text)
        if v is None:
            return None
        _VEC_CACHE[text] = v
    return _VEC_CACHE[text]


async def _cached_embed_one(text: str) -> Optional[list[float]]:
    """Stands in for match_service.embed_one: the harness pre-embeds each requester text, so the
    matcher's 1s vector timeout measures Qdrant, not a cold embedding model."""
    return await _vec(text)


async def _vector_with_fixtures(rfq, requester_id):
    """Real Qdrant hits plus the current scenario's eligible fixtures at their cosine similarity."""
    hits = await _ORIG_VECTOR_CANDIDATES(rfq, requester_id)
    if hits is None:
        return None  # Qdrant/embedding unavailable: the matcher itself falls back to SQL
    qv = await _vec(rfq_indexing.build_match_text(rfq))
    if qv is None:
        return hits
    now = datetime.now(UTC)
    floor = settings.RELEVANCE_FLOOR if hits else settings.RELEVANCE_FALLBACK_FLOOR
    out = dict(hits)
    for key, fx in _CTX["fixtures"].items():
        if fx.id == rfq.id or fx.role != rfq.role.counterpart or fx.user_id == requester_id:
            continue
        if fx.status != RFQStatus.ACTIVE or (fx.expires_at is not None and fx.expires_at <= now):
            continue
        fv = await _vec(rfq_indexing.build_match_text(fx))
        if fv is None:
            continue
        sim = _cosine(qv, fv)
        _CTX["sims"][key] = round(sim, 4)
        if sim >= floor:  # Qdrant would cut it at the same score_threshold
            out[fx.id] = sim
    return out


def _fmt(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.2f}"


async def run_scenario(db, s: dict[str, Any]) -> dict[str, Any]:
    t0 = time.time()
    owners: dict[str, User] = {}
    fixtures: dict[str, RFQ] = {}
    notes: list[str] = []
    req = await _make(db, s["requester"], owners)
    requester = await db.get(User, req.user_id)
    fixtures["req"] = req
    for c in s["candidates"]:
        try:
            async with db.begin_nested():
                fixtures[c["key"]] = await _make(db, c, owners)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"skipped fixture {c['key']}: {type(exc).__name__}: {str(exc)[:120]}")
    # Same path as production: listings saved while the model was busy carry _canon_pending; the background job
    # (value_canon.complete_pending) finishes them before scoring. Flush only: the harness rolls everything back.
    for _attempt in range(3):  # the shared model may be busy: the production job retries on every tick, so do we
        await value_canon.complete_pending(db, only_ids=[f.id for f in fixtures.values()], commit=False, reindex=False, timeout=90.0, limit=50)
        if not any(value_canon.PENDING_KEY in (f.product_details or {}) for f in fixtures.values()):
            break
        llm_client.reset_backoff()  # a failed call puts the model in a 20 s back-off; do not burn the retry inside it
        await asyncio.sleep(5)
    await db.refresh(req)
    _CTX["fixtures"], _CTX["sims"] = fixtures, {}
    if VECTOR_MODE:
        await _vec(rfq_indexing.build_match_text(req))  # warm the cache outside the matcher's timeout
    scored, path = await match_service._collect(db, req, requester.id, None)
    scored.sort(key=lambda p: (p[1].total, p[0].created_at), reverse=True)
    by_id = {f.id: k for k, f in fixtures.items()}
    rank: dict[str, int] = {}
    ranked: list[dict[str, Any]] = []
    for i, (rfq, score) in enumerate(scored):
        k = by_id.get(rfq.id)
        if k and k != "req":
            rank[k] = i
            ranked.append({"key": k, "rank": i + 1, "total": round(score.total, 4), "sim": _CTX["sims"].get(k),
                           "relevance": None if score.relevance is None else round(score.relevance, 4)})
    top5 = [{"title": r.title, "total": round(sc_.total, 4), "relevance": None if sc_.relevance is None else round(sc_.relevance, 4),
             "fixture": by_id.get(r.id)} for r, sc_ in scored[:5]]

    fails: list[str] = []
    for k in s.get("include", []):
        if k not in fixtures:
            fails.append(f"include {k}: fixture not created")
        elif k not in rank:
            fails.append(f"include {k}: missing from results")
    for k in s.get("exclude", []):
        if k not in fixtures:
            continue
        if k in rank:
            fails.append(f"exclude {k}: present at rank {rank[k] + 1}")
    for hi, lo in s.get("order", []):
        if hi not in rank:
            if lo in rank:
                fails.append(f"order {hi} > {lo}: {lo} present@{rank[lo] + 1} but {hi} missing")
        elif lo in rank and rank[hi] > rank[lo]:
            fails.append(f"order {hi} > {lo}: got {hi}@{rank[hi] + 1} below {lo}@{rank[lo] + 1}")
    if s.get("top_regex"):
        rx = re.compile(s["top_regex"], re.I)
        n = s.get("top_n", 5)
        bad = [f"'{r.title}'" for r, _ in scored[:n] if not rx.search(r.title or "")]
        if bad:
            fails.append(f"top{n} regex /{s['top_regex']}/: off-topic {', '.join(bad)}")

    def show(k: str) -> str:
        m = next(r for r in ranked if r["key"] == k)
        sim = f"/sim {_fmt(m['sim'])}" if VECTOR_MODE else ""
        return f"{k} {m['total']:.2f}/rel {_fmt(m['relevance'])}{sim}"

    got = ", ".join(show(r["key"]) for r in ranked) or "nothing"
    exp = []
    if s.get("include"): exp.append("include " + "+".join(s["include"]))
    if s.get("exclude"): exp.append("exclude " + "+".join(s["exclude"]))
    if s.get("order"): exp.append("order " + ", ".join(f"{a}>{b}" for a, b in s["order"]))
    if s.get("top_regex"): exp.append(f"top{s.get('top_n', 5)} ~ /{s['top_regex']}/")
    res = {
        "id": s["id"], "name": s["name"], "pass": not fails, "failed_checks": fails, "notes": notes,
        "path": path, "sims": dict(_CTX["sims"]), "seconds": round(time.time() - t0, 1), "ranked_fixtures": ranked, "top5": top5,
        "line": f"IF {s['intent']} THEN the matcher returned: [{got}] EXPECTED: {'; '.join(exp)} -> "
                f"{'PASS' if not fails else 'FAIL (' + '; '.join(fails) + ')'}",
    }
    return res


async def run(only: Optional[set[str]], json_path: Optional[str], repeat: int = 1) -> int:
    engine = create_async_engine(settings.DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    results: list[dict[str, Any]] = []
    t_all = time.time()
    if VECTOR_MODE:
        match_service._vector_candidates = _vector_with_fixtures
        match_service.embed_one = _cached_embed_one
    async with factory() as db:
        try:
            for rep_i in range(repeat):
                for s in SCENARIOS:
                    if only and s["id"] not in only:
                        continue
                    try:
                        # Savepoint per scenario, rolled back, so fixtures never leak into later scenarios.
                        sp = await db.begin_nested()
                        try:
                            r = await run_scenario(db, s)
                        finally:
                            await sp.rollback()
                    except Exception as exc:  # noqa: BLE001
                        await db.rollback()
                        r = {"id": s["id"], "name": s["name"], "pass": False, "failed_checks": [f"crashed: {type(exc).__name__}: {exc}"],
                             "notes": [], "seconds": 0, "ranked_fixtures": [], "top5": [], "line": f"{s['id']} CRASHED: {exc}"}
                    r["run"] = rep_i + 1
                    results.append(r)
                    if repeat > 1 and r["pass"]:
                        continue
                    tag = f" run {rep_i + 1}" if repeat > 1 else ""
                    rv = "vector(fixtures embedded)" if VECTOR_MODE else "sql-lexical(fixtures pending)"
                    print(f"\n[{r['id']} {r['name']}{tag}] ({r['seconds']}s, path={r.get('path')}, retrieval={rv})")
                    print("  " + r["line"])
                    for n in r["notes"]:
                        print("  note:", n)
                    print("  top5 overall: " + " | ".join(
                        f"{t['title'][:40]}{'*' if t['fixture'] else ''} {t['total']:.2f}/{_fmt(t['relevance'])}" for t in r["top5"]))
        finally:
            await db.rollback()  # never commit
            match_service._vector_candidates = _ORIG_VECTOR_CANDIDATES
            match_service.embed_one = embeddings.embed_query
    await engine.dispose()
    if repeat > 1:
        print("\nPASS RATE per scenario:")
        for s in SCENARIOS:
            rs = [r for r in results if r["id"] == s["id"]]
            if rs:
                print(f"  {s['id']:4} {sum(r['pass'] for r in rs)}/{len(rs)}  {s['name']}")
    passed = sum(r["pass"] for r in results)
    print(f"\nSUMMARY: {passed} passed, {len(results) - passed} failed of {len(results)} "
          f"in {time.time() - t_all:.0f}s (all fixtures rolled back; "
          f"retrieval={'vector(fixtures embedded)' if VECTOR_MODE else 'sql-lexical'})")
    if json_path:
        Path(json_path).write_text(json.dumps(results, indent=2, default=str))
    return 0 if passed == len(results) else 1


def main() -> None:
    global VECTOR_MODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated scenario ids, e.g. S1,S5")
    ap.add_argument("--json", help="write a machine-readable report here")
    ap.add_argument("--no-vector", action="store_true", help="do not inject fixture vector similarities")
    ap.add_argument("--repeat", type=int, default=1, help="run everything N times and report pass rates")
    a = ap.parse_args()
    VECTOR_MODE = not a.no_vector
    only = {x.strip() for x in a.only.split(",")} if a.only else None
    sys.exit(asyncio.run(run(only, a.json, max(1, a.repeat))))


if __name__ == "__main__":
    main()

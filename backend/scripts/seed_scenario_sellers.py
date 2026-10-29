"""Idempotent seed of active SELLER listings (onion near Nashik, turmeric near Erode/Sangli) through the REST API,
so indexing / canonicalisation / HSN run exactly as in production. Used by scripts/chat_scenarios.py (C1, C4).

Usage: .venv/bin/python scripts/seed_scenario_sellers.py [--base http://127.0.0.1:8011]
"""
from __future__ import annotations

import argparse
import sys

import httpx

API = "/api/v1"
EMAIL = "seed-scenario-sellers@example.com"
PASSWORD = "SeedSellers-2026!x9"

NASHIK = {"city": "Nashik", "state": "Maharashtra", "country": "India", "latitude": 19.9975, "longitude": 73.7898}
LASALGAON = {"city": "Lasalgaon", "state": "Maharashtra", "country": "India", "latitude": 20.1450, "longitude": 74.2380}
ERODE = {"city": "Erode", "state": "Tamil Nadu", "country": "India", "latitude": 11.3410, "longitude": 77.7172}
SANGLI = {"city": "Sangli", "state": "Maharashtra", "country": "India", "latitude": 16.8524, "longitude": 74.5815}

# (title, product, qty, unit, price, per_unit, location, description)
LISTINGS = [
    ("Red Onion (Pyaz) 50 kg bags, Nashik", "red onion", 200, "quintal", 1450, "quintal", NASHIK,
     "Fresh red onion (pyaz/kanda), 45-55 mm, farm sorted, packed in 50 kg mesh bags. Ready stock near Nashik."),
    ("Onion Pyaz Grade A Lasalgaon", "onion", 500, "quintal", 1380, "quintal", LASALGAON,
     "Grade A Lasalgaon onion, 50-60 mm, dry and cured. Loading from Lasalgaon mandi, Nashik district."),
    ("Nashik Red Kanda Onion Export Quality", "red onion", 30, "ton", 16, "kg", NASHIK,
     "Export quality red onion from Nashik, 55-65 mm, 25 kg and 50 kg bags, ready for container loading."),
    ("Turmeric (Haldi) Finger Erode", "turmeric", 100, "quintal", 14500, "quintal", ERODE,
     "Erode turmeric fingers (haldi), polished, curcumin 3.5 percent plus, packed in 50 kg PP bags."),
    ("Haldi Powder Turmeric Sangli", "turmeric powder", 10, "ton", 125, "kg", SANGLI,
     "Pure turmeric powder (haldi) from Sangli, 80 mesh, no added colour, 25 kg bags."),
    ("Dried Turmeric Bulbs Haldi Sangli", "turmeric", 150, "quintal", 13800, "quintal", SANGLI,
     "Sangli dried whole turmeric (haldi) bulbs and fingers, sun dried, moisture below 10 percent."),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.base, timeout=60)
    r = c.post(f"{API}/auth/signup", json={
        "email": EMAIL, "password": PASSWORD, "name": "Scenario Sellers", "role": "seller",
        "company_name": "Deccan Agro Traders", "city": "Nashik"})
    if r.status_code != 201:
        r = c.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    if r.status_code not in (200, 201):
        print(f"auth failed: HTTP {r.status_code} {r.text[:300]}")
        return 1
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    existing = set()
    off = 0
    while True:
        g = c.get(f"{API}/rfqs", headers=h, params={"limit": 100, "offset": off})
        if g.status_code != 200:
            print(f"list failed: HTTP {g.status_code} {g.text[:200]}")
            return 1
        items = g.json().get("items", [])
        existing |= {i["title"] for i in items}
        if len(items) < 100:
            break
        off += 100
    created = skipped = failed = 0
    for title, product, qty, unit, price, per, loc, desc in LISTINGS:
        if title in existing:
            print(f"skip   {title}")
            skipped += 1
            continue
        body = {
            "role": "seller", "category": "Agriculture", "title": title, "description": desc, "status": "active",
            "product_details": {"name": product}, "quantity": {"value": qty, "unit": unit},
            "price_target": {"amount": price, "currency": "INR", "per_unit": per}, "location": loc,
            "deadline": {"in_days": 90, "raw": "within 90 days"},
        }
        p = c.post(f"{API}/rfqs", json=body, headers=h)
        if p.status_code == 201:
            j = p.json()
            print(f"create {title} -> {j['id']} status={j['status']}")
            created += 1
        else:
            print(f"FAIL   {title}: HTTP {p.status_code} {p.text[:250]}")
            failed += 1
    print(f"done: created={created} skipped={skipped} failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

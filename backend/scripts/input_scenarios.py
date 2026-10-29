"""Live REST probes for listing input hygiene, moderation and catalog search.

    cd backend && .venv/bin/python scripts/input_scenarios.py --base http://127.0.0.1:8023 [--only categories,units]

Signs up a throw-away user, posts RFQs and checks the response of each bad / good input. Every RFQ the
script creates is closed again at the end. Prints PASS/FAIL per check and a SUMMARY line; exit 1 on any FAIL.
Needs a running backend and (for moderation / category classification of unfamiliar words) the AI model.
"""

import argparse
import random
import sys
import time

import httpx

ARGS: argparse.Namespace
RESULTS: list[tuple[str, bool, str]] = []
CREATED: list[str] = []
C: httpx.Client


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def tag() -> str:
    return f"{random.randint(100000, 999999)}"


def post_rfq(**over) -> httpx.Response:
    body = {
        "role": "buyer",
        "category": "Agriculture",
        "title": f"Basmati rice 1121 sella {tag()} kg lot",
        "quantity": {"value": 100, "unit": "kg"},
        "status": "active",
    }
    body.update(over)
    r = C.post("/api/v1/rfqs", json=body, timeout=120)
    if r.status_code == 201:
        CREATED.append(r.json()["id"])
    return r


def err_text(r: httpx.Response) -> str:
    try:
        return str(r.json())[:160]
    except ValueError:
        return r.text[:160]


def categories() -> None:
    r = post_rfq(category="Weapons", title=f"Industrial widget {tag()}")  # blocked by moderation or classified from the title
    check("category 'Weapons' not stored", r.status_code != 201 or r.json()["category"] not in ("Weapons",), f"{r.status_code} {err_text(r)}")
    r = post_rfq(category="asdf", title=f"asdf qwer {tag()}")
    check("category 'asdf' with a gibberish title -> 422 listing allowed values", r.status_code == 422 and "use one of" in err_text(r), f"{r.status_code} {err_text(r)}")
    r = post_rfq(category="electronic", title=f"USB cable {tag()} 2m")
    check("category 'electronic' -> Electronics", r.status_code == 201 and r.json()["category"] == "Electronics", f"{r.status_code}")
    r = post_rfq(category="gibberishzz", title=f"Turmeric finger {tag()} kg")
    check("unknown category + real product title is classified or refused", r.status_code in (201, 422) and (r.status_code != 201 or r.json()["category"] in ("Agriculture", "Food & Beverages", "General Goods")), f"{r.status_code} {err_text(r)}")


def units() -> None:
    r = post_rfq(quantity={"value": 5, "unit": "lightyear"})
    check("quantity unit 'lightyear' -> 422", r.status_code == 422, f"{r.status_code} {err_text(r)}")
    r = post_rfq(price_target={"amount": 20, "currency": "INR", "per_unit": "banana"})
    check("price per_unit 'banana' -> 422", r.status_code == 422, f"{r.status_code} {err_text(r)}")
    for raw, want in (("kilo", "kg"), ("tons", "tonne"), ("MT", "tonne"), ("Pieces", "pcs"), ("ltr", "l")):
        r = post_rfq(quantity={"value": 5, "unit": raw})
        got = r.json()["quantity"]["unit"] if r.status_code == 201 else err_text(r)
        check(f"unit alias {raw!r} -> {want}", r.status_code == 201 and got == want, f"{r.status_code} {got}")
    for raw in ("box", "bag", "carton", "container", "set", "pair", "roll", "bottle", "dozen"):
        r = post_rfq(quantity={"value": 5, "unit": raw})
        check(f"packaging unit {raw!r} accepted", r.status_code == 201, f"{r.status_code} {err_text(r) if r.status_code != 201 else ''}")
    r = post_rfq(price_target={"amount": 20, "currency": "INR", "per_unit": "per kg"})
    check("price per_unit 'per kg' -> kg", r.status_code == 201 and r.json()["price_target"]["per_unit"] == "kg", f"{r.status_code}")


def hygiene() -> None:
    r = post_rfq(location={"city": "   ", "country": "India"})
    check("location city '   ' -> null", r.status_code == 201 and r.json()["location"]["city"] is None, f"{r.status_code}")
    r = post_rfq(title="a")
    check("title 'a' -> 422", r.status_code == 422, f"{r.status_code} {err_text(r)}")
    r = post_rfq(price_target={"amount": 0, "currency": "INR", "per_unit": "kg"})
    check("price amount 0 -> 422", r.status_code == 422, f"{r.status_code} {err_text(r)}")
    r = post_rfq(title=f"<img src=x onerror=alert(1)> onion {tag()}", description="<script>alert(1)</script>fresh <b>red</b> onions",
                 product_details={"note": "<i>grade A</i>"})
    ok = r.status_code == 201 and "<" not in r.json()["title"] and "<" not in (r.json().get("description") or "") and "<" not in str(r.json()["product_details"])
    check("HTML stripped from title/description/product_details", ok, f"{r.status_code} {r.json().get('title') if r.status_code == 201 else err_text(r)}")


def moderation() -> None:
    for title in ("Ganja 5 kg", "Cannabis buds 2 kg", "10 pistols with bullets", "Elephant ivory tusks", "Cocaine 1 kg pure", "Human kidney for sale"):
        r = post_rfq(title=f"{title} {tag()}")
        check(f"prohibited blocked: {title!r}", r.status_code == 422, f"{r.status_code} {err_text(r)[:90]}")
    for title in ("Glue gun sticks 11mm", "Gun metal grey paint 20 litre", "Industrial hemp fibre bales", "Kitchen knife stainless steel"):
        r = post_rfq(title=f"{title} {tag()}", category="Industrial")
        check(f"legal not blocked: {title!r}", r.status_code == 201, f"{r.status_code} {err_text(r)[:90]}")


def catalog() -> None:
    def search(q: str) -> tuple[list[str], float]:
        t = time.monotonic()
        r = C.get("/api/v1/marketplace/catalog", params={"q": q, "limit": 50}, timeout=120)
        return [i["title"] for i in r.json().get("items", [])] if r.status_code == 200 else ["HTTP %s" % r.status_code], time.monotonic() - t

    for q, must_not in (("haldi", "half-cell"), ("moongfali", "moong"), ("sabut moongfali", "moong")):
        titles, dt = search(q)
        bad = [t for t in titles if must_not in t.lower()]
        check(f"search {q!r} has no unrelated {must_not!r} listings", not bad, f"{len(titles)} results, {dt:.1f}s")
    search("onion")  # warm the vocabulary / lexicon
    for q in ("onion", "pyaz", "xyzzyqq"):
        search(q)
        titles, dt = search(q)
        check(f"repeat search {q!r} < 1.5 s", dt < 1.5 and not any(t.startswith("HTTP") for t in titles), f"{len(titles)} results, {dt:.2f}s")
    titles, _ = search("onoin")
    check("typo 'onoin' is not matched to unrelated listings", all("onion" in t.lower() for t in titles), f"{len(titles)} results")


GROUPS = {"categories": categories, "units": units, "hygiene": hygiene, "moderation": moderation, "catalog": catalog}


def main() -> int:
    global ARGS, C
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8023")
    p.add_argument("--only", default="")
    ARGS = p.parse_args()
    C = httpx.Client(base_url=ARGS.base)
    email = f"scen{tag()}{tag()}@example.com"
    r = C.post("/api/v1/auth/signup", json={"email": email, "password": "Passw0rd!xyz9", "name": "Scenario User", "role": "buyer"})
    if r.status_code != 201:
        print("signup failed", r.status_code, r.text[:200])
        return 2
    C.headers["Authorization"] = "Bearer " + r.json()["access_token"]
    chosen = [g for g in GROUPS if not ARGS.only or g in ARGS.only.split(",")]
    for g in chosen:
        print(f"--- {g}")
        GROUPS[g]()
    for rid in CREATED:
        C.post(f"/api/v1/rfqs/{rid}/close", timeout=60)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"SUMMARY {len(RESULTS) - len(failed)}/{len(RESULTS)} passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

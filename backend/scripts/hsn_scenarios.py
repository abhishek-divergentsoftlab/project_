"""Live HSN scenario script: how real users type product names (English, Hinglish, Devanagari, typos, trade
shorthand, codes, services) against a RUNNING backend.

    .venv/bin/python scripts/hsn_scenarios.py --base http://127.0.0.1:8021 [--min-pass 0.95] [--only onion]

For every case it calls GET /hsn/suggest first (the way the UI does while a user types; this also teaches the
learned cache), then GET /hsn/search. A case passes when BOTH answers start with an accepted code prefix.
Also checks: no 5xx anywhere, no group (5/7-digit) rows returned, junk input returns nothing.
Exit code 0 only when the pass rate >= --min-pass, there are no 5xx and no junk answers."""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CASES = [
    # (query, acceptable prefixes)  -- plain English / Hinglish / Devanagari / typo
    ("onion", ["0703"]), ("pyaz", ["0703"]), ("kanda", ["0703"]), ("प्याज", ["0703"]), ("onoin", ["0703"]),
    ("onions", ["0703"]), ("ONION", ["0703"]), ("onion powder", ["0712"]), ("onion seeds", ["1209"]),
    ("dehydrated onion flakes", ["0712"]), ("lahsun", ["070320"]), ("garlic", ["070320"]),
    ("aloo", ["0701"]), ("potato", ["0701"]), ("potatoes", ["0701"]), ("tamatar", ["0702"]), ("tomatoes", ["0702"]),
    ("basmati rice", ["1006"]), ("chawal", ["1006"]), ("1121 sella rice", ["1006"]), ("wheat", ["1001"]),
    ("gehun", ["1001"]), ("atta", ["1101"]), ("wheat flour", ["1101"]), ("maida", ["1101"]), ("sugar", ["1701"]),
    ("cheeni", ["1701"]), ("jaggery", ["1701"]), ("gur", ["1701"]), ("haldi", ["0910"]), ("turmeric", ["0910"]),
    ("jeera", ["0909"]), ("cumin seeds", ["0909"]), ("lal mirch", ["0904"]), ("red chilli powder", ["0904"]),
    ("kali mirch", ["0904"]), ("elaichi", ["0908"]), ("cardamom", ["0908"]), ("kaju", ["0801"]),
    ("cashew W320", ["0801"]), ("moongfali", ["1202"]), ("peanut", ["1202"]), ("groundnut", ["1202"]),
    ("sarson ka tel", ["1514"]), ("mustard oil", ["1514"]), ("ghee", ["0405"]), ("paneer", ["0406"]),
    ("milk", ["0401"]), ("honey", ["0409"]), ("chai patti", ["0902"]), ("tea leaves", ["0902"]),
    ("coffee beans", ["0901"]), ("chana", ["0713"]), ("toor dal", ["0713"]), ("moong dal", ["0713"]),
    ("maize", ["1005"]), ("makka", ["1005"]), ("corn", ["1005"]), ("soybean", ["1201"]),
    ("dhaniya", ["0909", "0709"]), ("coriander seeds", ["0909"]), ("bhindi", ["0709"]), ("okra", ["0709"]),
    ("brinjal", ["0709"]), ("baingan", ["0709"]), ("capsicum", ["0709"]), ("shimla mirch", ["0709"]),
    ("banana", ["0803"]), ("kela", ["0803"]), ("mango", ["0804"]), ("aam", ["0804"]), ("alphonso mango", ["0804"]),
    # textiles
    ("cotton yarn 30s", ["5205"]), ("raw cotton", ["5201"]), ("cotton t-shirt", ["6109"]), ("tshirt", ["6109"]),
    ("t shirts", ["6109"]), ("jeans", ["6203", "6204"]), ("denim fabric", ["5209", "5211"]),
    ("polyester yarn", ["5402"]), ("saree", ["5007", "5208", "5407", "5208", "6211"]),
    # electronics
    ("mobile phone", ["8517"]), ("smartphone", ["8517"]), ("iphone", ["8517"]), ("laptop", ["8471"]),
    ("usb cable", ["8544"]), ("type c cable", ["8544"]), ("led bulb", ["8539"]), ("solar panel", ["8541"]),
    ("mobile charger", ["8504"]), ("ceiling fan", ["8414"]), ("cctv camera", ["8525"]),
    # industrial / metals / chemicals
    ("ss 304 sheet", ["7219", "7220"]), ("ss304 sheet", ["7219", "7220"]), ("stainless steel pipe", ["7304", "7306"]),
    ("ms pipe", ["7306", "7304"]), ("tmt bar", ["7214"]), ("saria", ["7214"]), ("cement", ["2523"]),
    ("opc 53 grade cement", ["2523"]), ("copper wire", ["7408"]), ("aluminium sheet", ["7606"]),
    ("aluminum sheet", ["7606"]), ("pvc pipe", ["3917"]), ("hdpe granules", ["3901"]), ("caustic soda", ["2815"]),
    ("NaOH", ["2815"]), ("sodium hydroxide", ["2815"]), ("urea", ["3102"]), ("dap fertilizer", ["3105"]),
    ("paracetamol tablets", ["3004"]), ("a4 paper", ["4802"]), ("hair dye", ["3305"]), ("shampoo", ["3305"]),
    ("soap", ["3401"]), ("wooden chair", ["9401"]), ("car tyre", ["4011"]), ("motorcycle", ["8711"]),
    ("gold jewellery", ["7113"]), ("cococola", ["2202"]), ("soft drink", ["2202"]), ("bolt m12", ["7318"]),
    ("fibc bags", ["6305"]), ("jumbo bag", ["6305"]), ("pp woven bags", ["6305", "3923"]),
    # services (SAC)
    ("transport services", ["9965"]), ("freight", ["9965"]), ("consulting services", ["9983"]),
    # codes the way users type them
    ("0703", ["0703"]), ("0703.10", ["070310"]), ("0703 10", ["070310"]), ("07031010", ["0703"]),
]
JUNK = ["", " ", "%", "_", "'", "\"", "a", "%%%%", "' OR 1=1 --", "<script>alert(1)</script>", "99999999",
        "x" * 100, "x" * 101, "🧅", "😀😀", "asdfghjkl", "\\", "null", "../../etc/passwd", "1" * 30]




def make_get(base):
    def get(path):
        t = time.time()
        try:
            with urllib.request.urlopen(base + "/api/v1/hsn" + path, timeout=120) as r:
                return r.status, json.loads(r.read() or b"null"), time.time() - t
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:150], time.time() - t
        except Exception as e:  # noqa: BLE001
            return -1, str(e)[:150], time.time() - t
    return get


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    ap.add_argument("--min-pass", type=float, default=0.95)
    ap.add_argument("--only", default="", help="run only cases whose query contains this text")
    ap.add_argument("--junk-only", action="store_true", help="skip the product cases, run the junk inputs only")
    args = ap.parse_args()
    get = make_get(args.base.rstrip("/"))
    cases = [] if args.junk_only else [(q, e) for q, e in CASES if args.only in q]
    # Warm the shared model first: a cold load (20-45 s) would otherwise time out the first case and put the model
    # in a back-off that turns every following answer into a low-confidence text match.
    for attempt in range(4):
        st, sug, dt = get("/suggest?" + urllib.parse.urlencode({"text": "warm up probe stainless steel spoon"}))
        print(f"warm-up {attempt}: status={st} source={(sug or {}).get('source') if isinstance(sug, dict) else None} ({dt:.0f}s)", flush=True)
        if isinstance(sug, dict) and sug.get("source") in ("ai", "cache"):
            break
        time.sleep(25)
    fails, bad5xx, group_rows, junk_bad = 0, 0, 0, 0
    for q, exp in cases:
        st2, sug, dt2 = get("/suggest?" + urllib.parse.urlencode({"text": q})) if len(q) >= 2 else (0, None, 0)
        st, res, dt = get("/search?" + urllib.parse.urlencode({"q": q, "limit": 5}))
        bad5xx += sum(1 for x in (st, st2) if x >= 500 or x == -1)
        top = res[0]["code"] if st == 200 and res else None
        top3 = [r["code"] for r in res[:3]] if st == 200 and res else []
        group_rows += sum(1 for r in (res if st == 200 and res else []) if len(r["code"]) in (5, 7))
        ok1 = bool(top) and any(top.startswith(e) for e in exp)
        scode = sug.get("code") if isinstance(sug, dict) else None
        ok2 = bool(scode) and any(scode.startswith(e) for e in exp)
        if not (ok1 and ok2):
            fails += 1
        conf = sug.get("confidence") if isinstance(sug, dict) else None
        title = (sug.get("formatted_title") or "")[:50] if isinstance(sug, dict) else ""
        print(f"{'PASS' if ok1 and ok2 else 'FAIL'} q={q!r:28} want={exp} search_top={top} top3={top3} ({dt:.1f}s)"
              f" | suggest={scode} conf={conf} title={title} ({dt2:.1f}s)", flush=True)
    n = len(cases)
    print(f"\nHSN product cases: {n - fails}/{n} pass")
    if args.only and not args.junk_only:
        return 0 if not fails else 1
    for q in JUNK:
        st, res, _ = get("/search?" + urllib.parse.urlencode({"q": q}))
        st2, sug, _ = get("/suggest?" + urllib.parse.urlencode({"text": q}))
        st3, _code, _ = get("/code/" + urllib.parse.quote(q, safe="") if q.strip() else "/code/%20")
        bad5xx += sum(1 for x in (st, st2, st3) if x >= 500 or x == -1)
        found = (isinstance(res, list) and len(res) > 0) or (isinstance(sug, dict) and sug.get("code")) or st3 == 200
        junk_bad += 1 if found else 0
        print(f"JUNK q={q[:30]!r:34} search={st}/{len(res) if isinstance(res, list) else '-'} "
              f"suggest={st2}/{sug.get('code') if isinstance(sug, dict) else None} code={st3} {'BAD' if found else 'ok'}",
              flush=True)
    rate = (n - fails) / max(n, 1)
    print(f"SUMMARY pass={n - fails}/{n} ({rate:.0%}) 5xx={bad5xx} group_rows={group_rows} junk_answered={junk_bad}")
    return 0 if rate >= args.min_pass and not bad5xx and not group_rows and not junk_bad else 1


if __name__ == "__main__":
    sys.exit(main())

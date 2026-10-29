"""Live end-to-end chat scenarios against a running backend (mirrors the frontend's streaming protocol).

Usage: .venv/bin/python scripts/chat_scenarios.py [--only C1,C4] [--json PATH] [--base http://127.0.0.1:8011]
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import statistics
import sys
import time
import traceback

import httpx

API = "/api/v1"
TURN_TIMEOUT = 240.0
FORM_RE = re.compile(
    r"\b(fill (in|out)?( the)? form|use the form|open the form|click (on )?(the )?\w+ button)\b", re.I
)
TRACE_RE = re.compile(r"Traceback \(most recent call last\)|\bFile \"[^\"]+\", line \d+|Exception:|\bHTTP 5\d\d\b")
ONION_RE = re.compile(r"onion|pyaz|kanda|vengayam|ulli|dungri", re.I)
INVENTED_RE = re.compile(r"typical market|industry average|usually costs?", re.I)

ALL_TURNS: list[float] = []
CHECKS: list[dict] = []
CREATED_BY_US: list[str] = []
DEBUG_PRINTED = False


def out(s: str) -> None:
    print(s, flush=True)


class Conv:
    def __init__(self, client: httpx.Client, headers: dict, name: str):
        self.c, self.h, self.name = client, headers, name
        self.messages: list[dict] = []
        self.draft: dict | None = None
        self.conv_id: str | None = None
        self.candidates: list[dict] | None = None
        self.created: dict | None = None
        self.quick: list[str] = []
        self.checks: list[tuple[bool, str]] = []
        self.last_reply = ""
        self.last_chunks: list[dict] = []

    # -- checks
    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks.append((bool(ok), label))
        CHECKS.append({"conv": self.name, "label": label, "ok": bool(ok), "detail": detail})
        out(f"    {'PASS' if ok else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))
        return bool(ok)

    # -- one turn, as the UI does it
    def say(self, text: str, retry: bool = True) -> str:
        global DEBUG_PRINTED
        self.messages.append({"role": "user", "content": text})
        body = {
            "messages": self.messages,
            "current_rfq": self.draft,
            "conversation_id": self.conv_id,
            "matched_candidates": self.candidates or None,
            "active_connection_id": None,
            "agent_id": None,
        }
        t0 = time.time()
        content, chunks, err = "", [], None
        try:
            with self.c.stream(
                "POST", f"{API}/ai-chat/stream", json=body, headers=self.h,
                timeout=httpx.Timeout(TURN_TIMEOUT, connect=15.0),
            ) as r:
                if r.status_code != 200:
                    err = f"HTTP {r.status_code}: {r.read().decode()[:300]}"
                else:
                    for line in r.iter_lines():
                        line = line.strip()
                        if not line.startswith("data: "):
                            continue
                        try:
                            ch = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        chunks.append(ch)
                        if ch.get("conversation_id") and not self.conv_id:
                            self.conv_id = ch["conversation_id"]
                        if ch.get("rfq_draft") and (
                            len(ch["rfq_draft"]) > 0 or (ch.get("done") and ch.get("created_rfq"))
                        ):
                            self.draft = ch["rfq_draft"]
                        if ch.get("matched_candidates"):
                            self.candidates = ch["matched_candidates"]
                        if ch.get("created_rfq"):
                            self.created = ch["created_rfq"]
                        if ch.get("quick_replies"):
                            self.quick = ch["quick_replies"]
                        if ch.get("replace_content"):
                            content = ch.get("content") or ""
                        else:
                            content += ch.get("content") or ""
                        if ch.get("done"):
                            break
        except Exception as e:  # timeout etc.
            err = f"{type(e).__name__}: {e}"
        secs = time.time() - t0
        if err and retry and "HTTP 4" not in err:
            out(f"    (turn error: {err[:150]}; retrying once)")
            self.messages.pop()
            return self.say(text, retry=False)
        ALL_TURNS.append(secs)
        self.last_chunks = chunks
        if err:
            content = content or f"[TRANSPORT ERROR] {err}"
        # the UI keeps the assembled content in history
        self.messages.append({"role": "assistant", "content": content or "(empty)"})
        self.last_reply = content
        d = self.draft or {}
        if not DEBUG_PRINTED:
            DEBUG_PRINTED = True
            out(f"    [debug] raw draft keys: {sorted(d.keys())}")
            out(f"    [debug] final chunk keys: {sorted((chunks[-1] if chunks else {}).keys())}")
        out(
            f'IF the user says "{text}" THEN the chat replied ({secs:.1f}s): '
            f'"{content[:160].replace(chr(10), " ")}" | draft: {self.fmt()} | created={(self.created or {}).get("id", "-")}'
        )
        # global checks
        self.check(bool(content.strip()) and not err, "non-empty reply", err or "")
        self.check(not TRACE_RE.search(content), "no traceback/error text")
        self.check(not FORM_RE.search(content), "no form hand-off")
        q = content.count("?")
        self.check(q <= 2, f"<=2 questions (found {q})")
        return content

    def fmt(self) -> str:
        d = self.draft or {}
        pd = d.get("product_details") or {}
        qty, pt, loc = d.get("quantity") or {}, d.get("price_target") or {}, d.get("location") or {}
        return (
            f"role={d.get('role')} product={pd.get('name')} qty={qty.get('value')} unit={qty.get('unit')} "
            f"price={pt.get('amount')} cur={pt.get('currency')}/{pt.get('per_unit')} "
            f"place={loc.get('city')}" + (f"({loc.get('state')},{loc.get('latitude')},{loc.get('longitude')})" if loc else "")
        )

    # -- draft accessors
    def product(self) -> str:
        d = self.draft or {}
        return str((d.get("product_details") or {}).get("name") or "") + " " + str(d.get("title") or "")

    def specs_text(self) -> str:
        return json.dumps((self.draft or {}).get("product_details") or {}, ensure_ascii=False).lower()

    def num(self, section: str, key: str):
        v = ((self.draft or {}).get(section) or {}).get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def loc(self) -> str:
        return json.dumps((self.draft or {}).get("location") or {}, ensure_ascii=False).lower()


def unit_is(u, *opts) -> bool:
    return str(u or "").strip().lower().rstrip("s") in {o.rstrip("s") for o in opts}


def check_draft(cv: Conv, role=None, product_re=None, qty=None, units=None, price=None, cur=None, per=None, place=None):
    d = cv.draft or {}
    if role:
        cv.check(d.get("role") == role, f"draft role={role}", f"got {d.get('role')}")
    if product_re:
        cv.check(bool(re.search(product_re, cv.product(), re.I)), f"product ~ /{product_re}/", f"got '{cv.product().strip()}'")
    if qty is not None:
        cv.check(cv.num("quantity", "value") == qty, f"qty={qty}", f"got {cv.num('quantity', 'value')}")
    if units:
        u = (d.get("quantity") or {}).get("unit")
        cv.check(unit_is(u, *units), f"unit in {units}", f"got {u}")
    if price is not None:
        cv.check(cv.num("price_target", "amount") == price, f"price={price}", f"got {cv.num('price_target', 'amount')}")
    pt = d.get("price_target") or {}
    if cur:
        cv.check(str(pt.get("currency") or "").upper() == cur, f"currency={cur}", f"got {pt.get('currency')}")
    if per:
        cv.check(unit_is(pt.get("per_unit"), *per), f"per_unit in {per}", f"got {pt.get('per_unit')}")
    if place:
        cv.check(place.lower() in cv.loc(), f"place has {place}", f"got {cv.loc()}")


# ---------------------------------------------------------------- scenarios
def c1(cv: Conv, client, headers):
    cv.say("mujhe 50 quintal pyaz chahiye nashik delivery")
    check_draft(cv, role="buyer", product_re=r"onion|pyaz", qty=50, units=["quintal"], place="Nashik")
    cv.say("1500 rupaye per quintal tak")
    check_draft(cv, price=1500, cur="INR", per=["quintal"])
    cv.say("quantity 80 quintal kar do")
    check_draft(cv, qty=80, price=1500)
    answers_given = 0
    for i in range(8 - 3):
        if cv.created:
            break
        low = cv.last_reply.lower()
        if re.search(r"confirm|publish|post it|go ahead|shall i|recap|summary", low) and "?" in low:
            msg = "haan, publish kar do"
        elif re.search(r"deadline|when|kab|by when|delivery date|how soon|timeline", low):
            msg = "15 din mein"
        elif re.search(r"quality|grade|variety|size|type|specification|spec", low):
            msg = "red onion, medium size"
        else:
            msg = ["15 din mein", "red onion, medium size", "haan, publish kar do", "koi aur requirement nahi hai"][answers_given % 4]
        answers_given += 1
        cv.say(msg)
    cv.check(bool(cv.created), "created_rfq appeared within 8 turns")
    if not cv.created:
        return
    rid = cv.created["id"]
    CREATED_BY_US.append(rid)
    r = client.get(f"{API}/rfqs/{rid}", headers=headers, timeout=30)
    cv.check(r.status_code == 200, "GET created RFQ", f"HTTP {r.status_code}")
    if r.status_code == 200:
        j = r.json()
        loc, q, p = j.get("location") or {}, j.get("quantity") or {}, j.get("price_target") or {}
        out(f"    CREATED RFQ: role={j['role']} status={j['status']} title={j['title']!r} category={j['category']} "
            f"qty={q} price={p} loc={loc} deadline={j.get('deadline')}")
        cv.check(j["role"] == "buyer", "RFQ role buyer", j["role"])
        cv.check(bool(ONION_RE.search(j["title"] + " " + json.dumps(j.get("product_details") or {}))), "RFQ title/product mentions onion")
        cv.check(float(q.get("value") or 0) == 80 and unit_is(q.get("unit"), "quintal"), "RFQ qty 80 quintal", str(q))
        cv.check(float(p.get("amount") or 0) == 1500 and str(p.get("currency")).upper() == "INR" and unit_is(p.get("per_unit"), "quintal"),
                 "RFQ price 1500 INR per quintal", str(p))
        cv.check("nashik" in str(loc.get("city") or "").lower(), "RFQ city Nashik", str(loc.get("city")))
        cv.check(loc.get("latitude") is not None and loc.get("longitude") is not None, "RFQ lat/lon not null",
                 f"{loc.get('latitude')},{loc.get('longitude')}")
    cv.candidates = None
    cv.say("is RFQ ke liye matching sellers dikhao")
    cands = cv.candidates or []
    out(f"    candidates ({len(cands)}):")
    bad = []
    for cd in cands:
        t = cd.get("title") or ""
        sc = (cd.get("score") or {}).get("total")
        out(f"      - {t!r} score={sc} cat={cd.get('category')}")
        if not ONION_RE.search(t):
            bad.append(t)
    cv.check(len(cands) >= 1, "at least 1 candidate", f"{len(cands)}")
    cv.check(not bad, "every candidate onion-related", f"off-commodity: {bad}" if bad else "")


def c2(cv, *_):
    cv.say("selling vengayam 10 ton chennai, 18 rs per kg")
    check_draft(cv, role="seller", product_re=r"onion|vengayam", qty=10, units=["ton", "tonne", "mt"], price=18,
                cur="INR", per=["kg", "kilogram"], place="Chennai")


def c3(cv, *_):
    cv.say("need 2 container jeera, delivery at unjha")
    check_draft(cv, product_re=r"cumin|jeera", place="Unjha")
    out(f"    location: {(cv.draft or {}).get('location')}")


def _titles_check(cv, name_re, commodity):
    cands = cv.candidates or []
    titles = [c.get("title") or "" for c in cands]
    for c in cands:
        out(f"      - {c.get('title')!r} score={(c.get('score') or {}).get('total')}")
    bad = [t for t in titles if not re.search(name_re, t, re.I)]
    reply_ok = bool(re.search(name_re, cv.last_reply, re.I)) or not cv.last_reply
    cv.check(bool(titles) or reply_ok, f"reply or candidates reference {commodity}", f"{len(titles)} candidates")
    cv.check(not bad, f"no off-commodity listings as {commodity}", f"off: {bad}" if bad else "")


def c4(cv, *_):
    cv.say("show me sellers of haldi")
    _titles_check(cv, r"turmeric|haldi", "turmeric/haldi")


def c5(cv, *_):
    r = cv.say("what is the current price of basmati rice on the platform?")
    cv.check(not INVENTED_RE.search(r), "no invented market-data phrases", (INVENTED_RE.search(r) or [""])[0] if INVENTED_RE.search(r) else "")
    has_num = bool(re.search(r"\d", r)) and bool(re.search(r"₹|rs\.?|inr|usd|\$|/\s?(kg|quintal|ton|mt)|per (kg|quintal|ton)", r, re.I))
    no_data = bool(re.search(r"no (listing|data|price|active|current)|don't have|do not have|couldn't find|could not find|not (enough|available)|none", r, re.I))
    cv.check(has_num or no_data, "gives numbers with currency/unit or says no data")


def c6(cv, *_):
    cv.say("We manufacture SS 304 sheets 2mm, 5 MT ready stock in Ahmedabad at 190 rs/kg")
    check_draft(cv, role="seller", product_re=r"stainless|ss\s*304", qty=5, units=["mt", "ton", "tonne"], price=190,
                cur="INR", per=["kg", "kilogram"], place="Ahmedabad")
    cv.check("304" in cv.specs_text() or "304" in cv.product(), "grade 304 in draft specs/attributes", cv.specs_text()[:200])
    cv.check(bool(re.search(r"stainless|sheet", cv.product(), re.I)), "product mentions stainless/sheet")


def c7(cv, *_):
    r = cv.say("write me a poem about love")
    lines = [l for l in r.splitlines() if l.strip()]
    short = [l for l in lines if len(l.strip()) <= 60]
    cv.check(not (len(short) >= 4 and len(short) >= 0.6 * len(lines)), "no poem (verse-like lines)", f"{len(short)} short lines of {len(lines)}")
    cv.check(bool(re.search(r"trade|b2b|rfq|buy|sell|product|business|marketplace|listing|sourc", r, re.I)), "steers back to trade")


def c8(cv, *_):
    cv.say("मुझे 10 टन टमाटर चाहिए इंदौर में")
    check_draft(cv, role="buyer", product_re=r"tomato|टमाटर|tamatar", qty=10, units=["ton", "tonne", "mt"], place="Indore")


def _api_rfq(client, headers, title: str, status: str, product: str = "basmati rice") -> dict | None:
    """A listing created through the REST API (not the chat), so the scenario starts from a known saved/live RFQ."""
    body = {
        "role": "buyer", "category": "Agriculture", "title": title, "status": status,
        "product_details": {"name": product}, "quantity": {"value": 500, "unit": "kg"},
        "price_target": {"amount": 50, "currency": "INR", "per_unit": "kg"},
        "location": {"city": "Indore", "state": "Madhya Pradesh", "country": "India", "latitude": 22.7196, "longitude": 75.8577},
        "deadline": {"in_days": 30, "raw": "within 30 days"},
    }
    r = client.post(f"{API}/rfqs", json=body, headers=headers, timeout=60)
    if r.status_code != 201:
        out(f"    (could not create the fixture RFQ: HTTP {r.status_code} {r.text[:200]})")
        return None
    CREATED_BY_US.append(r.json()["id"])
    return r.json()


def _rfq_status(client, headers, rid: str) -> str:
    r = client.get(f"{API}/rfqs/{rid}", headers=headers, timeout=30)
    return str(r.json().get("status")) if r.status_code == 200 else f"HTTP {r.status_code}"


def c9(cv, client, headers):
    """AI-2: "cancel this request" while drafting discards the DRAFT; closing a live listing is two steps."""
    live = _api_rfq(client, headers, "Basmati Rice 1121 Sella WP4A", "active")
    if not live:
        cv.check(False, "fixture listing created")
        return
    short = live["id"][:8]
    cv.say("I want to buy 1,00,000 kg basmati rice delivered to Pune")
    cv.say("cancel this request")
    cv.check("discard" in cv.last_reply.lower(), "cancel while drafting says the draft was discarded")
    st = client.get(f"{API}/ai-chat/conversations/{cv.conv_id}", headers=headers, timeout=30)
    cv.check(st.status_code == 200 and not (st.json().get("state") or {}).get("rfq_draft"), "the chat draft is gone (server state)")
    cv.check(_rfq_status(client, headers, live["id"]) == "active", "the live listing is still active")
    cv.say("cancel this request")  # no draft now: this names the listing and asks, it does not close
    cv.check("Basmati Rice 1121 Sella WP4A" in cv.last_reply and short in cv.last_reply, "close names the exact listing (title + short id)")
    cv.check(any("close" in q.lower() for q in cv.quick), "close confirmation comes as quick-reply chips", str(cv.quick))
    cv.check(_rfq_status(client, headers, live["id"]) == "active", "the listing is still active before the confirmation")
    cv.say("Yes, close it")
    cv.check(_rfq_status(client, headers, live["id"]) == "closed", "only the confirming reply closed it")


def c10(cv, client, headers):
    """AI-3: a bare "ok" does not publish a saved draft; publishing shows a recap and needs the confirm chip."""
    title = "Rice 500 kg WP4A draft"
    saved = _api_rfq(client, headers, title, "draft")
    if not saved:
        cv.check(False, "fixture draft created")
        return
    cv.say("ok")
    cv.check(_rfq_status(client, headers, saved["id"]) == "draft", "a bare ok did not publish the saved draft")
    cv.say("publish my rice rfq")
    cv.check(title in cv.last_reply and saved["id"][:8] in cv.last_reply, "publish shows a recap with the title and short id")
    cv.check(any("publish" in q.lower() for q in cv.quick), "publish confirmation comes as quick-reply chips", str(cv.quick))
    cv.check(_rfq_status(client, headers, saved["id"]) == "draft", "still a draft before the confirmation")
    cv.say("ok")  # confirms the pending publish the server stored on the previous turn
    cv.check(_rfq_status(client, headers, saved["id"]) == "active", "ok after the recap publishes exactly that draft")


SCENARIOS = {"C1": c1, "C2": c2, "C3": c3, "C4": c4, "C5": c5, "C6": c6, "C7": c7, "C8": c8, "C9": c9, "C10": c10}


# ---------------------------------------------------------------- main
def cleanup(client, headers):
    # every RFQ owned by the test user (created by chat, whatever the scenario)
    ids = set(CREATED_BY_US)
    try:
        r = client.get(f"{API}/rfqs", headers=headers, params={"limit": 100}, timeout=30)
        if r.status_code == 200:
            ids |= {i["id"] for i in r.json().get("items", [])}
    except Exception as e:
        out(f"cleanup list failed: {e}")
    for rid in sorted(ids):
        try:
            r = client.post(f"{API}/rfqs/{rid}/close", headers=headers, timeout=30)
            out(f"cleanup: closed RFQ {rid} -> HTTP {r.status_code}")
        except Exception as e:
            out(f"cleanup: RFQ {rid} failed: {e}")
    if not ids:
        out("cleanup: no RFQs to clean")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    a = ap.parse_args()
    only = [x.strip().upper() for x in a.only.split(",") if x.strip()] or list(SCENARIOS)
    client = httpx.Client(base_url=a.base)
    headers: dict = {}
    results: dict[str, dict] = {}
    try:
        email = f"chatqa-{secrets.token_hex(4)}@example.com"
        r = client.post(f"{API}/auth/signup", json={
            "email": email, "password": "ChatQa-" + secrets.token_hex(6) + "!9", "name": "Chat QA",
            "role": "both", "company_name": "ChatQA Traders", "city": "Mumbai"}, timeout=30)
        if r.status_code != 201:
            out(f"signup failed: HTTP {r.status_code} {r.text[:300]}")
            return 1
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        out(f"signed up {email}")
        for name in only:
            fn = SCENARIOS[name]
            out(f"\n=== {name} ===")
            cv = Conv(client, headers, name)
            try:
                fn(cv, client, headers)
            except Exception:
                cv.check(False, "scenario crashed", traceback.format_exc()[-300:])
            ok = all(x for x, _ in cv.checks) and bool(cv.checks)
            results[name] = {"ok": ok, "passed": sum(x for x, _ in cv.checks), "total": len(cv.checks),
                             "conversation_id": cv.conv_id, "final_draft": cv.draft}
            out(f"{name}: {'PASS' if ok else 'FAIL'} ({results[name]['passed']}/{results[name]['total']} checks)")
            if cv.created:
                CREATED_BY_US.append(cv.created["id"])
    finally:
        if headers:
            cleanup(client, headers)
    npass = sum(1 for v in results.values() if v["ok"])
    cp = sum(1 for c in CHECKS if c["ok"])
    lat = ALL_TURNS or [0.0]
    out(f"\nSUMMARY: {npass}/{len(results)} conversations passed, {cp}/{len(CHECKS)} checks passed, "
        f"turn latency p50 {statistics.median(lat):.1f}s max {max(lat):.1f}s")
    for c in CHECKS:
        if not c["ok"]:
            out(f"FAIL {c['conv']}: {c['label']} -- {c['detail']}"[:300])
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"conversations": results, "checks": CHECKS, "turn_secs": ALL_TURNS}, f, indent=1, default=str)
    return 0 if npass == len(results) and results else 1


if __name__ == "__main__":
    sys.exit(main())

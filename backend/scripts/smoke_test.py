#!/usr/bin/env python
"""Walk every user-facing flow against a running instance.

The pytest suite runs against a throwaway database with the vector index stubbed
out. This does the opposite: it drives a *real* deployment over HTTP, with
whatever Qdrant, Ollama and data that instance actually has, and reports what a
person would see.

    .venv/bin/python scripts/smoke_test.py
    .venv/bin/python scripts/smoke_test.py --base-url https://api.example.com/api/v1

It signs up two throwaway accounts, exercises the flows between them and deletes
nothing else. Every account it creates is on ``@smoke.example.com`` and
is removed at the end, so it is safe to run against a populated environment --
though it does leave a handful of match_searches rows behind, as any real use
would.

Exit code 0 means every check passed.
"""

import argparse
import sys
import uuid
from datetime import UTC, datetime
from typing import Any, Optional

import httpx

DEFAULT_BASE = "http://127.0.0.1:8010/api/v1"
PASSWORD = "smoke-test-password"

_PASS = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"


class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def that(self, description: str, condition: Any, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  {_PASS} {description}")
            return True
        self.failed.append(description)
        print(f"  {_FAIL} {description}{f' -- {detail}' if detail else ''}")
        return False

    def section(self, title: str) -> None:
        print(f"\n{title}")


class Account:
    def __init__(self, client: httpx.Client, email: str, tokens: dict) -> None:
        self.client = client
        self.email = email
        self.tokens = tokens
        self.id: Optional[str] = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens['access_token']}"}

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.client.get(url, headers=self.headers, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.client.post(url, headers=self.headers, **kw)

    def patch(self, url: str, **kw: Any) -> httpx.Response:
        return self.client.patch(url, headers=self.headers, **kw)


def signup(client: httpx.Client, checks: Checks, role: str, **profile: Any) -> Account:
    email = f"smoke-{uuid.uuid4().hex[:10]}@smoke.example.com"
    body = {"email": email, "password": PASSWORD, "name": profile.pop("name", "Smoke Tester"),
            "role": role, **profile}
    response = client.post("/auth/signup", json=body)
    if not checks.that(f"signup as {role}", response.status_code == 201, response.text):
        # Nothing downstream can run without an account, and a cascade of
        # secondary failures would hide the one that matters.
        raise SystemExit(f"cannot continue: signup failed with {response.status_code}")
    account = Account(client, email, response.json())
    me = account.get("/auth/me")
    account.id = me.json().get("id")
    return account


def listing(role: str, title: str, city: str, **extra: Any) -> dict:
    body: dict[str, Any] = {
        "role": role,
        "category": "Electronics",
        "title": title,
        "product_details": {"name": "usb type-c cable", "colour": "red", "length": "1m"},
        "quantity": {"value": 6000 if role == "buyer" else 9000, "unit": "pcs"},
        "price_target": {"amount": 200 if role == "buyer" else 180,
                         "currency": "INR", "per_unit": "pcs"},
        "location": {"city": city},
        "deadline": {"in_days": 21, "raw": "within 21 days"},
        "status": "active",
    }
    body.update(extra)
    return body


def run(base_url: str) -> int:
    checks = Checks()
    print(f"Smoke test against {base_url}")
    print(f"Started {datetime.now(UTC).isoformat(timespec='seconds')}")

    with httpx.Client(base_url=base_url, timeout=180) as client:
        # --- infrastructure ---------------------------------------------------
        checks.section("Health")
        health = client.get("/health")
        checks.that("API answers", health.status_code == 200, health.text)
        body = health.json() if health.status_code == 200 else {}
        checks.that("database reachable", body.get("database") == "ok", str(body))

        # --- accounts ---------------------------------------------------------
        checks.section("Accounts")
        buyer = signup(client, checks, "buyer", company_name="Smoke Buyers Ltd", city="Indore")
        seller = signup(client, checks, "seller", name="Smoke Seller",
                        company_name="Smoke Supply Co", phone="+91 90000 00001", city="Pune")

        login = client.post("/auth/login", json={"email": buyer.email, "password": PASSWORD})
        checks.that("login works", login.status_code == 200, login.text)
        checks.that("wrong password rejected",
                    client.post("/auth/login",
                                json={"email": buyer.email, "password": "nope"}).status_code == 401)
        checks.that("unauthenticated request rejected",
                    client.get("/rfqs").status_code == 401)

        refreshed = client.post("/auth/refresh",
                                json={"refresh_token": buyer.tokens["refresh_token"]})
        checks.that("refresh token exchanges", refreshed.status_code == 200, refreshed.text)

        # --- profile ----------------------------------------------------------
        checks.section("Profile")
        profile = seller.patch("/users/me/profile", json={"city": "pune"})
        checks.that("profile saves", profile.status_code == 200, profile.text)
        checks.that("a known city is geocoded",
                    profile.json().get("latitude") is not None, profile.text)
        checks.that("role can be changed",
                    buyer.patch("/users/me", json={"role": "both"}).status_code == 200)
        buyer.patch("/users/me", json={"role": "buyer"})

        # --- listings ---------------------------------------------------------
        checks.section("Listings")
        created = buyer.post("/rfqs", json=listing("buyer", "SMOKE need 6000 red Type-C cables",
                                                   "Indore"))
        checks.that("buyer RFQ created", created.status_code == 201, created.text)
        buyer_rfq = created.json()
        checks.that("city geocoded on write",
                    (buyer_rfq.get("location") or {}).get("latitude") is not None)
        checks.that("search tags derived", bool(buyer_rfq.get("search_tags")))

        sell = seller.post("/rfqs", json=listing("seller", "SMOKE supplying 9000 red Type-C cables",
                                                 "Pune"))
        checks.that("seller RFQ created", sell.status_code == 201, sell.text)
        seller_rfq = sell.json()

        draft = buyer.post("/rfqs", json=listing("buyer", "SMOKE draft", "Indore", status="draft"))
        draft_id = draft.json()["id"]
        checks.that("matching a draft is refused",
                    buyer.post(f"/rfqs/{draft_id}/matches").status_code == 409)
        checks.that("draft publishes",
                    buyer.post(f"/rfqs/{draft_id}/publish").status_code == 200)
        checks.that("listing closes",
                    buyer.post(f"/rfqs/{draft_id}/close").status_code == 200)

        edited = buyer.patch(f"/rfqs/{buyer_rfq['id']}",
                             json={"title": "SMOKE need 7000 red Type-C cables"})
        checks.that("listing edits in place", edited.status_code == 200, edited.text)
        checks.that("edit applied",
                    edited.json().get("title") == "SMOKE need 7000 red Type-C cables")
        checks.that("someone else's listing is a 404",
                    seller.patch(f"/rfqs/{buyer_rfq['id']}",
                                 json={"title": "hijacked"}).status_code == 404)

        listed = buyer.get("/rfqs", params={"limit": 24})
        checks.that("own listings visible", listed.status_code == 200)
        checks.that("only own listings returned",
                    all(item["user_id"] == buyer.id for item in listed.json()["items"]))

        # --- matching ---------------------------------------------------------
        checks.section("Matching")
        matched = buyer.post(f"/rfqs/{buyer_rfq['id']}/matches", params={"limit": 10})
        checks.that("matching runs", matched.status_code == 200, matched.text)
        results = matched.json().get("results", []) if matched.status_code == 200 else []
        checks.that("matching returns candidates", bool(results),
                    "the index may be empty -- run scripts/index_vectors.py --all")
        checks.that("a buyer only ever sees sellers",
                    all(r["role"] == "seller" for r in results))
        checks.that("own listings never matched",
                    all(r["rfq_id"] != buyer_rfq["id"] for r in results))

        ours = next((r for r in results if r["rfq_id"] == seller_rfq["id"]), None)
        checks.that("the obvious counterparty is matched", ours is not None)
        if ours:
            checks.that("distance is reported", ours.get("distance_km") is not None)
            checks.that("score is explained",
                        all(k in ours["score"] for k in
                            ("relevance", "attributes", "price", "quantity", "location")))
            checks.that("contact details are hidden before consent",
                        ours["counterparty"]["email"] is None
                        and ours["counterparty"]["phone"] is None)
        checks.that("seller's email appears nowhere in the response",
                    seller.email not in matched.text)

        # --- connections ------------------------------------------------------
        checks.section("Connections")
        request = buyer.post("/connections", json={"rfq_id": seller_rfq["id"]})
        checks.that("connection requested", request.status_code == 201, request.text)
        connection = request.json()
        checks.that("request starts pending", connection.get("status") == "pending")
        checks.that("request names the listing", bool(connection.get("rfq_title")))
        checks.that("asking twice is idempotent",
                    buyer.post("/connections",
                               json={"rfq_id": seller_rfq["id"]}).json()["id"] == connection["id"])
        checks.that("cannot request your own listing",
                    seller.post("/connections",
                                json={"rfq_id": seller_rfq["id"]}).status_code == 400)

        card = buyer.post(f"/rfqs/{buyer_rfq['id']}/matches", params={"limit": 10}).json()
        pending_card = next((r for r in card["results"] if r["rfq_id"] == seller_rfq["id"]), None)
        checks.that("the card reports the pending request",
                    pending_card and pending_card["counterparty"]["connection_status"] == "pending")

        checks.that("only the receiver may answer",
                    buyer.post(f"/connections/{connection['id']}/accept").status_code == 403)
        checks.that("messages blocked before acceptance",
                    buyer.post(f"/connections/{connection['id']}/messages",
                               json={"content": "hello"}).status_code == 409)

        accepted = seller.post(f"/connections/{connection['id']}/accept")
        checks.that("request accepted", accepted.status_code == 200, accepted.text)
        checks.that("a decision cannot be retaken",
                    seller.post(f"/connections/{connection['id']}/reject").status_code == 409)

        unlocked = buyer.get(f"/connections/{connection['id']}").json()["counterparty"]
        checks.that("contact revealed after acceptance",
                    unlocked.get("email") == seller.email and unlocked.get("phone"))

        checks.that("message sends",
                    buyer.post(f"/connections/{connection['id']}/messages",
                               json={"content": "Can you quote CIF?"}).status_code == 201)
        checks.that("reply sends",
                    seller.post(f"/connections/{connection['id']}/messages",
                                json={"content": "Yes, today."}).status_code == 201)
        thread = buyer.get(f"/connections/{connection['id']}/messages").json()
        checks.that("both sides read the same thread",
                    [m["content"] for m in thread] == ["Can you quote CIF?", "Yes, today."])
        checks.that("empty message refused",
                    buyer.post(f"/connections/{connection['id']}/messages",
                               json={"content": ""}).status_code == 422)

        # --- direct search ----------------------------------------------------
        checks.section("Direct search")
        turn = buyer.post("/search", json={"message": "i want red usb type c cable"})
        checks.that("search answers", turn.status_code == 200, turn.text)
        first = turn.json() if turn.status_code == 200 else {}
        checks.that("an incomplete query asks before searching",
                    first.get("pending_question") is not None and not first.get("results"))

        conversation = first.get("conversation_id")
        answers = {"quantity": "6000 pcs", "price": "200 inr per piece",
                   "location": "indore", "deadline": "21 days"}
        guard = 0
        while first.get("pending_question") and guard < 8:
            guard += 1
            first = buyer.post("/search",
                               json={"message": answers.get(first["pending_question"], "skip"),
                                     "conversation_id": conversation}).json()
        checks.that("the conversation reaches a search", guard < 8)
        checks.that("search returns results", bool(first.get("results")),
                    "index may be empty")
        checks.that("direct search creates no RFQ",
                    buyer.get("/rfqs", params={"limit": 100}).json()["total"]
                    == listed.json()["total"])
        checks.that("a stale conversation id is refused",
                    buyer.post("/search", json={"message": "hi",
                                                "conversation_id": str(uuid.uuid4())}
                               ).status_code == 404)
        checks.that("an empty message is refused",
                    buyer.post("/search", json={"message": "   "}).status_code == 422)

        # --- cleanup ----------------------------------------------------------
        checks.section("Cleanup")
        removed = 0
        for account in (buyer, seller):
            for item in account.get("/rfqs", params={"limit": 100}).json()["items"]:
                account.post(f"/rfqs/{item['id']}/close")
                removed += 1
        checks.that(f"closed {removed} smoke listings", removed >= 3)
        print("  note: the two smoke.example.com accounts remain; "
              "delete them from the database if you care.")

    print(f"\n{checks.passed} passed, {len(checks.failed)} failed")
    for failure in checks.failed:
        print(f"  {_FAIL} {failure}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE,
                        help=f"API root including /api/v1 (default: {DEFAULT_BASE})")
    sys.exit(run(parser.parse_args().base_url))

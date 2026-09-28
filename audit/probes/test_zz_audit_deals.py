"""AUDIT PROBES: deal flow after matching (connections, escrow, disputes, logistics,
PDFs, dashboard, currency, translation).

Each test asserts the *expected secure/correct* behaviour. A failing test is a
finding that demonstrates a defect. Temporary file -- not part of the suite.
"""

import asyncio
import base64
import re
import types
import uuid
import zlib
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from models.connection import Connection, ConnectionMessage
from models.enums import ConnectionStatus
from models.escrow import EscrowAccount
from models.user import User
from services import currency, logistics_service, translation_service
from services.ai_chat_service import execute_draft_counterparty_message_call


QUOTE = {
    "unit_price": 100.00,
    "currency": "USD",
    "quantity": 100.0,
    "quantity_unit": "units",
    "lead_time_days": 10,
    "incoterms": "FOB",
    "payment_terms": "Escrow 30/40/30",
    "valid_days": 14,
    "notes": "Audit batch",
}

DISPUTE = {
    "title": "Carton mismatch",
    "category": "quality",
    "reason": "Single-wall cartons shipped instead of double-wall export boxes.",
    "severity": "high",
}


async def _pending_pair(make_actor, make_rfq):
    buyer = await make_actor("buyer", name="Buyer P", company_name="P Buy")
    seller = await make_actor("seller", name="Seller P", company_name="P Sell", phone="+919999999999")
    listing = await make_rfq(seller, role="seller", title="Supplying 10,000 ball bearings")
    res = await buyer.post("/connections", json={"rfq_id": listing["id"]})
    assert res.status_code == 201, res.text
    return buyer, seller, res.json()["id"], listing


async def _accepted_quote(buyer, seller, conn_id, **overrides):
    body = {**QUOTE, **overrides}
    q = await seller.post(f"/connections/{conn_id}/quotes", json=body)
    assert q.status_code == 201, q.text
    qid = q.json()["id"]
    a = await buyer.post(f"/connections/{conn_id}/quotes/{qid}/accept")
    assert a.status_code == 200, a.text
    return qid


async def _funded(accepted_pair):
    buyer, seller, conn_id, _ = accepted_pair
    qid = await _accepted_quote(buyer, seller, conn_id)
    f = await buyer.post(f"/connections/{conn_id}/escrow/fund", json={"payment_method": "mock_instant"})
    assert f.status_code == 200, f.text
    return buyer, seller, conn_id, qid, f.json()


def _pdf_page_text(raw: bytes) -> bytes:
    """Decode ReportLab's ASCII85+Flate page streams into raw content operators."""
    out = b""
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        s = m.group(1).strip()
        for dec in (lambda x: zlib.decompress(base64.a85decode(x, adobe=True)), zlib.decompress):
            try:
                out += dec(s)
                break
            except Exception:
                continue
    return out


# =========================== CONNECTIONS ====================================


async def test_connection_guards_baseline(make_actor, make_rfq):
    """Guards that SHOULD hold: sender can't accept own request, outsider can't read/send,
    no messaging while pending/after rejection, rejected can't be re-accepted or re-requested."""
    buyer, seller, conn_id, listing = await _pending_pair(make_actor, make_rfq)
    outsider = await make_actor()

    assert (await buyer.post(f"/connections/{conn_id}/accept")).status_code == 403
    assert (await buyer.post(f"/connections/{conn_id}/messages", json={"content": "hi"})).status_code == 409
    assert (await outsider.get(f"/connections/{conn_id}/messages")).status_code == 403
    assert (await outsider.post(f"/connections/{conn_id}/messages", json={"content": "x"})).status_code == 403
    dup = await buyer.post("/connections", json={"rfq_id": listing["id"]})
    assert dup.status_code == 201 and dup.json()["id"] == conn_id

    assert (await seller.post(f"/connections/{conn_id}/reject")).status_code == 200
    assert (await seller.post(f"/connections/{conn_id}/accept")).status_code == 409
    assert (await buyer.post("/connections", json={"rfq_id": listing["id"]})).status_code == 409
    assert (await seller.post(f"/connections/{conn_id}/messages", json={"content": "x"})).status_code == 409


async def test_concurrent_duplicate_connection_requests_single_row(make_actor, make_rfq, db):
    """Five simultaneous connection requests from one buyer produce exactly one row."""
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    listing = await make_rfq(seller, role="seller", title="Supplying steel rods")
    res = await asyncio.gather(*[buyer.post("/connections", json={"rfq_id": listing["id"]}) for _ in range(5)])
    assert all(r.status_code == 201 for r in res), [r.status_code for r in res]
    n = await db.scalar(select(func.count()).select_from(Connection).where(Connection.rfq_id == uuid.UUID(listing["id"])))
    assert n == 1


async def test_ai_draft_tool_lets_sender_self_accept_pending_request(make_actor, make_rfq, db):
    """The AI 'draft counterparty message' tool must not accept a pending request on the
    receiver's behalf; connection_service says only the receiver may accept."""
    buyer, seller, conn_id, _ = await _pending_pair(make_actor, make_rfq)
    user = await db.get(User, uuid.UUID(buyer.id))
    await execute_draft_counterparty_message_call({"message": "Hello", "connection_id": conn_id}, db=db, user=user)

    view = (await buyer.get(f"/connections/{conn_id}")).json()
    assert view["status"] == "pending", f"sender self-accepted; contact leaked: {view['counterparty']}"
    assert view["counterparty"]["email"] is None


async def test_ai_draft_tool_reverses_a_rejection(make_actor, make_rfq, db):
    """A REJECTED connection must stay rejected; the AI draft tool must not revive it."""
    buyer, seller, conn_id, _ = await _pending_pair(make_actor, make_rfq)
    assert (await seller.post(f"/connections/{conn_id}/reject")).status_code == 200
    user = await db.get(User, uuid.UUID(buyer.id))
    await execute_draft_counterparty_message_call({"message": "Please reconsider", "connection_id": conn_id}, db=db, user=user)

    view = (await buyer.get(f"/connections/{conn_id}")).json()
    assert view["status"] == "rejected", "rejection undone; buyer can now message seller"


async def test_ai_draft_tool_idor_outsider_connection_id(make_actor, make_rfq, db):
    """An outsider passing someone else's connection_id (client-controlled via
    active_connection_id) must get an error, not a draft bound to that deal room."""
    buyer, seller, conn_id, _ = await _pending_pair(make_actor, make_rfq)
    outsider = await make_actor("buyer")
    user = await db.get(User, uuid.UUID(outsider.id))
    result, err = await execute_draft_counterparty_message_call(
        {"message": "hijack"}, db=db, user=user, active_connection_id=conn_id
    )
    row = await db.get(Connection, uuid.UUID(conn_id))
    await db.refresh(row)
    assert result is None and err, f"outsider got draft {result}"
    assert row.status is ConnectionStatus.PENDING, "outsider flipped a stranger's connection to ACCEPTED"


async def test_client_can_spoof_live_capture_and_external_image(accepted_pair):
    """is_live_capture / image_url are client-supplied on the JSON message endpoint, so a
    'verified live camera snapshot' can be forged with any URL and skips image moderation."""
    buyer, seller, conn_id, _ = accepted_pair
    r = await buyer.post(
        f"/connections/{conn_id}/messages",
        json={"content": "Live proof of goods", "image_url": "javascript:alert(document.cookie)", "is_live_capture": True},
    )
    assert r.status_code == 422, f"accepted spoofed live capture: {r.status_code} {r.json()}"


# ============================== ESCROW ======================================


async def test_escrow_guards_baseline(accepted_pair, make_actor):
    """Guards that SHOULD hold: no release before funding, seller can't release, outsider
    can't dispute, released milestone can't be released again."""
    buyer, seller, conn_id, _ = accepted_pair
    outsider = await make_actor()
    await _accepted_quote(buyer, seller, conn_id)
    esc = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    m1 = esc["milestones"][0]["id"]
    assert (await buyer.post(f"/connections/{conn_id}/escrow/milestones/{m1}/release", json={})).status_code == 400
    await buyer.post(f"/connections/{conn_id}/escrow/fund", json={})
    assert (await seller.post(f"/connections/{conn_id}/escrow/milestones/{m1}/release", json={})).status_code == 403
    assert (await outsider.post(f"/connections/{conn_id}/disputes", json=DISPUTE)).status_code == 403
    assert (await buyer.post(f"/connections/{conn_id}/escrow/milestones/{m1}/release", json={})).status_code == 200
    assert (await buyer.post(f"/connections/{conn_id}/escrow/milestones/{m1}/release", json={})).status_code == 400


async def test_escrow_fund_requires_no_real_payment(accepted_pair):
    """Funding flips funded_amount to the full total for ANY payment_method string with no
    payment reference/verification; the vault is 'secured' without money moving."""
    buyer, seller, conn_id, _ = accepted_pair
    await _accepted_quote(buyer, seller, conn_id)
    r = await buyer.post(f"/connections/{conn_id}/escrow/fund", json={"payment_method": "i_promise"})
    assert r.status_code in (400, 422), f"funded with bogus method: {r.json()['status']} {r.json()['funded_amount']}"


async def test_seller_unilaterally_resolves_buyer_dispute_with_release(accepted_pair):
    """The party being disputed must not be able to resolve the dispute alone by
    releasing all escrow to itself."""
    buyer, seller, conn_id, _, _ = await _funded(accepted_pair)
    d = (await buyer.post(f"/connections/{conn_id}/disputes", json=DISPUTE)).json()
    r = await seller.post(
        f"/connections/{conn_id}/disputes/{d['id']}/resolve",
        json={"resolution": "release_funds", "resolution_notes": "I decide I get paid"},
    )
    esc = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    assert r.status_code == 403, f"seller self-resolved: status={esc['status']} released={esc['released_amount']}"


async def test_buyer_raises_and_resolves_own_dispute_refund(accepted_pair):
    """Buyer can open a dispute and immediately resolve it as refund_buyer (after the
    seller already shipped) -- no counter-party consent or mediator."""
    buyer, seller, conn_id, _, _ = await _funded(accepted_pair)
    d = (await buyer.post(f"/connections/{conn_id}/disputes", json=DISPUTE)).json()
    r = await buyer.post(
        f"/connections/{conn_id}/disputes/{d['id']}/resolve",
        json={"resolution": "refund_buyer", "resolution_notes": "refund me now"},
    )
    assert r.status_code == 403, "dispute raiser unilaterally refunded themselves"


async def test_dispute_resolved_twice_double_payout(accepted_pair):
    """A resolved dispute must not be resolvable again. Resolving refund_buyer then
    release_funds makes released + refunded exceed the funded amount (double spend)."""
    buyer, seller, conn_id, _, _ = await _funded(accepted_pair)
    d = (await buyer.post(f"/connections/{conn_id}/disputes", json=DISPUTE)).json()
    url = f"/connections/{conn_id}/disputes/{d['id']}/resolve"
    r1 = await buyer.post(url, json={"resolution": "refund_buyer", "resolution_notes": "refund first"})
    r2 = await seller.post(url, json={"resolution": "release_funds", "resolution_notes": "then release"})
    esc = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    paid_out = Decimal(esc["released_amount"]) + Decimal(esc["refunded_amount"])
    assert r1.status_code == 200
    assert r2.status_code == 409, f"re-resolved; released={esc['released_amount']} refunded={esc['refunded_amount']}"
    assert paid_out <= Decimal(esc["funded_amount"])


async def test_dispute_resolution_value_not_validated(accepted_pair):
    """resolution/category/severity are free strings: 'bogus' unfreezes escrow via the
    mutual-settlement else-branch; an over-long value overflows String(32)/String(64) (500)."""
    buyer, seller, conn_id, _, _ = await _funded(accepted_pair)
    d = (await buyer.post(f"/connections/{conn_id}/disputes", json=DISPUTE)).json()
    r = await seller.post(
        f"/connections/{conn_id}/disputes/{d['id']}/resolve",
        json={"resolution": "bogus", "resolution_notes": "whatever"},
    )
    assert r.status_code == 422, f"bogus resolution accepted: {r.json().get('status')}"


async def test_dispute_overlong_category_500(accepted_pair):
    """A 100-char category must be a 422, not a DB overflow 500."""
    buyer, seller, conn_id, _, _ = await _funded(accepted_pair)
    try:
        r = await buyer.post(f"/connections/{conn_id}/disputes", json={**DISPUTE, "category": "c" * 100})
        code = r.status_code
    except Exception as exc:  # ASGITransport re-raises unhandled app errors
        code = f"500 ({type(exc).__name__})"
    assert code == 422, code


async def test_dispute_on_unfunded_escrow_then_settlement_marks_funded(accepted_pair):
    """Disputing a pending_deposit escrow then 'mutual_settlement' sets status=funded with
    funded_amount=0; fund_escrow then short-circuits so the deal can never be funded."""
    buyer, seller, conn_id, _ = accepted_pair
    await _accepted_quote(buyer, seller, conn_id)
    await buyer.get(f"/connections/{conn_id}/escrow")
    d = (await buyer.post(f"/connections/{conn_id}/disputes", json=DISPUTE)).json()
    await buyer.post(
        f"/connections/{conn_id}/disputes/{d['id']}/resolve",
        json={"resolution": "mutual_settlement", "resolution_notes": "ok settled"},
    )
    esc = (await buyer.post(f"/connections/{conn_id}/escrow/fund", json={})).json()
    assert not (esc["status"] == "funded" and Decimal(esc["funded_amount"]) == 0), \
        f"escrow shows 'funded' with 0 funds: {esc['status']} {esc['funded_amount']}"


async def test_dispute_on_completed_escrow_reopens_it(accepted_pair):
    """After every milestone is released (completed), a new dispute must be rejected,
    not flip a closed vault to 'disputed' / 'refunded'."""
    buyer, seller, conn_id, _, esc = await _funded(accepted_pair)
    for m in esc["milestones"]:
        await buyer.post(f"/connections/{conn_id}/escrow/milestones/{m['id']}/release", json={})
    assert (await buyer.get(f"/connections/{conn_id}/escrow")).json()["status"] == "completed"
    r = await buyer.post(f"/connections/{conn_id}/disputes", json=DISPUTE)
    after = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    assert r.status_code == 409, f"dispute accepted on completed escrow -> status {after['status']}"


async def test_resolving_one_of_two_disputes_unfreezes_escrow(accepted_pair):
    """With two open disputes, settling one must keep escrow frozen."""
    buyer, seller, conn_id, _, esc = await _funded(accepted_pair)
    d1 = (await buyer.post(f"/connections/{conn_id}/disputes", json=DISPUTE)).json()
    await buyer.post(f"/connections/{conn_id}/disputes", json={**DISPUTE, "title": "Late delivery"})
    await seller.post(
        f"/connections/{conn_id}/disputes/{d1['id']}/resolve",
        json={"resolution": "mutual_settlement", "resolution_notes": "settle first one"},
    )
    after = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    assert after["status"] == "disputed", f"escrow unfrozen with a dispute still open: {after['status']}"


async def test_concurrent_release_of_two_milestones_lost_update(accepted_pair):
    """Releasing m1 and m2 concurrently must total 3000+4000=7000 released."""
    buyer, seller, conn_id, _, esc = await _funded(accepted_pair)
    m1, m2 = esc["milestones"][0]["id"], esc["milestones"][1]["id"]
    res = await asyncio.gather(
        buyer.post(f"/connections/{conn_id}/escrow/milestones/{m1}/release", json={}),
        buyer.post(f"/connections/{conn_id}/escrow/milestones/{m2}/release", json={}),
    )
    after = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    released_ms = sum(Decimal(m["amount"]) for m in after["milestones"] if m["status"] == "released")
    assert [r.status_code for r in res] == [200, 200]
    assert Decimal(after["released_amount"]) == released_ms == Decimal("7000"), \
        f"released_amount={after['released_amount']} but milestones released sum={released_ms}"


async def test_concurrent_double_release_same_milestone(accepted_pair, db):
    """Two simultaneous releases of the same milestone: exactly one should succeed."""
    buyer, seller, conn_id, _, esc = await _funded(accepted_pair)
    m1 = esc["milestones"][0]["id"]
    res = await asyncio.gather(*[
        buyer.post(f"/connections/{conn_id}/escrow/milestones/{m1}/release", json={}) for _ in range(4)
    ])
    ok = [r.status_code for r in res].count(200)
    notices = await db.scalar(
        select(func.count()).select_from(ConnectionMessage).where(
            ConnectionMessage.connection_id == uuid.UUID(conn_id),
            ConnectionMessage.content.like("%MILESTONE FUNDS RELEASED%"),
        )
    )
    after = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    assert ok == 1 and notices == 1, f"{ok} successful releases, {notices} payout notices, released={after['released_amount']}"


async def test_concurrent_escrow_get_creates_duplicate_vaults(accepted_pair, db):
    """GET /escrow lazily creates the vault; concurrent GETs must not create several."""
    buyer, seller, conn_id, _ = accepted_pair
    await _accepted_quote(buyer, seller, conn_id)
    await asyncio.gather(*[buyer.get(f"/connections/{conn_id}/escrow") for _ in range(5)])
    n = await db.scalar(select(func.count()).select_from(EscrowAccount).where(EscrowAccount.connection_id == uuid.UUID(conn_id)))
    assert n == 1, f"{n} escrow vaults created for one connection"


async def test_second_accepted_quote_leaves_escrow_on_stale_amount(accepted_pair):
    """After escrow exists for v1 (10,000), a v2 quote at 20,000 can also be accepted;
    two ACCEPTED quotes coexist and escrow still secures only 10,000."""
    buyer, seller, conn_id, _ = accepted_pair
    await _accepted_quote(buyer, seller, conn_id)
    await buyer.get(f"/connections/{conn_id}/escrow")
    q2 = await seller.post(f"/connections/{conn_id}/quotes", json={**QUOTE, "unit_price": 200.0})
    a2 = await buyer.post(f"/connections/{conn_id}/quotes/{q2.json()['id']}/accept")
    esc = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    assert a2.status_code == 409 or Decimal(esc["total_amount"]) == Decimal("20000"), \
        f"v2 accepted ({a2.status_code}) but escrow total still {esc['total_amount']}"


async def test_escrow_unavailable_after_quote_dispatched(accepted_pair):
    """If the seller marks the order dispatched before escrow is opened, escrow can never
    be created (DISPATCHED is missing from the eligible quote statuses)."""
    buyer, seller, conn_id, _ = accepted_pair
    qid = await _accepted_quote(buyer, seller, conn_id)
    d = await seller.post(f"/connections/{conn_id}/quotes/{qid}/dispatch")
    assert d.status_code == 200, d.text
    r = await buyer.get(f"/connections/{conn_id}/escrow")
    assert r.status_code == 200, r.json()


async def test_huge_quote_amount_is_422_not_500(accepted_pair):
    """unit_price*quantity beyond Numeric(18,4) must be rejected cleanly."""
    buyer, seller, conn_id, _ = accepted_pair
    try:
        r = await seller.post(f"/connections/{conn_id}/quotes", json={**QUOTE, "unit_price": 1e13, "quantity": 1e6})
        code = r.status_code
    except Exception as exc:
        code = f"500 ({type(exc).__name__})"
    assert code == 422, code


async def test_milestone_split_sums_to_total_with_odd_amount(accepted_pair):
    """Money precision: milestones for 99.9999 total must sum exactly to the total."""
    buyer, seller, conn_id, _ = accepted_pair
    await _accepted_quote(buyer, seller, conn_id, unit_price=33.3333, quantity=3)
    esc = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    assert sum(Decimal(m["amount"]) for m in esc["milestones"]) == Decimal(esc["total_amount"])


# ============================= LOGISTICS ====================================

SHIP = {"carrier_name": "DHL", "shipping_mode": "road", "weight_kg": 10}


async def test_dispatch_does_not_trigger_escrow_milestone(accepted_pair):
    """trigger_escrow_milestone=True (default) should move the 40% dispatch milestone to
    release_requested. The call is broken (wrong signatures) and the error is swallowed."""
    buyer, seller, conn_id, _, _ = await _funded(accepted_pair)
    r = await seller.post(f"/logistics/connections/{conn_id}/dispatch", json={**SHIP, "trigger_escrow_milestone": True})
    assert r.status_code == 201, r.text
    esc = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    assert esc["milestones"][1]["status"] == "release_requested", esc["milestones"][1]["status"]


async def test_buyer_can_dispatch_and_pending_quote_skips_acceptance(accepted_pair):
    """Only the seller should dispatch, and only against an ACCEPTED quote. Here the buyer
    dispatches and a still-PENDING quote jumps straight to DISPATCHED."""
    buyer, seller, conn_id, _ = accepted_pair
    q = (await seller.post(f"/connections/{conn_id}/quotes", json=QUOTE)).json()
    r = await buyer.post(f"/logistics/connections/{conn_id}/dispatch", json=SHIP)
    quotes = (await buyer.get(f"/connections/{conn_id}/quotes")).json()
    status = next(x["status"] for x in quotes if x["id"] == q["id"])
    assert r.status_code == 403 and status == "pending", f"dispatch={r.status_code}, quote now {status}"


async def test_shipment_status_free_text_and_self_delivery(accepted_pair):
    """Seller can mark own shipment 'delivered' (driving quote -> DELIVERED), then move it
    back to 'dispatched', and set an arbitrary status. Expect enum + forward-only rules."""
    buyer, seller, conn_id, _ = accepted_pair
    await _accepted_quote(buyer, seller, conn_id)
    sh = (await seller.post(f"/logistics/connections/{conn_id}/dispatch", json=SHIP)).json()
    url = f"/logistics/shipments/{sh['id']}/events"
    await seller.post(url, json={"status": "delivered", "location": "Buyer dock"})
    back = await seller.post(url, json={"status": "dispatched", "location": "x"})
    junk = await seller.post(url, json={"status": "teleported", "location": "Mars"})
    assert back.status_code in (409, 422) and junk.status_code == 422, \
        f"regression allowed={back.status_code}, junk status allowed={junk.status_code} ({junk.json().get('status')})"


async def test_duplicate_tracking_number_500(accepted_pair):
    """A user-supplied tracking number already in use must be a 409, not an IntegrityError."""
    buyer, seller, conn_id, _ = accepted_pair
    await _accepted_quote(buyer, seller, conn_id)
    await seller.post(f"/logistics/connections/{conn_id}/dispatch", json={**SHIP, "tracking_number": "DUP-1"})
    try:
        r = await seller.post(f"/logistics/connections/{conn_id}/dispatch", json={**SHIP, "tracking_number": "DUP-1"})
        code = r.status_code
    except Exception as exc:
        code = f"500 ({type(exc).__name__})"
    assert code == 409, code


async def test_dispatch_zero_weight_becomes_50kg(accepted_pair):
    """weight_kg=0 is allowed (ge=0) and silently replaced by 50 kg on the waybill."""
    buyer, seller, conn_id, _ = accepted_pair
    await _accepted_quote(buyer, seller, conn_id)
    r = await seller.post(f"/logistics/connections/{conn_id}/dispatch", json={**SHIP, "weight_kg": 0})
    assert r.status_code == 422 or Decimal(r.json()["weight_kg"]) == 0, f"stored weight {r.json()['weight_kg']}"


async def test_dispatch_huge_weight_500(accepted_pair):
    """weight beyond Numeric(12,3) must be a 422 not a DB overflow."""
    buyer, seller, conn_id, _ = accepted_pair
    await _accepted_quote(buyer, seller, conn_id)
    try:
        r = await seller.post(f"/logistics/connections/{conn_id}/dispatch", json={**SHIP, "weight_kg": 1e12})
        code = r.status_code
    except Exception as exc:
        code = f"500 ({type(exc).__name__})"
    assert code == 422, code


async def test_freight_unknown_cities_collapse_to_50km(client):
    """Kigali (Rwanda) -> Accra (Ghana) is ~4,200 km and Lima -> Santiago ~2,450 km. Unknown
    city+country both fall back to (22.0, 78.0) = 50 km floor; fictional cities get a quote too."""
    for o, oc, d, dc in (("Kigali", "Rwanda", "Accra", "Ghana"), ("Lima", "Peru", "Santiago", "Chile")):
        r = await client.post("/logistics/estimate", json={
            "origin_city": o, "origin_country": oc, "destination_city": d, "destination_country": dc, "weight_kg": 100,
        })
        assert r.status_code == 200
        assert r.json()["distance_km"] > 2000, f"{o}->{d} distance_km={r.json()['distance_km']}"
    fake = await client.post("/logistics/estimate", json={
        "origin_city": "Xyzzyville", "origin_country": "Atlantis", "destination_city": "Qwertown",
        "destination_country": "Mu", "weight_kg": 100})
    assert fake.status_code == 422, "fictional locations priced"


async def test_freight_unknown_currency_labelled_but_usd(client):
    """currency=XYZ is accepted and USD numbers are labelled XYZ (fx fallback 1.0)."""
    r = await client.post("/logistics/estimate", json={"weight_kg": 10, "currency": "XYZ"})
    assert r.status_code == 422, f"{r.status_code} currency={r.json().get('currency')}"


def test_freight_fx_table_disagrees_with_currency_service():
    """logistics_service has its own hard-coded FX table that disagrees with services.currency."""
    diffs = {
        k: (v, float(currency.RATES[k]))
        for k, v in logistics_service.USD_CONVERSIONS.items()
        if k in currency.RATES and abs(v - float(currency.RATES[k])) > 1e-9
    }
    assert not diffs, f"logistics vs currency rates: {diffs}"


async def test_freight_rejects_zero_nan_inf_weight(client):
    """Zero / NaN / Infinity weights are rejected (422)."""
    for w in (0, "NaN", "Infinity", -5):
        r = await client.post("/logistics/estimate", json={"weight_kg": w})
        assert r.status_code == 422, (w, r.status_code)


# ================================ PDFs ======================================


async def test_pdf_unicode_fields_render_as_boxes(make_actor, make_rfq):
    """PDFs use core Helvetica; Hindi/Chinese in notes/company names become ZapfDingbats
    black boxes instead of text."""
    buyer = await make_actor("buyer", company_name="北京贸易有限公司")
    seller = await make_actor("seller", company_name="भारत स्टील")
    listing = await make_rfq(seller, role="seller", title="Supplying steel coils")
    conn_id = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()["id"]
    await seller.post(f"/connections/{conn_id}/accept")
    qid = await _accepted_quote(buyer, seller, conn_id, notes="交货期 30 天 / डिलीवरी 30 दिन")
    r = await buyer.get(f"/connections/{conn_id}/quotes/{qid}/po-pdf")
    assert r.status_code == 200
    assert b"ZapfDingbats" not in r.content, "non-Latin text replaced by ZapfDingbats glyph boxes"


async def test_pdf_long_unbroken_fields_do_not_crash(accepted_pair):
    """2000-char unbroken notes / 120-char payment terms still produce a PDF."""
    buyer, seller, conn_id, _ = accepted_pair
    qid = await _accepted_quote(buyer, seller, conn_id, notes="A" * 2000, payment_terms="B" * 120)
    for kind in ("po-pdf", "invoice-pdf"):
        try:
            r = await buyer.get(f"/connections/{conn_id}/quotes/{qid}/{kind}")
            code = r.status_code
        except Exception as exc:
            code = f"500 ({type(exc).__name__}: {str(exc)[:80]})"
        assert code == 200, (kind, code)


async def test_invoice_claims_escrow_cleared_when_unfunded(accepted_pair):
    """Invoice prints 'B2B-ESCROW-CLEARED-...' although no escrow exists/is funded."""
    buyer, seller, conn_id, _ = accepted_pair
    qid = await _accepted_quote(buyer, seller, conn_id)
    r = await seller.get(f"/connections/{conn_id}/quotes/{qid}/invoice-pdf")
    text = _pdf_page_text(r.content)
    assert b"ESCROW-CLEARED" not in text, "invoice asserts escrow cleared on an unfunded deal"


async def test_invoice_unavailable_after_buyer_receives_goods(accepted_pair):
    """After buyer accepts delivery (status RECEIVED) invoice/PO download must still work."""
    buyer, seller, conn_id, _ = accepted_pair
    qid = await _accepted_quote(buyer, seller, conn_id)
    await seller.post(f"/connections/{conn_id}/quotes/{qid}/dispatch")
    await seller.post(f"/connections/{conn_id}/quotes/{qid}/deliver")
    rec = await buyer.post(f"/connections/{conn_id}/quotes/{qid}/accept-delivery")
    assert rec.status_code == 200, rec.text
    r = await buyer.get(f"/connections/{conn_id}/quotes/{qid}/invoice-pdf")
    assert r.status_code == 200, r.json()


# =========================== DASHBOARD / NOTIFS ==============================


async def test_dashboard_negative_limit_500(make_actor):
    """/dashboard/activity?limit=-5 passes min(-5,50) into SQL LIMIT."""
    a = await make_actor()
    try:
        r = await a.get("/dashboard/activity", params={"limit": -5})
        code = r.status_code
    except Exception as exc:
        code = f"500 ({type(exc).__name__})"
    assert code in (200, 422), code


async def test_dashboard_messages_sent_counts_system_escrow_notices(accepted_pair):
    """Buyer never typed a message, but escrow/dispute system banners are stored with
    sender_id=buyer, inflating messages_sent (and appearing as if the buyer wrote them)."""
    buyer, seller, conn_id, _, _ = await _funded(accepted_pair)
    await buyer.post(f"/connections/{conn_id}/disputes", json=DISPUTE)
    stats = (await buyer.get("/dashboard/stats")).json()
    assert stats["messages_sent"] == 0, f"messages_sent={stats['messages_sent']}"


async def test_no_notifications_for_escrow_and_dispute_events(accepted_pair):
    """Seller should get an in-app notification when escrow is funded and a dispute opened."""
    buyer, seller, conn_id, _, _ = await _funded(accepted_pair)
    await buyer.post(f"/connections/{conn_id}/disputes", json=DISPUTE)
    items = (await seller.get("/notifications")).json()["items"]
    types_ = [str(i.get("type")) for i in items]
    assert any(("escrow" in t or "dispute" in t) for t in types_), f"seller notification types: {types_}"


async def test_no_notification_on_connection_reject(make_actor, make_rfq):
    """Sender should be told their request was declined."""
    buyer, seller, conn_id, _ = await _pending_pair(make_actor, make_rfq)
    before = (await buyer.get("/notifications")).json()["total"]
    await seller.post(f"/connections/{conn_id}/reject")
    after = (await buyer.get("/notifications")).json()["total"]
    assert after > before, "no notification on rejection"


# ============================ CURRENCY =====================================


def test_currency_normalizer_misreads_weight_pounds():
    """'500 pounds of steel' is a weight, not GBP."""
    assert currency.normalize_currency("500 pounds of steel") != "GBP"


async def test_currency_rates_as_of_is_process_start_not_live(client):
    """Rates are a hard-coded table; update_rates() is never called in app code, yet
    /currency/rates reports as_of = import time, implying live data."""
    import inspect
    import pathlib
    root = pathlib.Path(currency.__file__).resolve().parent.parent
    callers = [
        p for p in root.rglob("*.py")
        if ".venv" not in p.parts and "tests" not in p.parts and p.name != "currency.py"
        and "update_rates(" in p.read_text(errors="ignore")
    ]
    assert callers, "no live FX refresh anywhere: RATES are static DEFAULT_RATES"


# ============================ TRANSLATION ==================================


async def test_translation_sends_deal_room_text_to_google(accepted_pair, monkeypatch):
    """When Ollama is down, private deal-room text is sent to translate.googleapis.com
    (unofficial 'gtx' client) with no opt-in."""
    buyer, seller, conn_id, _ = accepted_pair
    seen: list[str] = []

    class FakeResp:
        status_code = 200
        def json(self):
            return [[["Precio confidencial", "x"]], None, "en"]

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k):
            seen.append(url)
            return FakeResp()
        async def post(self, url, **k):
            raise RuntimeError("ollama down")

    monkeypatch.setattr(translation_service, "httpx", types.SimpleNamespace(AsyncClient=FakeClient))
    translation_service._translation_cache.clear()
    r = await buyer.post(
        f"/translation/connections/{conn_id}/translate",
        json={"text": "Our confidential floor price is USD 4.10 per unit", "target_language": "es", "source_language": "en"},
    )
    assert r.status_code == 200
    assert not any("googleapis" in u for u in seen), f"deal text sent to {seen}"


async def test_translation_unsupported_target_silently_english(make_actor):
    """target_language='klingon' should be 422, not a silent 'en' no-op marked cached."""
    a = await make_actor()
    r = await a.post("/translation/translate", json={"text": "Hello partner", "target_language": "klingon"})
    assert r.status_code == 422, r.json()


async def test_translation_batch_items_unbounded(make_actor, monkeypatch):
    """Batch caps count (50) but not item length: 50 x 100k chars accepted (single
    endpoint caps at 5000), each item a sequential LLM/HTTP call -> cheap DoS."""
    a = await make_actor()

    async def fake(text, target_language, source_language=None):
        return translation_service.TranslationResponse(
            original_text=text[:5], translated_text="x", source_language="en", target_language="es")

    monkeypatch.setattr(translation_service, "translate_text", fake)
    r = await a.post("/translation/batch", json={"texts": ["word " * 20000] * 50, "target_language": "es"})
    assert r.status_code == 422, r.status_code

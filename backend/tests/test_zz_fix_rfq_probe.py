"""AUDIT PROBES: RFQ lifecycle, marketplace catalog/search, saved RFQs,
quotations and reviews.

Every test asserts the *expected secure/correct* behaviour. A failing test is a
finding that demonstrates a real defect. Unhandled server exceptions are caught
and reported as status 500 (ASGITransport re-raises app exceptions).
"""

import asyncio
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from models.user import UserProfile
from tests.conftest import rfq_body


async def safe(coro) -> tuple[int, Any]:
    """Run a request; turn an unhandled server exception into a 500."""
    try:
        r = await coro
        try:
            body = r.json()
        except Exception:
            body = r.text
        return r.status_code, body
    except Exception as exc:  # noqa: BLE001
        return 500, repr(exc)[:300]


async def _pair(make_actor, **listing_overrides):
    buyer = await make_actor("buyer", name="Buyer", company_name="BuyCo")
    seller = await make_actor("seller", name="Seller", company_name="SellCo")
    body = rfq_body(role="seller", title="Supplying ball bearings", **listing_overrides)
    res = await seller.post("/rfqs", json=body)
    assert res.status_code == 201, res.text
    listing = res.json()
    conn = (await buyer.post("/connections", json={"rfq_id": listing["id"]})).json()
    acc = await seller.post(f"/connections/{conn['id']}/accept")
    assert acc.status_code == 200, acc.text
    return buyer, seller, conn["id"], listing


async def _expire(db, rid: str) -> None:
    """Deadline.date cannot be used (see test_absolute_deadline_date_is_accepted),
    so push expires_at into the past directly in the test DB."""
    await db.execute(
        text("UPDATE rfqs SET expires_at = now() - interval '1 day', "
             "deadline_at = now() - interval '1 day' WHERE id = :i"),
        {"i": rid},
    )
    await db.commit()


QUOTE = {"unit_price": 10, "currency": "INR", "quantity": 100, "quantity_unit": "pcs"}


async def _full_deal(buyer, seller, conn_id) -> str:
    """seller quotes -> buyer accepts -> dispatch -> deliver -> accept-delivery."""
    q = (await seller.post(f"/connections/{conn_id}/quotes", json=QUOTE)).json()
    qid = q["id"]
    assert (await buyer.post(f"/connections/{conn_id}/quotes/{qid}/accept")).status_code == 200
    assert (await seller.post(f"/connections/{conn_id}/quotes/{qid}/dispatch")).status_code == 200
    assert (await seller.post(f"/connections/{conn_id}/quotes/{qid}/deliver")).status_code == 200
    assert (
        await buyer.post(f"/connections/{conn_id}/quotes/{qid}/accept-delivery")
    ).status_code == 200
    return qid


# ---------------------------------------------------------------------------
# RFQ authorization / state machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_kind", ["draft", "closed"])
async def test_catalog_item_does_not_leak_non_public_rfq(make_actor, status_kind):
    """GET /marketplace/catalog/{id} must 404 for another user's draft/closed RFQ.

    get_catalog_item has no status/expiry filter, so any logged-in user holding
    an id can read unpublished drafts or withdrawn listings.
    """
    owner = await make_actor("seller")
    other = await make_actor("buyer")
    rfq = (
        await owner.post(
            "/rfqs",
            json=rfq_body(role="seller", title="SECRET unreleased draft", status="draft"),
        )
    ).json()
    if status_kind == "closed":
        await owner.post(f"/rfqs/{rfq['id']}/publish")
        await owner.post(f"/rfqs/{rfq['id']}/close")
    r = await other.get(f"/marketplace/catalog/{rfq['id']}")
    assert r.status_code == 404, f"{status_kind} RFQ leaked: {r.status_code} {r.json().get('title')}"


async def test_catalog_item_does_not_leak_expired_rfq(make_actor, db):
    """An expired (past expires_at) listing must not be served by the detail endpoint."""
    owner = await make_actor("seller")
    other = await make_actor("buyer")
    rfq = (await owner.post("/rfqs", json=rfq_body(role="seller"))).json()
    await _expire(db, rfq["id"])
    r = await other.get(f"/marketplace/catalog/{rfq['id']}")
    assert r.status_code == 404, r.status_code


async def test_other_user_cannot_read_patch_close_publish_rfq(make_actor):
    """IDOR on /rfqs/{id}: all single-RFQ routes must 404 for a non-owner."""
    owner = await make_actor("buyer")
    other = await make_actor("buyer")
    rfq = (await owner.post("/rfqs", json=rfq_body(status="draft"))).json()
    rid = rfq["id"]
    assert (await other.get(f"/rfqs/{rid}")).status_code == 404
    assert (await other.patch(f"/rfqs/{rid}", json={"title": "pwned"})).status_code == 404
    assert (await other.post(f"/rfqs/{rid}/publish")).status_code == 404
    assert (await other.post(f"/rfqs/{rid}/close")).status_code == 404


async def test_closed_rfq_cannot_be_reopened_via_patch(make_actor):
    """/publish refuses non-drafts, but PATCH {status: active} reopens a closed RFQ.

    Expected: closed is terminal (409/422). Actual: the state machine is bypassed.
    """
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    await owner.post(f"/rfqs/{rid}/close")
    assert (await owner.post(f"/rfqs/{rid}/publish")).status_code == 409
    r = await owner.patch(f"/rfqs/{rid}", json={"status": "active"})
    assert r.status_code in (409, 422), f"reopened: {r.status_code} status={r.json().get('status')}"


async def test_patch_cannot_set_arbitrary_status(make_actor):
    """PATCH should not let a client set 'expired' or move active back to 'draft'."""
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    r1 = await owner.patch(f"/rfqs/{rid}", json={"status": "expired"})
    r2 = await owner.patch(f"/rfqs/{rid}", json={"status": "draft"})
    assert r1.status_code in (409, 422) and r2.status_code in (409, 422), (
        r1.status_code, r1.json().get("status"), r2.status_code, r2.json().get("status")
    )


async def test_closed_rfq_content_is_immutable(make_actor):
    """Editing the title/price of a closed RFQ should be refused."""
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    await owner.post(f"/rfqs/{rid}/close")
    r = await owner.patch(f"/rfqs/{rid}", json={"title": "edited after close"})
    assert r.status_code in (409, 422), r.status_code


async def test_absolute_deadline_date_is_accepted(make_actor):
    """Deadline has a field named `date` annotated Optional[date]; the name shadows
    the type so Pydantic makes it NoneType -> EVERY absolute date is rejected
    ("Input should be None"). AI-chat drafts with a date silently lose the deadline."""
    owner = await make_actor("buyer")
    body = rfq_body()
    body["deadline"] = {"date": "2031-03-15"}
    r = await owner.post("/rfqs", json=body)
    assert r.status_code == 201, (r.status_code, r.text[:200])


async def test_create_rfq_rejects_deadline_in_past(make_actor):
    """deadline.date=2000-01-01 should be rejected (passes only because of the
    shadowing bug above, which rejects all dates)."""
    owner = await make_actor("buyer")
    body = rfq_body()
    body["deadline"] = {"date": "2000-01-01"}
    status, data = await safe(owner.post("/rfqs", json=body))
    assert status == 422, (status, data.get("status") if isinstance(data, dict) else data,
                           data.get("expires_at") if isinstance(data, dict) else None)


async def test_in_days_zero_listing_is_immediately_invisible(make_actor):
    """deadline.in_days=0 must be rejected with 422 rather than creating an immediately expired listing."""
    owner = await make_actor("seller")
    r = await owner.post("/rfqs", json=rfq_body(role="seller", deadline_days=0))
    assert r.status_code == 422, f"Expected 422 for in_days=0, got {r.status_code}: {r.text}"


async def test_expired_rfq_status_reflects_expiry(make_actor, db):
    """expire_due_rfqs() is never scheduled, so owners see 'active' forever."""
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    await _expire(db, rid)
    got = (await owner.get(f"/rfqs/{rid}")).json()
    assert got["status"] == "expired", (got["status"], got["expires_at"])


async def test_publish_refuses_already_expired_draft(make_actor, db):
    """Publishing a draft whose expires_at already passed yields a dead 'active' row."""
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body(status="draft"))).json()["id"]
    await _expire(db, rid)
    r = await owner.post(f"/rfqs/{rid}/publish")
    assert r.status_code in (409, 422), (r.status_code, r.json().get("status"))


async def test_reopen_after_expiry_keeps_old_expires_at(make_actor, db):
    """Reopening (PATCH status=active) does not refresh expires_at: listing stays hidden."""
    owner = await make_actor("buyer")
    viewer = await make_actor("seller")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    await _expire(db, rid)
    r = await owner.patch(f"/rfqs/{rid}", json={"status": "active"})
    if r.status_code in (409, 422):
        return  # reopen refused: acceptable
    ids = [i["id"] for i in (await viewer.get("/marketplace/catalog")).json()["items"]]
    assert rid in ids, "PATCH returned 200 'active' but listing is invisible (stale expires_at)"


# ---------------------------------------------------------------------------
# RFQ validation
# ---------------------------------------------------------------------------


async def test_whitespace_only_title_rejected(make_actor):
    owner = await make_actor("buyer")
    status, _ = await safe(owner.post("/rfqs", json=rfq_body(title="     ")))
    assert status == 422, status


async def test_title_over_300_rejected(make_actor):
    owner = await make_actor("buyer")
    status, _ = await safe(owner.post("/rfqs", json=rfq_body(title="x" * 10_000)))
    assert status == 422


@pytest.mark.parametrize("q", [0, -5])
async def test_nonpositive_quantity_rejected(make_actor, q):
    owner = await make_actor("buyer")
    status, _ = await safe(owner.post("/rfqs", json=rfq_body(quantity=(q, "pcs"))))
    assert status == 422


async def test_huge_quantity_is_422_not_500(make_actor):
    """quantity 1e20 overflows Numeric(18,4) -> DB error -> 500 instead of 422."""
    owner = await make_actor("buyer")
    status, data = await safe(owner.post("/rfqs", json=rfq_body(quantity=(1e20, "pcs"))))
    assert status == 422, (status, data)


async def test_huge_price_is_422_not_500(make_actor):
    owner = await make_actor("buyer")
    status, data = await safe(owner.post("/rfqs", json=rfq_body(price=(1e20, "INR", "pcs"))))
    assert status == 422, (status, data)


@pytest.mark.parametrize("cur", ["XYZ", "FOOBAR", "12"])
async def test_unsupported_currency_rejected(make_actor, cur):
    """Money.currency accepts anything and silently truncates to 3 upper chars."""
    owner = await make_actor("buyer")
    status, data = await safe(owner.post("/rfqs", json=rfq_body(price=(10, cur, "pcs"))))
    stored = data.get("price_target", {}).get("currency") if isinstance(data, dict) else None
    assert status == 422, (status, stored)


async def test_description_has_length_cap(make_actor):
    """description is an unbounded Text field on the API (200k chars accepted)."""
    owner = await make_actor("buyer")
    body = rfq_body()
    body["description"] = "spam " * 40_000
    status, _ = await safe(owner.post("/rfqs", json=body))
    assert status == 422, status


async def test_product_details_payload_is_bounded(make_actor):
    """product_details accepts an arbitrary dict (5000 keys) -> JSONB/search bloat."""
    owner = await make_actor("buyer")
    body = rfq_body(attributes={f"k{i}": "v" * 50 for i in range(5000)})
    status, _ = await safe(owner.post("/rfqs", json=body))
    assert status == 422, status


async def test_unicode_and_html_roundtrip_unchanged(make_actor):
    """Informational: unicode/emoji + HTML are stored and returned verbatim (frontend escapes)."""
    owner = await make_actor("buyer")
    title = "कपास 🧵 <script>alert(1)</script> Ünïcødé"
    r = await owner.post("/rfqs", json=rfq_body(title=title))
    assert r.status_code == 201
    assert r.json()["title"] == title


async def test_location_with_nul_byte_is_422_not_500(make_actor):
    """A NUL byte in text is rejected by Postgres -> should be 422."""
    owner = await make_actor("buyer")
    status, data = await safe(owner.post("/rfqs", json=rfq_body(title="bad\x00title")))
    assert status in (201, 422), (status, data)


# ---------------------------------------------------------------------------
# Catalog search robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    ["(", "[", "\\", "*", "a(b", "c++", "?", "{1,", "'; DROP TABLE rfqs;--", "%", "_",
     ".*", "\\m", "^$", "|", "   ", "🧵", "a" * 3000],
)
async def test_catalog_search_special_chars_no_500(make_actor, q):
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    await seller.post("/rfqs", json=rfq_body(role="seller", title="C++ (legacy) [x] cables"))
    status, data = await safe(viewer.get("/marketplace/catalog", params={"q": q}))
    assert status == 200, (status, data)
    status2, data2 = await safe(viewer.get("/marketplace/categories", params={"q": q}))
    assert status2 == 200, (status2, data2)


async def test_catalog_search_nul_byte_no_500(make_actor):
    viewer = await make_actor("buyer")
    status, data = await safe(viewer.get("/marketplace/catalog", params={"q": "cab\x00le"}))
    assert status in (200, 422), (status, data)


async def test_catalog_regex_metachar_is_literal(make_actor):
    """q='.*' must not match every listing (pattern must be escaped)."""
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    await seller.post("/rfqs", json=rfq_body(role="seller", title="Cotton yarn"))
    data = (await viewer.get("/marketplace/catalog", params={"q": ".*"})).json()
    assert data["total"] == 0


async def test_catalog_search_long_query_bounded(make_actor):
    """A 2000-word query builds 2000 AND-ed OR groups of ~* scans; should be capped."""
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    for i in range(5):
        await seller.post("/rfqs", json=rfq_body(role="seller", title=f"widget {i}"))
    q = " ".join(f"w{i}x" for i in range(2000))
    t0 = time.perf_counter()
    status, _ = await safe(viewer.get("/marketplace/catalog", params={"q": q}))
    elapsed = time.perf_counter() - t0
    assert status == 422 or elapsed < 1.0, (status, round(elapsed, 2))


@pytest.mark.parametrize(
    "params", [{"limit": 100000}, {"limit": 0}, {"offset": -1}, {"limit": -3}]
)
async def test_catalog_pagination_bounds(make_actor, params):
    viewer = await make_actor("buyer")
    r = await viewer.get("/marketplace/catalog", params=params)
    assert r.status_code == 422


async def test_catalog_min_price_infinity_no_500(make_actor):
    viewer = await make_actor("buyer")
    status, data = await safe(viewer.get("/marketplace/catalog", params={"min_price": "inf"}))
    assert status in (200, 422), (status, data)


async def test_categories_endpoint_validates_price(make_actor):
    """/marketplace/categories lacks the ge=0 constraint the catalog has."""
    viewer = await make_actor("buyer")
    r = await viewer.get("/marketplace/categories", params={"min_price": -100})
    assert r.status_code == 422, r.status_code


async def test_category_filter_is_not_wildcard_injectable(make_actor):
    """category='%' is passed into ILIKE unescaped and matches every listing."""
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    await seller.post("/rfqs", json=rfq_body(role="seller", category="Electronics"))
    await seller.post("/rfqs", json=rfq_body(role="seller", category="Textiles"))
    data = (await viewer.get("/marketplace/catalog", params={"category": "%"})).json()
    assert data["total"] == 0, data["total"]


async def test_catalog_hides_expired_closed_draft(make_actor, db):
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    live = (await seller.post("/rfqs", json=rfq_body(role="seller"))).json()["id"]
    draft = (await seller.post("/rfqs", json=rfq_body(role="seller", status="draft"))).json()["id"]
    closed = (await seller.post("/rfqs", json=rfq_body(role="seller"))).json()["id"]
    await seller.post(f"/rfqs/{closed}/close")
    expired = (await seller.post("/rfqs", json=rfq_body(role="seller"))).json()["id"]
    await _expire(db, expired)
    ids = {i["id"] for i in (await viewer.get("/marketplace/catalog", params={"limit": 100})).json()["items"]}
    assert live in ids and not ({draft, closed, expired} & ids)


async def test_radius_filter_total_and_pagination_consistent(make_actor):
    """radius_km is applied in Python *after* LIMIT/OFFSET and COUNT.

    3 listings (1 in Indore, 2 in Delhi); radius 50 km around Indore, limit=1
    sorted newest-first -> page 1 is empty and total says 3.
    """
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    await seller.post("/rfqs", json=rfq_body(role="seller", title="near", city="Indore"))
    for t in ("far1", "far2"):
        await seller.post(
            "/rfqs",
            json=rfq_body(role="seller", title=t, city="Delhi", state="Delhi",
                          latitude=28.61, longitude=77.21),
        )
    params = {"lat": 22.7196, "lon": 75.8577, "radius_km": 50, "limit": 1}
    data = (await viewer.get("/marketplace/catalog", params=params)).json()
    assert data["total"] == 1 and len(data["items"]) == 1, (data["total"], len(data["items"]))


async def test_categories_are_normalized(make_actor):
    """services/categories.normalize_category is never called: 'electronic' and
    'Electronics' show up as two separate facets, and filtering by 'Electronics'
    misses the 'electronic' listing."""
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    await seller.post("/rfqs", json=rfq_body(role="seller", category="Electronics"))
    await seller.post("/rfqs", json=rfq_body(role="seller", category="electronic"))
    cats = [c["category"] for c in (await viewer.get("/marketplace/categories")).json()]
    filt = (await viewer.get("/marketplace/catalog", params={"category": "Electronics"})).json()
    assert cats == ["Electronics"] and filt["total"] == 2, (cats, filt["total"])


# ---------------------------------------------------------------------------
# Saved RFQs
# ---------------------------------------------------------------------------


async def test_cannot_bookmark_someone_elses_draft(make_actor):
    """save_rfq only checks existence -> existence oracle for private drafts."""
    owner = await make_actor("seller")
    other = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body(role="seller", status="draft"))).json()["id"]
    r = await other.post(f"/rfqs/{rid}/save")
    assert r.status_code == 404, r.status_code


async def test_save_nonexistent_is_404_and_idempotent(make_actor):
    viewer = await make_actor("buyer")
    assert (await viewer.post(f"/rfqs/{uuid.uuid4()}/save")).status_code == 404
    seller = await make_actor("seller")
    rid = (await seller.post("/rfqs", json=rfq_body(role="seller"))).json()["id"]
    assert (await viewer.post(f"/rfqs/{rid}/save")).status_code == 200
    assert (await viewer.post(f"/rfqs/{rid}/save")).status_code == 200
    assert (await viewer.get("/rfqs/saved/ids")).json() == [rid]


async def test_concurrent_save_no_500(make_actor):
    """Two simultaneous saves race past the SELECT and hit the unique constraint."""
    viewer = await make_actor("buyer")
    seller = await make_actor("seller")
    rid = (await seller.post("/rfqs", json=rfq_body(role="seller"))).json()["id"]
    results = await asyncio.gather(*[safe(viewer.post(f"/rfqs/{rid}/save")) for _ in range(6)])
    assert all(s == 200 for s, _ in results), [s for s, _ in results]


async def test_saved_list_count_matches_ids(make_actor):
    """/rfqs/saved hides closed bookmarks but /rfqs/saved/ids still returns them."""
    viewer = await make_actor("buyer")
    seller = await make_actor("seller")
    rid = (await seller.post("/rfqs", json=rfq_body(role="seller"))).json()["id"]
    await viewer.post(f"/rfqs/{rid}/save")
    await seller.post(f"/rfqs/{rid}/close")
    ids = (await viewer.get("/rfqs/saved/ids")).json()
    listing = (await viewer.get("/rfqs/saved")).json()
    assert len(ids) == listing["total"], (ids, listing["total"])


# ---------------------------------------------------------------------------
# Quotations
# ---------------------------------------------------------------------------


async def test_cannot_quote_on_closed_rfq(make_actor):
    buyer, seller, conn_id, listing = await _pair(make_actor)
    await seller.post(f"/rfqs/{listing['id']}/close")
    r = await seller.post(f"/connections/{conn_id}/quotes", json=QUOTE)
    assert r.status_code == 409, r.status_code


async def test_only_one_accepted_quote_per_connection(make_actor):
    """After v1 is accepted (PO issued) a v2 can be created and accepted too."""
    buyer, seller, conn_id, _ = await _pair(make_actor)
    q1 = (await seller.post(f"/connections/{conn_id}/quotes", json=QUOTE)).json()
    assert (await buyer.post(f"/connections/{conn_id}/quotes/{q1['id']}/accept")).status_code == 200
    r2 = await seller.post(f"/connections/{conn_id}/quotes", json={**QUOTE, "unit_price": 99})
    if r2.status_code == 201:
        a2 = await buyer.post(f"/connections/{conn_id}/quotes/{r2.json()['id']}/accept")
        quotes = (await buyer.get(f"/connections/{conn_id}/quotes")).json()
        accepted = [q for q in quotes if q["status"] == "accepted"]
        assert len(accepted) <= 1, f"{len(accepted)} accepted POs on one connection (a2={a2.status_code})"


async def test_quote_total_overflow_is_422(make_actor):
    """unit_price 1e10 * qty 1e10 overflows Numeric(18,4) total -> 500."""
    buyer, seller, conn_id, _ = await _pair(make_actor)
    status, data = await safe(
        seller.post(f"/connections/{conn_id}/quotes",
                    json={**QUOTE, "unit_price": 1e10, "quantity": 1e10})
    )
    assert status == 422, (status, data)


async def test_quote_unsupported_currency_rejected(make_actor):
    buyer, seller, conn_id, _ = await _pair(make_actor)
    r = await seller.post(f"/connections/{conn_id}/quotes", json={**QUOTE, "currency": "ZZZ"})
    assert r.status_code == 422, (r.status_code, r.json().get("currency"))


@pytest.mark.parametrize("field,val", [("unit_price", 0), ("unit_price", -1), ("quantity", 0),
                                       ("valid_days", 0), ("valid_days", 1000),
                                       ("lead_time_days", 0)])
async def test_quote_field_bounds(make_actor, field, val):
    buyer, seller, conn_id, _ = await _pair(make_actor)
    r = await seller.post(f"/connections/{conn_id}/quotes", json={**QUOTE, field: val})
    assert r.status_code == 422


async def test_stranger_cannot_accept_quote(make_actor):
    buyer, seller, conn_id, _ = await _pair(make_actor)
    stranger = await make_actor("buyer")
    q = (await seller.post(f"/connections/{conn_id}/quotes", json=QUOTE)).json()
    r = await stranger.post(f"/connections/{conn_id}/quotes/{q['id']}/accept")
    assert r.status_code in (403, 404)


async def test_po_pdf_with_markup_in_product_details_no_500(make_actor):
    """product_details values go into a ReportLab Paragraph unescaped."""
    buyer, seller, conn_id, _ = await _pair(
        make_actor, attributes={"grade": "<b unclosed & <script>", "size": "5<6"}
    )
    q = (await seller.post(f"/connections/{conn_id}/quotes", json=QUOTE)).json()
    await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/accept")
    status, data = await safe(buyer.get(f"/connections/{conn_id}/quotes/{q['id']}/po-pdf"))
    assert status == 200, (status, str(data)[:200])


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


async def test_review_requires_completed_delivery_and_no_duplicates(make_actor):
    buyer, seller, conn_id, _ = await _pair(make_actor)
    q = (await seller.post(f"/connections/{conn_id}/quotes", json=QUOTE)).json()
    # pending quote -> cannot rate
    assert (await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/rate",
                             json={"rating": 5})).status_code == 400
    await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/accept")
    # buyer cannot mark dispatched; seller cannot accept delivery
    assert (await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/dispatch")).status_code == 400
    await seller.post(f"/connections/{conn_id}/quotes/{q['id']}/dispatch")
    await seller.post(f"/connections/{conn_id}/quotes/{q['id']}/deliver")
    assert (await seller.post(f"/connections/{conn_id}/quotes/{q['id']}/accept-delivery")).status_code == 400
    await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/accept-delivery")
    # seller cannot rate first
    assert (await seller.post(f"/connections/{conn_id}/quotes/{q['id']}/rate",
                              json={"rating": 1})).status_code == 400
    assert (await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/rate",
                             json={"rating": 5})).status_code == 201
    assert (await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/rate",
                             json={"rating": 5})).status_code == 400
    stranger = await make_actor("buyer")
    assert (await stranger.post(f"/connections/{conn_id}/quotes/{q['id']}/rate",
                                json={"rating": 1})).status_code == 400
    for bad in (0, 6, -1):
        assert (await seller.post(f"/connections/{conn_id}/quotes/{q['id']}/rate",
                                  json={"rating": bad})).status_code == 422


async def test_trust_score_cannot_be_farmed_on_one_connection(make_actor, db):
    """Two colluding accounts loop tiny 1-rupee deals on ONE connection; every
    5-star review adds +5 trust with no cap per counterparty. 3 loops -> +15,
    enough to push towards the >=60 'verified_only' threshold."""
    buyer, seller, conn_id, _ = await _pair(make_actor)
    prof = await db.scalar(select(UserProfile).where(UserProfile.user_id == uuid.UUID(seller.id)))
    start = prof.trust_score
    for _ in range(3):
        qid = await _full_deal(buyer, seller, conn_id)
        r = await buyer.post(f"/connections/{conn_id}/quotes/{qid}/rate", json={"rating": 5})
        assert r.status_code == 201
    db.expire_all()
    prof = await db.scalar(select(UserProfile).where(UserProfile.user_id == uuid.UUID(seller.id)))
    assert prof.trust_score - start <= 5, (start, prof.trust_score, prof.total_reviews)


async def test_quote_reviews_require_participation(make_actor):
    """GET .../quotes/{id}/reviews has no auth check and ignores connection_id."""
    buyer, seller, conn_id, _ = await _pair(make_actor)
    qid = await _full_deal(buyer, seller, conn_id)
    await buyer.post(f"/connections/{conn_id}/quotes/{qid}/rate",
                     json={"rating": 2, "comment": "private complaint"})
    stranger = await make_actor("buyer")
    r = await stranger.get(f"/connections/{uuid.uuid4()}/quotes/{qid}/reviews")
    assert r.status_code in (403, 404) or r.json() == [], (r.status_code, r.json())


async def test_user_reviews_404_for_unknown_user(make_actor):
    """/users/{random}/reviews returns 200 with zeroed stats for nonexistent users."""
    viewer = await make_actor("buyer")
    r = await viewer.get(f"/users/{uuid.uuid4()}/reviews")
    assert r.status_code == 404, r.status_code


async def test_user_reviews_requires_auth(client):
    """/users/{id}/reviews is served without authentication (no CurrentUser dep)."""
    r = await client.get(f"/users/{uuid.uuid4()}/reviews")
    assert r.status_code == 401, r.status_code

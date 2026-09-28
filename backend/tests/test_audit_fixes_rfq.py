"""Regression tests for the RFQ / marketplace / saved RFQ / review audit fixes.

Each test pins a defect from AUDIT_REPORT.md sections 2 and 3.2 (probes in
audit/probes/test_zz_audit_rfq_marketplace.py) so it cannot come back.
"""

import asyncio
import json
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from models.enums import RFQStatus
from models.rfq import RFQ
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


async def _expire(db, rid: str, *, deadline_too: bool = True) -> None:
    sql = "UPDATE rfqs SET expires_at = now() - interval '1 day'"
    if deadline_too:
        sql += ", deadline_at = now() - interval '1 day'"
    await db.execute(text(sql + " WHERE id = :i"), {"i": rid})
    await db.commit()


QUOTE = {"unit_price": 10, "currency": "INR", "quantity": 100, "quantity_unit": "pcs"}


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


async def _full_deal(buyer, seller, conn_id) -> str:
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
# 1. Catalog detail visibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_kind", ["draft", "closed"])
async def test_catalog_item_hides_other_users_draft_and_closed(make_actor, status_kind):
    owner = await make_actor("seller")
    other = await make_actor("buyer")
    rfq = (
        await owner.post("/rfqs", json=rfq_body(role="seller", title="SECRET draft", status="draft"))
    ).json()
    if status_kind == "closed":
        await owner.post(f"/rfqs/{rfq['id']}/publish")
        await owner.post(f"/rfqs/{rfq['id']}/close")
    assert (await other.get(f"/marketplace/catalog/{rfq['id']}")).status_code == 404
    # The owner can still open their own listing.
    assert (await owner.get(f"/marketplace/catalog/{rfq['id']}")).status_code == 200


async def test_catalog_item_hides_expired(make_actor, db):
    owner = await make_actor("seller")
    other = await make_actor("buyer")
    rfq = (await owner.post("/rfqs", json=rfq_body(role="seller"))).json()
    assert (await other.get(f"/marketplace/catalog/{rfq['id']}")).status_code == 200
    await _expire(db, rfq["id"])
    assert (await other.get(f"/marketplace/catalog/{rfq['id']}")).status_code == 404


async def test_catalog_item_visible_to_connected_party_after_close(make_actor):
    buyer, seller, _conn_id, listing = await _pair(make_actor)
    await seller.post(f"/rfqs/{listing['id']}/close")
    assert (await buyer.get(f"/marketplace/catalog/{listing['id']}")).status_code == 200
    stranger = await make_actor("buyer")
    assert (await stranger.get(f"/marketplace/catalog/{listing['id']}")).status_code == 404


# ---------------------------------------------------------------------------
# 2. Deadline schema
# ---------------------------------------------------------------------------


async def test_absolute_deadline_date_is_accepted(make_actor):
    owner = await make_actor("buyer")
    body = rfq_body()
    body["deadline"] = {"date": "2031-03-15"}
    r = await owner.post("/rfqs", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["deadline"]["date"].startswith("2031-03-15")
    assert r.json()["expires_at"].startswith("2031-03-15")


async def test_deadline_today_is_accepted(make_actor):
    owner = await make_actor("buyer")
    body = rfq_body()
    body["deadline"] = {"date": datetime.now(UTC).date().isoformat()}
    assert (await owner.post("/rfqs", json=body)).status_code == 201


@pytest.mark.parametrize(
    "deadline", [{"date": "2000-01-01"}, {"in_days": 0}, {"in_days": -1}, {"date": "not-a-date"}]
)
async def test_deadline_in_past_or_zero_days_rejected(make_actor, deadline):
    owner = await make_actor("buyer")
    body = rfq_body()
    body["deadline"] = deadline
    status, _ = await safe(owner.post("/rfqs", json=body))
    assert status == 422


def test_ai_payload_builder_keeps_absolute_deadline():
    """rfq_tool_service.build_rfq_create_payload used to drop every date."""
    from services.rfq_tool_service import build_rfq_create_payload

    future = (date.today() + timedelta(days=30)).isoformat()
    payload = build_rfq_create_payload(
        {
            "role": "buyer",
            "category": "Agriculture",
            "product_details": {"name": "basmati rice"},
            "deadline": {"date": future},
        },
        status="active",
    )
    assert payload.deadline is not None and payload.deadline.date.isoformat() == future


# ---------------------------------------------------------------------------
# 3. State machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["active", "expired", "draft", "closed"])
async def test_patch_rejects_status(make_actor, target):
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    r = await owner.patch(f"/rfqs/{rid}", json={"status": target})
    assert r.status_code == 422
    assert (await owner.get(f"/rfqs/{rid}")).json()["status"] == "active"


async def test_closed_rfq_is_terminal_and_immutable(make_actor):
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    assert (await owner.post(f"/rfqs/{rid}/close")).status_code == 200
    assert (await owner.post(f"/rfqs/{rid}/publish")).status_code == 409
    r = await owner.patch(f"/rfqs/{rid}", json={"title": "edited after close"})
    assert r.status_code == 409
    # Closing again is an idempotent no-op.
    again = await owner.post(f"/rfqs/{rid}/close")
    assert again.status_code == 200 and again.json()["status"] == "closed"
    assert (await owner.get(f"/rfqs/{rid}")).json()["title"] != "edited after close"


async def test_draft_can_be_closed(make_actor):
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body(status="draft"))).json()["id"]
    r = await owner.post(f"/rfqs/{rid}/close")
    assert r.status_code == 200 and r.json()["status"] == "closed"


async def test_publish_refuses_draft_whose_deadline_passed(make_actor, db):
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body(status="draft"))).json()["id"]
    await _expire(db, rid)
    r = await owner.post(f"/rfqs/{rid}/publish")
    assert r.status_code == 409
    assert (await owner.get(f"/rfqs/{rid}")).json()["status"] == "draft"


async def test_publish_recomputes_stale_expiry(make_actor, db):
    """A draft without a deadline whose TTL lapsed gets a fresh TTL on publish."""
    owner = await make_actor("buyer")
    viewer = await make_actor("seller")
    rid = (
        await owner.post("/rfqs", json=rfq_body(status="draft", deadline_days=None))
    ).json()["id"]
    await _expire(db, rid, deadline_too=False)
    r = await owner.post(f"/rfqs/{rid}/publish")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    assert datetime.fromisoformat(r.json()["expires_at"]) > datetime.now(UTC) + timedelta(days=29)
    ids = [i["id"] for i in (await viewer.get("/marketplace/catalog")).json()["items"]]
    assert rid in ids


async def test_internal_status_update_still_closes(make_actor, db):
    """The AI chat's close tool calls update_rfq(RFQUpdate(status=CLOSED))."""
    from schemas.rfq import RFQUpdate
    from services import rfq_service
    from services.rfq_service import RFQStateError

    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    rfq = await db.get(RFQ, uuid.UUID(rid))
    closed = await rfq_service.update_rfq(db, rfq, RFQUpdate(status=RFQStatus.CLOSED))
    assert closed.status == RFQStatus.CLOSED
    with pytest.raises(RFQStateError):
        await rfq_service.update_rfq(db, closed, RFQUpdate(status=RFQStatus.ACTIVE))


# ---------------------------------------------------------------------------
# 4. Expiry
# ---------------------------------------------------------------------------


async def test_owner_sees_expired_status_on_get_and_list(make_actor, db):
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    await _expire(db, rid)
    assert (await owner.get(f"/rfqs/{rid}")).json()["status"] == "expired"
    listed = (await owner.get("/rfqs")).json()["items"]
    assert [i["status"] for i in listed if i["id"] == rid] == ["expired"]
    expired_only = (await owner.get("/rfqs", params={"status": "expired"})).json()
    assert [i["id"] for i in expired_only["items"]] == [rid]
    # An expired RFQ can no longer be edited or republished.
    assert (await owner.patch(f"/rfqs/{rid}", json={"title": "x"})).status_code == 409
    assert (await owner.post(f"/rfqs/{rid}/publish")).status_code == 409


async def test_expire_due_rfqs_is_idempotent(make_actor, db):
    from services import rfq_service

    owner = await make_actor("buyer")
    live = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    due = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    draft = (await owner.post("/rfqs", json=rfq_body(status="draft"))).json()["id"]
    await _expire(db, due)
    await _expire(db, draft)
    assert await rfq_service.expire_due_rfqs(db) == 1
    assert await rfq_service.expire_due_rfqs(db) == 0
    db.expire_all()
    statuses = {
        str(r.id): r.status
        for r in (await db.scalars(select(RFQ).where(RFQ.id.in_([live, due, draft])))).all()
    }
    assert statuses == {live: RFQStatus.ACTIVE, due: RFQStatus.EXPIRED, draft: RFQStatus.DRAFT}


# ---------------------------------------------------------------------------
# 5. Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", ["     ", "\t\n", "x" * 301])
async def test_bad_title_rejected(make_actor, title):
    owner = await make_actor("buyer")
    status, _ = await safe(owner.post("/rfqs", json=rfq_body(title=title)))
    assert status == 422


async def test_title_is_trimmed(make_actor):
    owner = await make_actor("buyer")
    r = await owner.post("/rfqs", json=rfq_body(title="  Cotton yarn  "))
    assert r.status_code == 201 and r.json()["title"] == "Cotton yarn"


async def test_patch_whitespace_title_rejected(make_actor):
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    assert (await owner.patch(f"/rfqs/{rid}", json={"title": "   "})).status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": (1e20, "pcs")},
        {"quantity": (1e14, "pcs")},
        {"price": (1e20, "INR", "pcs")},
        {"price": (-1, "INR", "pcs")},
    ],
)
async def test_out_of_range_numbers_are_422_not_500(make_actor, overrides):
    owner = await make_actor("buyer")
    status, data = await safe(owner.post("/rfqs", json=rfq_body(**overrides)))
    assert status == 422, (status, data)


@pytest.mark.parametrize("raw", ["Infinity", "NaN", "-Infinity"])
async def test_non_finite_numbers_rejected(make_actor, raw):
    owner = await make_actor("buyer")
    body = json.dumps(rfq_body()).replace('"value": 6000', f'"value": {raw}')
    assert raw in body
    status, _ = await safe(
        owner.post("/rfqs", content=body, headers={"Content-Type": "application/json"})
    )
    assert status == 422


async def test_max_allowed_quantity_is_stored(make_actor):
    owner = await make_actor("buyer")
    r = await owner.post("/rfqs", json=rfq_body(quantity=(1e13, "pcs")))
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("cur", ["XYZ", "FOOBAR", "12", "INRX"])
async def test_unsupported_currency_rejected(make_actor, cur):
    owner = await make_actor("buyer")
    status, data = await safe(owner.post("/rfqs", json=rfq_body(price=(10, cur, "pcs"))))
    assert status == 422, (status, data)


@pytest.mark.parametrize("cur,expected", [("usd", "USD"), ("rupees", "INR"), ("EUR", "EUR")])
async def test_supported_currency_normalised(make_actor, cur, expected):
    owner = await make_actor("buyer")
    r = await owner.post("/rfqs", json=rfq_body(price=(10, cur, "pcs")))
    assert r.status_code == 201, r.text
    assert r.json()["price_target"]["currency"] == expected


async def test_description_cap(make_actor):
    owner = await make_actor("buyer")
    body = rfq_body()
    body["description"] = "x" * 5001
    assert (await owner.post("/rfqs", json=body)).status_code == 422
    body["description"] = "x" * 5000
    assert (await owner.post("/rfqs", json=body)).status_code == 201


@pytest.mark.parametrize(
    "attributes",
    [
        {f"k{i}": "v" for i in range(60)},  # too many keys
        {"notes": "v" * 501},  # value too long
        {"nested": {"deep": ["v" * 501]}},  # nested value too long
        {f"k{i}": "v" * 400 for i in range(30)},  # > 10 kB JSON
        {"k" * 101: "v"},  # key too long
    ],
)
async def test_product_details_bounded(make_actor, attributes):
    owner = await make_actor("buyer")
    status, _ = await safe(owner.post("/rfqs", json=rfq_body(attributes=attributes)))
    assert status == 422


async def test_product_details_bounded_on_patch(make_actor):
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    r = await owner.patch(
        f"/rfqs/{rid}", json={"product_details": {f"k{i}": "v" for i in range(60)}}
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(title="bad\x00title"),
        lambda b: b.update(description="bad\x00desc"),
        lambda b: b.update(category="Elec\x00tronics"),
        lambda b: b["location"].update(city="Ind\x00ore"),
        lambda b: b["product_details"].update(grade="A\x00"),
        lambda b: b["quantity"].update(unit="p\x00cs"),
        lambda b: b["deadline"].update(raw="soon\x00"),
    ],
)
async def test_nul_bytes_rejected_with_422(make_actor, mutate):
    owner = await make_actor("buyer")
    body = rfq_body()
    mutate(body)
    status, data = await safe(owner.post("/rfqs", json=body))
    assert status == 422, (status, data)


async def test_conftest_default_body_still_valid(make_actor):
    """Other suites build RFQs from rfq_body(); its defaults must stay valid."""
    owner = await make_actor("both")
    assert (await owner.post("/rfqs", json=rfq_body())).status_code == 201
    assert (await owner.post("/rfqs", json=rfq_body("seller"))).status_code == 201


# ---------------------------------------------------------------------------
# 5b/7. Catalog query hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["/marketplace/catalog", "/marketplace/categories"])
@pytest.mark.parametrize(
    "params",
    [
        {"min_price": "inf"},
        {"max_price": "nan"},
        {"min_price": -100},
        {"max_price": -1},
        {"min_price": 1e20},
    ],
)
async def test_price_params_validated(make_actor, endpoint, params):
    viewer = await make_actor("buyer")
    status, _ = await safe(viewer.get(endpoint, params=params))
    assert status == 422


async def test_nul_byte_in_query_params_no_500(make_actor):
    viewer = await make_actor("buyer")
    for key in ("q", "category", "city", "country"):
        for endpoint in ("/marketplace/catalog", "/marketplace/categories"):
            if endpoint.endswith("categories") and key == "category":
                continue
            status, data = await safe(viewer.get(endpoint, params={key: "cab\x00le"}))
            assert status == 200, (endpoint, key, status, data)


async def test_long_query_is_capped_and_fast(make_actor):
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    for i in range(3):
        await seller.post("/rfqs", json=rfq_body(role="seller", title=f"widget {i}"))
    q = " ".join(f"w{i}x" for i in range(2000))
    t0 = time.perf_counter()
    r = await viewer.get("/marketplace/catalog", params={"q": q})
    assert r.status_code == 200
    assert time.perf_counter() - t0 < 1.0
    # 'a'*3000 must not 500 either.
    assert (await viewer.get("/marketplace/catalog", params={"q": "a" * 3000})).status_code == 200


def test_search_conditions_capped():
    from services.catalog_service import MAX_QUERY_TOKENS, build_search_conditions

    conds = build_search_conditions(" ".join(f"tok{i}" for i in range(50)))
    assert len(conds) == MAX_QUERY_TOKENS


@pytest.mark.parametrize("param", ["category", "city", "country"])
@pytest.mark.parametrize("wildcard", ["%", "_", "%%", "\\"])
async def test_like_wildcards_are_literal(make_actor, param, wildcard):
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    await seller.post("/rfqs", json=rfq_body(role="seller", category="Electronics"))
    await seller.post("/rfqs", json=rfq_body(role="seller", category="Textiles"))
    data = (await viewer.get("/marketplace/catalog", params={param: wildcard})).json()
    assert data["total"] == 0, data["total"]


async def test_substring_category_and_city_filters_still_work(make_actor):
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    await seller.post("/rfqs", json=rfq_body(role="seller", category="Electronics"))
    assert (await viewer.get("/marketplace/catalog", params={"category": "electro"})).json()["total"] == 1
    assert (await viewer.get("/marketplace/catalog", params={"city": "ndor"})).json()["total"] == 1


# ---------------------------------------------------------------------------
# 6. Radius filter in SQL
# ---------------------------------------------------------------------------


INDORE = {"lat": 22.7196, "lon": 75.8577}


async def test_radius_total_and_pages_consistent(make_actor):
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    near_ids = []
    for t in ("near1", "near2", "near3"):
        near_ids.append(
            (await seller.post("/rfqs", json=rfq_body(role="seller", title=t, city="Indore"))).json()["id"]
        )
    for t in ("far1", "far2"):
        await seller.post(
            "/rfqs",
            json=rfq_body(role="seller", title=t, city="Delhi", state="Delhi",
                          latitude=28.61, longitude=77.21),
        )
    seen = []
    for offset in range(4):
        page = (
            await viewer.get(
                "/marketplace/catalog",
                params={**INDORE, "radius_km": 50, "limit": 1, "offset": offset},
            )
        ).json()
        assert page["total"] == 3
        seen += [i["id"] for i in page["items"]]
        for item in page["items"]:
            assert item["distance_km"] is not None and item["distance_km"] <= 50
    assert sorted(seen) == sorted(near_ids)
    wide = (await viewer.get("/marketplace/catalog", params={**INDORE, "radius_km": 1000})).json()
    assert wide["total"] == 5


async def test_radius_uses_gazetteer_region_fallback(make_actor):
    """A listing whose 'city' is a region (no coordinates stored) is placed by the
    gazetteer, as the displayed map pin is, and filtered consistently."""
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    created = (
        await seller.post(
            "/rfqs",
            json=rfq_body(role="seller", title="Punjab wheat", city="Punjab", state=None,
                          country=None, latitude=None, longitude=None),
        )
    ).json()
    assert created["location"]["latitude"] is None
    punjab = {"lat": 31.1471, "lon": 75.3412}
    hit = (await viewer.get("/marketplace/catalog", params={**punjab, "radius_km": 20})).json()
    assert [i["id"] for i in hit["items"]] == [created["id"]] and hit["total"] == 1
    miss = (await viewer.get("/marketplace/catalog", params={**INDORE, "radius_km": 50})).json()
    assert miss["total"] == 0


async def test_radius_uses_owner_profile_fallback(make_actor, db):
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    created = (
        await seller.post(
            "/rfqs",
            json=rfq_body(role="seller", title="Unknown town goods", city="Nowhereville",
                          state=None, country=None, latitude=None, longitude=None),
        )
    ).json()
    await db.execute(
        text("UPDATE user_profiles SET latitude = 22.72, longitude = 75.86 WHERE user_id = :u"),
        {"u": seller.id},
    )
    await db.commit()
    hit = (await viewer.get("/marketplace/catalog", params={**INDORE, "radius_km": 10})).json()
    assert hit["total"] == 1 and hit["items"][0]["id"] == created["id"]
    far = (await viewer.get("/marketplace/catalog", params={"lat": 28.61, "lon": 77.21, "radius_km": 10})).json()
    assert far["total"] == 0


async def test_radius_without_reference_point_returns_nothing(make_actor):
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    await seller.post("/rfqs", json=rfq_body(role="seller"))
    data = (await viewer.get("/marketplace/catalog", params={"radius_km": 50})).json()
    assert data["total"] == 0 and data["items"] == []


# ---------------------------------------------------------------------------
# 8. Category normalization
# ---------------------------------------------------------------------------


async def test_categories_are_normalized(make_actor):
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    a = (await seller.post("/rfqs", json=rfq_body(role="seller", category="Electronics"))).json()
    b = (await seller.post("/rfqs", json=rfq_body(role="seller", category="electronic"))).json()
    assert a["category"] == b["category"] == "Electronics"
    cats = [c["category"] for c in (await viewer.get("/marketplace/categories")).json()]
    assert cats == ["Electronics"]
    for filt in ("Electronics", "electronic", "ELECTRONICS"):
        data = (await viewer.get("/marketplace/catalog", params={"category": filt})).json()
        assert data["total"] == 2, (filt, data["total"])


async def test_legacy_unnormalized_rows_merge_in_facets_and_filter(make_actor, db):
    seller = await make_actor("seller")
    viewer = await make_actor("buyer")
    rid = (await seller.post("/rfqs", json=rfq_body(role="seller", category="Electronics"))).json()["id"]
    await seller.post("/rfqs", json=rfq_body(role="seller", category="Electronics"))
    # Simulate a row written before normalization-on-write.
    await db.execute(text("UPDATE rfqs SET category = 'electronic' WHERE id = :i"), {"i": rid})
    await db.commit()
    facets = (await viewer.get("/marketplace/categories")).json()
    assert [(c["category"], c["total_count"]) for c in facets] == [("Electronics", 2)]
    assert (await viewer.get("/marketplace/catalog", params={"category": "Electronics"})).json()["total"] == 2


async def test_patch_normalizes_category(make_actor):
    owner = await make_actor("buyer")
    rid = (await owner.post("/rfqs", json=rfq_body())).json()["id"]
    r = await owner.patch(f"/rfqs/{rid}", json={"category": "education"})
    assert r.status_code == 200 and r.json()["category"] == "Learning"


def test_learning_is_canonical():
    from services.categories import CANONICAL_CATEGORIES, normalize_category

    assert "Learning" in CANONICAL_CATEGORIES
    assert normalize_category("education") == "Learning"
    assert normalize_category("learning") == "Learning"


# ---------------------------------------------------------------------------
# 9. Saved RFQs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["draft", "closed", "expired"])
async def test_cannot_bookmark_non_public_rfq(make_actor, db, state):
    owner = await make_actor("seller")
    other = await make_actor("buyer")
    rid = (
        await owner.post(
            "/rfqs", json=rfq_body(role="seller", status="draft" if state == "draft" else "active")
        )
    ).json()["id"]
    if state == "closed":
        await owner.post(f"/rfqs/{rid}/close")
    if state == "expired":
        await _expire(db, rid)
    assert (await other.post(f"/rfqs/{rid}/save")).status_code == 404
    assert (await other.post(f"/rfqs/{uuid.uuid4()}/save")).status_code == 404


async def test_save_is_idempotent_and_race_safe(make_actor):
    viewer = await make_actor("buyer")
    seller = await make_actor("seller")
    rid = (await seller.post("/rfqs", json=rfq_body(role="seller"))).json()["id"]
    results = await asyncio.gather(*[safe(viewer.post(f"/rfqs/{rid}/save")) for _ in range(6)])
    assert all(s == 200 for s, _ in results), [s for s, _ in results]
    assert (await viewer.get("/rfqs/saved/ids")).json() == [rid]


@pytest.mark.parametrize("state", ["closed", "expired"])
async def test_saved_ids_consistent_with_saved_list(make_actor, db, state):
    viewer = await make_actor("buyer")
    seller = await make_actor("seller")
    keep = (await seller.post("/rfqs", json=rfq_body(role="seller"))).json()["id"]
    gone = (await seller.post("/rfqs", json=rfq_body(role="seller"))).json()["id"]
    await viewer.post(f"/rfqs/{keep}/save")
    await viewer.post(f"/rfqs/{gone}/save")
    if state == "closed":
        await seller.post(f"/rfqs/{gone}/close")
    else:
        await _expire(db, gone)
    ids = (await viewer.get("/rfqs/saved/ids")).json()
    listing = (await viewer.get("/rfqs/saved")).json()
    assert ids == [keep]
    assert listing["total"] == 1 and [i["id"] for i in listing["items"]] == [keep]


# ---------------------------------------------------------------------------
# 10. Reviews / trust
# ---------------------------------------------------------------------------


async def test_trust_only_moves_on_first_review_of_a_pair(make_actor, db):
    buyer, seller, conn_id, _ = await _pair(make_actor)
    prof = await db.scalar(select(UserProfile).where(UserProfile.user_id == uuid.UUID(seller.id)))
    start = prof.trust_score
    for _ in range(3):
        qid = await _full_deal(buyer, seller, conn_id)
        r = await buyer.post(f"/connections/{conn_id}/quotes/{qid}/rate", json={"rating": 5})
        assert r.status_code == 201, r.text
    db.expire_all()
    prof = await db.scalar(select(UserProfile).where(UserProfile.user_id == uuid.UUID(seller.id)))
    assert prof.trust_score - start == 5
    # Every review still counts towards the visible rating summary.
    assert prof.total_reviews == 3


async def test_quote_reviews_require_participation(make_actor):
    buyer, seller, conn_id, _ = await _pair(make_actor)
    qid = await _full_deal(buyer, seller, conn_id)
    await buyer.post(f"/connections/{conn_id}/quotes/{qid}/rate",
                     json={"rating": 2, "comment": "private complaint"})
    stranger = await make_actor("buyer")
    assert (await stranger.get(f"/connections/{conn_id}/quotes/{qid}/reviews")).status_code == 404
    assert (await stranger.get(f"/connections/{uuid.uuid4()}/quotes/{qid}/reviews")).status_code == 404
    # The parties themselves can read, but only via the right connection id.
    assert (await buyer.get(f"/connections/{uuid.uuid4()}/quotes/{qid}/reviews")).status_code == 404
    for party in (buyer, seller):
        r = await party.get(f"/connections/{conn_id}/quotes/{qid}/reviews")
        assert r.status_code == 200 and [x["comment"] for x in r.json()] == ["private complaint"]


async def test_user_reviews_404_for_unknown_user(make_actor):
    viewer = await make_actor("buyer")
    assert (await viewer.get(f"/users/{uuid.uuid4()}/reviews")).status_code == 404
    assert (await viewer.get(f"/users/{viewer.id}/reviews")).status_code == 200


async def test_user_reviews_requires_auth(client, make_actor):
    someone = await make_actor("buyer")
    assert (await client.get(f"/users/{someone.id}/reviews")).status_code == 401

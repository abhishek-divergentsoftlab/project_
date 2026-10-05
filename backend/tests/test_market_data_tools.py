"""Read-only market data tools: dedup, visibility, exclusion and statistics."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from models.enums import RFQRole, RFQStatus
from models.rfq import RFQ
from models.user import User
from services import market_data_tools as tools


async def _user(db, actor) -> User:
    return await db.get(User, uuid.UUID(actor.id))


async def seed(db, owner, *, title="Procurement of 50,000 pcs Appy Fizz in Indore", role=RFQRole.BUYER,
               price=18, per_unit="pcs", qty=50000, status=RFQStatus.ACTIVE, expires_in_days=10,
               city="Indore", currency="INR", category="Food & Beverages") -> RFQ:
    rfq = RFQ(
        user_id=uuid.UUID(owner.id),
        role=role,
        status=status,
        category=category,
        title=title,
        product_details={"name": "appy fizz"},
        quantity_value=Decimal(qty) if qty is not None else None,
        quantity_unit="pcs",
        price_amount=Decimal(str(price)) if price is not None else None,
        price_currency=currency,
        price_per_unit=per_unit,
        location_city=city,
        search_text=title.lower(),
        expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
    )
    db.add(rfq)
    await db.commit()
    await db.refresh(rfq)
    return rfq


async def test_appy_fizz_five_copies_are_one_listing_with_no_sellers(db, make_actor):
    poster = await make_actor("buyer")
    viewer = await make_actor("buyer")
    for _ in range(5):
        await seed(db, poster)

    viewer_user = await _user(db, viewer)
    res = await tools.run_tool(db, viewer_user, "search_listings", {"product": "Appy Fizz"})
    assert res["ok"]
    assert res["data"]["distinct_listings"] == 1
    assert res["data"]["duplicates_removed"] == 4
    assert res["data"]["distinct_users"] == 1
    assert res["data"]["by_role"] == {"buyer": 1, "seller": 0}
    assert any("duplicate" in n for n in res["notes"])

    stats = await tools.run_tool(db, viewer_user, "market_price_stats", {"product": "Appy Fizz", "role": "seller"})
    assert stats["ok"] and stats["data"]["n_listings"] == 0


async def test_exclude_rfq_and_include_mine(db, make_actor):
    me = await make_actor("buyer")
    other = await make_actor("seller")
    mine = await seed(db, me, title="Need 100 kg onion", price=20, per_unit="kg", qty=100)
    theirs = await seed(db, other, title="Onion supply 500 kg", role=RFQRole.SELLER, price=25, per_unit="kg", qty=500)
    me_user = await _user(db, me)

    default = await tools.search_listings(db, user=me_user, product="onion")
    assert [s["id"] for s in default["data"]["samples"]] == [str(theirs.id)]

    with_mine = await tools.search_listings(db, user=me_user, product="onion", include_mine=True)
    assert with_mine["data"]["distinct_listings"] == 2

    excluded = await tools.search_listings(
        db, user=me_user, product="onion", include_mine=True, exclude_rfq_id=str(mine.id)
    )
    assert [s["id"] for s in excluded["data"]["samples"]] == [str(theirs.id)]


async def test_expired_and_inactive_listings_are_excluded_everywhere(db, make_actor):
    poster = await make_actor("seller")
    viewer = await make_actor("buyer")
    await seed(db, poster, title="Rice 1 tonne", role=RFQRole.SELLER, expires_in_days=-1, price=30, per_unit="kg", qty=1000)
    await seed(db, poster, title="Rice 2 tonne", role=RFQRole.SELLER, status=RFQStatus.CLOSED, price=31, per_unit="kg", qty=2000)
    live = await seed(db, poster, title="Rice 3 tonne", role=RFQRole.SELLER, price=32, per_unit="kg", qty=3000)
    viewer_user = await _user(db, viewer)

    res = await tools.search_listings(db, user=viewer_user, product="rice")
    assert [s["id"] for s in res["data"]["samples"]] == [str(live.id)]
    stats = await tools.market_price_stats(db, user=viewer_user, product="rice")
    assert stats["data"]["n_listings"] == 1


async def test_price_statistics_on_a_known_set(db, make_actor):
    viewer = await make_actor("buyer")
    prices = [10, 20, 30, 40, 50]
    for p in prices:
        seller = await make_actor("seller")
        await seed(db, seller, title="Widget lot", role=RFQRole.SELLER, price=p, per_unit="pcs", qty=100)
    viewer_user = await _user(db, viewer)

    stats = await tools.market_price_stats(db, user=viewer_user, product="widget")
    d = stats["data"]
    assert (d["n_listings"], d["n_distinct_users"]) == (5, 5)
    assert (d["median"], d["p25"], d["p75"], d["min"], d["max"]) == (30, 20, 40, 10, 50)
    assert d["currency"] == "INR" and d["unit_basis"] == "pcs"
    assert "sample_warning" not in d
    assert any(f["label"] == "median price" and f["value"] == 30 for f in stats["facts"])


async def test_sample_warning_when_few_distinct_users(db, make_actor):
    viewer = await make_actor("buyer")
    one = await make_actor("seller")
    for p, q in ((10, 100), (12, 200), (14, 300)):
        await seed(db, one, title="Gadget lot", role=RFQRole.SELLER, price=p, per_unit="pcs", qty=q)
    viewer_user = await _user(db, viewer)

    stats = await tools.market_price_stats(db, user=viewer_user, product="gadget")
    assert stats["data"]["n_listings"] == 3
    assert stats["data"]["n_distinct_users"] == 1
    assert "sample_warning" in stats["data"]


async def test_get_listing_visibility_and_my_rfqs(db, make_actor):
    me = await make_actor("buyer")
    other = await make_actor("seller")
    closed_other = await seed(db, other, title="Closed thing", role=RFQRole.SELLER, status=RFQStatus.CLOSED)
    mine_closed = await seed(db, me, title="My closed", status=RFQStatus.CLOSED)
    active_other = await seed(db, other, title="Active thing", role=RFQRole.SELLER)
    me_user = await _user(db, me)

    assert (await tools.get_listing(db, user=me_user, rfq_id=str(active_other.id)))["ok"]
    assert not (await tools.get_listing(db, user=me_user, rfq_id=str(closed_other.id)))["ok"]
    own = await tools.get_listing(db, user=me_user, rfq_id=str(mine_closed.id))
    assert own["ok"] and own["data"]["is_mine"] is True

    mine = await tools.get_my_rfqs(db, user=me_user)
    assert mine["data"]["count"] == 1
    bad = await tools.get_my_rfqs(db, user=me_user, status="nonsense")
    assert not bad["ok"]


async def test_run_tool_never_raises(db, make_actor):
    me = await make_actor("buyer")
    me_user = await _user(db, me)
    assert not (await tools.run_tool(db, me_user, "no_such_tool", {}))["ok"]
    assert not (await tools.run_tool(db, me_user, "get_listing", {"rfq_id": "not-a-uuid"}))["ok"]
    assert (await tools.run_tool(db, me_user, "search_listings", "garbage"))["ok"]  # bad JSON -> no filters
    odd = await tools.run_tool(db, me_user, "search_listings", {"limit": "many"})
    assert odd["ok"] is False


async def test_freight_never_invents_unknown_places(db, make_actor):
    me = await make_actor("buyer")
    me_user = await _user(db, me)
    res = await tools.estimate_freight(db, user=me_user, origin="Qwertyville", destination="Indore", weight_kg=100)
    assert res["ok"] is False
    # Even with a weight, only distance comes back: the platform has no verified rates.
    dist_only = await tools.estimate_freight(db, user=me_user, origin="Indore", destination="Pune", weight_kg=500)
    assert dist_only["ok"] and "options" not in dist_only["data"]
    assert dist_only["data"]["freight_rates_available"] is False
    assert {f["unit"] for f in dist_only["facts"]} == {"km"}
    assert any("quote" in n for n in dist_only["notes"])


async def test_schemas_cover_every_tool():
    names = {s["function"]["name"] for s in tools.READ_TOOL_SCHEMAS}
    assert names == set(tools.READ_TOOL_NAMES)


async def test_find_matches_runs_and_finds_the_counterparty(db, make_actor):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    wanted = await seed(db, buyer, title="Need 1000 pcs usb type-c cable", price=200, qty=1000)
    await seed(db, seller, title="Supplying usb type-c cable", role=RFQRole.SELLER, price=190, qty=5000)
    me = await _user(db, buyer)
    res = await tools.find_matches(db, user=me, rfq_id=str(wanted.id))
    assert res["ok"], res["notes"]
    assert res["data"]["total_matches"] >= 1

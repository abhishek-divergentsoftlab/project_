"""Tests for AI content moderation and prohibited items detection."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.moderation import ModerationLog


@pytest.mark.asyncio
async def test_moderation_preflight_endpoint(client: AsyncClient):
    # 1. Prohibited item check
    resp_bad = await client.post(
        "/moderation/check",
        json={"title": "Wholesale Glock pistols and 9mm ammunition"},
    )
    assert resp_bad.status_code == 200
    res = resp_bad.json()
    assert res["is_safe"] is False
    assert res["category"] == "firearms_and_weapons"
    assert len(res["flagged_terms"]) > 0

    # 2. Safe item check
    resp_good = await client.post(
        "/moderation/check",
        json={"title": "5000 units of industrial cotton t-shirts", "category": "Apparel"},
    )
    assert resp_good.status_code == 200
    res2 = resp_good.json()
    assert res2["is_safe"] is True
    assert res2["category"] is None


@pytest.mark.asyncio
async def test_prohibited_rfq_blocked(db: AsyncSession, make_actor):
    user = await make_actor("seller", name="Contraband Seller")

    # 1. Attempt to post firearms/guns
    gun_rfq = {
        "role": "seller",
        "category": "Defense",
        "title": "Military Grade Assault Rifles and Guns for export",
        "description": "High velocity rifles with suppressor",
        "status": "active",
    }
    resp1 = await user.post("/rfqs", json=gun_rfq)
    assert resp1.status_code == 422
    assert "ai safety moderation" in resp1.json()["detail"].lower()
    assert "firearms" in resp1.json()["detail"].lower()

    # 2. Attempt to post human organs / trafficking
    organ_rfq = {
        "role": "seller",
        "category": "Medical",
        "title": "Urgent need to buy human kidney donor",
        "description": "Offering high compensation for healthy organ",
        "status": "active",
    }
    resp2 = await user.post("/rfqs", json=organ_rfq)
    assert resp2.status_code == 422
    assert "ai safety moderation" in resp2.json()["detail"].lower()

    # 3. Attempt to post illicit narcotics
    narcotics_rfq = {
        "role": "seller",
        "category": "Chemicals",
        "title": "Bulk pure cocaine and fentanyl precursor supplies",
        "status": "active",
    }
    resp3 = await user.post("/rfqs", json=narcotics_rfq)
    assert resp3.status_code == 422
    assert "narcotics" in resp3.json()["detail"].lower()

    # 4. Verify moderation log recorded these blocked attempts
    import uuid
    logs_query = select(ModerationLog).where(ModerationLog.user_id == uuid.UUID(user.id))
    logs = list((await db.execute(logs_query)).scalars().all())
    assert len(logs) == 3
    assert all(log.blocked is True for log in logs)

    # 5. Legitimate industrial tool with "gun" in name (e.g. glue gun) IS ALLOWED
    glue_gun_rfq = {
        "role": "seller",
        "category": "Industrial Tools",
        "title": "Professional 100W Hot Melt Glue Gun for packaging line",
        "description": "Industrial heavy duty glue gun with nozzle",
        "status": "active",
    }
    resp_legit = await user.post("/rfqs", json=glue_gun_rfq)
    assert resp_legit.status_code == 201
    assert resp_legit.json()["title"] == "Professional 100W Hot Melt Glue Gun for packaging line"


@pytest.mark.asyncio
async def test_evasion_bypass_attacks_blocked(client: AsyncClient, make_actor):
    user = await make_actor("seller", name="Evasion Attacker")

    # 1. Spaced weapon model with firearm part: "ak4 7 with barrel"
    resp_ak = await client.post(
        "/moderation/check",
        json={"title": "ak4 7 with barrel"},
    )
    assert resp_ak.status_code == 200
    res_ak = resp_ak.json()
    assert res_ak["is_safe"] is False
    assert res_ak["category"] == "firearms_and_weapons"
    assert "ak47" in res_ak["flagged_terms"]

    # 2. RFQ creation with "ak4 7 with barrel" is blocked with 422
    ak_rfq = {
        "role": "seller",
        "category": "Defense",
        "title": "ak4 7 with barrel",
        "description": "Custom tactical stock included",
        "status": "active",
    }
    resp_rfq_ak = await user.post("/rfqs", json=ak_rfq)
    assert resp_rfq_ak.status_code == 422
    assert "ai safety moderation" in resp_rfq_ak.json()["detail"].lower()

    # 3. Underage minor / child exploitation: "13 year girls"
    resp_minor = await client.post(
        "/moderation/check",
        json={"title": "13 year girls"},
    )
    assert resp_minor.status_code == 200
    res_minor = resp_minor.json()
    assert res_minor["is_safe"] is False
    assert res_minor["category"] == "child_exploitation_and_minors"

    # 4. RFQ creation with minor exploitation is strictly blocked with 422
    minor_rfq = {
        "role": "seller",
        "category": "Services",
        "title": "13 year girls available for hire",
        "status": "active",
    }
    resp_rfq_minor = await user.post("/rfqs", json=minor_rfq)
    assert resp_rfq_minor.status_code == 422
    assert "ai safety moderation" in resp_rfq_minor.json()["detail"].lower()
    assert "child sexual exploitation" in resp_rfq_minor.json()["detail"].lower()

    # 5. Additional spacing and leetspeak bypass variations
    evasion_checks = [
        ("a k 4 7 with barrel", "firearms_and_weapons"),
        ("a-k-4-7 rifle", "firearms_and_weapons"),
        ("ar 1 5 rifle receiver", "firearms_and_weapons"),
        ("threaded barrel for pistol", "firearms_and_weapons"),
        ("g u n for sale", "firearms_and_weapons"),
        ("b0mb assembly manual", "firearms_and_weapons"),
        ("13 yo girl", "child_exploitation_and_minors"),
        ("14 year old boy", "child_exploitation_and_minors"),
        ("under 18 yo girl", "child_exploitation_and_minors"),
        ("under 18 yo girl  ", "child_exploitation_and_minors"),
        ("order under 18 yo girl", "child_exploitation_and_minors"),
        ("under-18 escort", "child_exploitation_and_minors"),
        ("underage girls escort", "child_exploitation_and_minors"),
        ("buy girl", "child_exploitation_and_minors"),
    ]
    for text, expected_category in evasion_checks:
        r = await client.post("/moderation/check", json={"title": text})
        assert r.status_code == 200
        data = r.json()
        assert data["is_safe"] is False, f"Expected {text!r} to be blocked"
        assert data["category"] == expected_category, f"Expected {expected_category} for {text!r}, got {data['category']}"

    # 6. Legitimate apparel and industrial containers MUST pass safely
    safe_samples = [
        "13 year girls dresses and apparel",
        "under 18 yo girl dresses",
        "under 18 youth sports gear",
        "sell kids t-shirts wholesale",
        "buy baby clothes bulk",
        "50 steel barrels for crude oil storage",
        "wooden oak wine barrel",
        "heavy duty plastic drum barrel 55 gallon",
    ]
    for text in safe_samples:
        r = await client.post("/moderation/check", json={"title": text})
        assert r.status_code == 200
        data = r.json()
        assert data["is_safe"] is True, f"Expected {text!r} to be allowed, but got: {data}"


@pytest.mark.asyncio
async def test_ammunition_and_caliber_moderation(client: AsyncClient):
    """Verify comprehensive ammunition, caliber, and weapon cartridge detection."""
    ammo_queries = [
        "i want 5.56 ammo",
        "i need 5.56mm ammo",
        "5.56 nato",
        "5.56 rounds",
        "5.56 mm bullets",
        "5.56x45",
        "7.62x51",
        "7.62x39",
        "7.62 nato",
        "9mm",
        "9mm ammo",
        "9mm parabellum",
        "308 win ammo",
        "223 rem",
        "hollow point bullets",
        "12 gauge shells",
        "i need ammo",
        "buy rifle ammo",
    ]
    for q in ammo_queries:
        r = await client.post("/moderation/check", json={"title": q})
        assert r.status_code == 200
        data = r.json()
        assert data["is_safe"] is False, f"Expected '{q}' to be blocked"
        assert data["category"] == "firearms_and_weapons", f"Expected firearms_and_weapons for '{q}'"


@pytest.mark.asyncio
async def test_commercial_human_trade_and_adult_trafficking(client: AsyncClient, make_actor):
    """Verify commercial human trading, supply of women/ladies, and typo bypasses are blocked."""
    prohibited_queries = [
        "i suppy 67 yo leddy",
        "i supply 67 yo lady",
        "supply 67 yo lady",
        "i supply 30 yo woman",
        "selling 25 yo lady",
        "order 20 yo woman",
        "buy 40 yo lady",
        "hire 50 yo maid",
        "i supply 67 year old lady",
        "67 yo leddy",
        "67 yo lady",
        "buy 30 year old woman",
        "sell lady",
        "buy woman",
        "supply maid",
    ]
    for q in prohibited_queries:
        r = await client.post("/moderation/check", json={"title": q})
        assert r.status_code == 200
        data = r.json()
        assert data["is_safe"] is False, f"Expected '{q}' to be blocked"
        assert data["category"] in ("human_trafficking_and_organs", "child_exploitation_and_minors")

    # RFQ post with human supply is blocked with 422
    user = await make_actor("seller", name="Human Trafficker")
    resp_rfq = await user.post(
        "/rfqs", json={"role": "seller", "category": "Services", "title": "i suppy 67 yo leddy", "status": "active"}
    )
    assert resp_rfq.status_code == 422
    assert "ai safety moderation" in resp_rfq.json()["detail"].lower()

    # Moderation check with human supply is blocked
    resp_check = await client.post("/moderation/check", json={"title": "i suppy 67 yo leddy"})
    assert resp_check.status_code == 200
    check_data = resp_check.json()
    assert check_data["is_safe"] is False
    assert check_data["category"] == "human_trafficking_and_organs"

    # Legitimate ladies and women apparel/garments must remain 100% permitted
    safe_samples = [
        "supply ladies dresses",
        "sell women shoes",
        "buy ladies clothing bulk",
        "looking for women apparel",
        "sell men t-shirts",
        "buy ladies kurti wholesale",
        "supply women handbags",
        "5000 units of ladies garments",
    ]
    for q in safe_samples:
        r = await client.post("/moderation/check", json={"title": q})
        assert r.status_code == 200
        data = r.json()
        assert data["is_safe"] is True, f"Expected safe sample '{q}' to pass, got: {data}"





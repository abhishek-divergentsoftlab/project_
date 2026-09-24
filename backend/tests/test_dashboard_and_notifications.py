"""Tests for Dashboard & Analytics, Multi-Agent catalogue, and Notification Center."""

import pytest
import uuid


async def test_list_agents_catalogue(make_actor):
    actor = await make_actor("buyer")
    res = await actor.get("/ai-chat/agents")
    assert res.status_code == 200
    agents = res.json()
    assert len(agents) >= 7
    agent_ids = {a["id"] for a in agents}
    assert "general" in agent_ids
    assert "market_research" in agent_ids
    assert "rfq_drafting" in agent_ids
    assert "negotiation" in agent_ids
    assert "price_analyst" in agent_ids
    assert "logistics" in agent_ids
    assert "verification" in agent_ids

    # Each agent has icon, name, description, suggested_prompts
    for a in agents:
        assert a["icon"]
        assert a["name"]
        assert a["description"]
        assert a["short_description"]


async def test_dashboard_stats_and_activity(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    # Initial empty stats
    res = await buyer.get("/dashboard/stats")
    assert res.status_code == 200
    data = res.json()
    assert "rfqs" in data
    assert "connections" in data
    assert "quotations" in data
    assert "profile_completeness" in data
    assert data["rfqs"]["total"] == 0

    # Post an RFQ
    rfq = await make_rfq(buyer, role="buyer", title="Need Organic Apples")
    res2 = await buyer.get("/dashboard/stats")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["rfqs"]["total"] >= 1

    # Check activity feed
    act_res = await buyer.get("/dashboard/activity")
    assert act_res.status_code == 200
    assert isinstance(act_res.json(), list)


async def test_notifications_lifecycle(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")

    # Initially unread count is 0
    c_res = await seller.get("/notifications/unread-count")
    assert c_res.status_code == 200
    assert c_res.json()["count"] == 0

    # Seller creates an RFQ
    rfq = await make_rfq(seller, role="seller", title="Fresh Alphonso Mangoes")

    # Buyer connects to Seller's RFQ
    conn_res = await buyer.post("/connections", json={"rfq_id": rfq["id"]})
    assert conn_res.status_code == 201

    # Seller should now have 1 unread notification!
    c_res2 = await seller.get("/notifications/unread-count")
    assert c_res2.status_code == 200
    assert c_res2.json()["count"] >= 1

    # Seller lists notifications
    list_res = await seller.get("/notifications")
    assert list_res.status_code == 200
    notif_data = list_res.json()
    assert notif_data["total"] >= 1
    items = notif_data["items"]
    assert len(items) >= 1
    first_notif = items[0]
    assert first_notif["type"] == "connection_request"
    assert first_notif["is_read"] is False

    # Mark single notification as read
    read_res = await seller.post(f"/notifications/{first_notif['id']}/read")
    assert read_res.status_code == 200
    assert read_res.json()["ok"] is True

    # Unread count should now be decremented
    c_res3 = await seller.get("/notifications/unread-count")
    assert c_res3.json()["count"] == 0

    # Test mark-all-read endpoint
    mark_all_res = await seller.post("/notifications/read-all")
    assert mark_all_res.status_code == 200

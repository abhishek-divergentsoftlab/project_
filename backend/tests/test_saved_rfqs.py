"""Tests for Saved (bookmarked) RFQs feature."""

import uuid
import pytest
from tests.conftest import rfq_body


async def test_save_and_unsave_rfq(make_actor):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")

    # Seller creates an active RFQ
    seller_rfq = (await seller.post("/rfqs", json=rfq_body("seller"))).json()
    rfq_id = seller_rfq["id"]

    # Buyer saves seller's RFQ
    save_res = await buyer.post(f"/rfqs/{rfq_id}/save")
    assert save_res.status_code == 200
    assert save_res.json() == {"status": "saved", "rfq_id": rfq_id}

    # Saving again is idempotent
    save_again = await buyer.post(f"/rfqs/{rfq_id}/save")
    assert save_again.status_code == 200
    assert save_again.json() == {"status": "saved", "rfq_id": rfq_id}

    # Saved IDs list contains rfq_id
    ids_res = await buyer.get("/rfqs/saved/ids")
    assert ids_res.status_code == 200
    assert rfq_id in ids_res.json()

    # List saved RFQs returns the listing with is_saved = True
    list_saved = await buyer.get("/rfqs/saved")
    assert list_saved.status_code == 200
    saved_items = list_saved.json()["items"]
    assert len(saved_items) == 1
    assert saved_items[0]["id"] == rfq_id
    assert saved_items[0]["is_saved"] is True

    # Marketplace catalog with saved_only=True returns the saved listing
    market_saved = await buyer.get("/marketplace/catalog?saved_only=true")
    assert market_saved.status_code == 200
    assert any(item["id"] == rfq_id for item in market_saved.json()["items"])

    # General marketplace catalog shows is_saved = True for this item
    market_all = await buyer.get("/marketplace/catalog")
    assert market_all.status_code == 200
    catalog_item = next((item for item in market_all.json()["items"] if item["id"] == rfq_id), None)
    assert catalog_item is not None
    assert catalog_item["is_saved"] is True

    # Another user (seller) has empty saved list (isolation)
    seller_saved = await seller.get("/rfqs/saved/ids")
    assert seller_saved.status_code == 200
    assert rfq_id not in seller_saved.json()

    # Buyer unsaves the RFQ
    unsave_res = await buyer.delete(f"/rfqs/{rfq_id}/save")
    assert unsave_res.status_code == 200
    assert unsave_res.json() == {"status": "unsaved", "rfq_id": rfq_id}

    # Saved list is now empty for buyer
    ids_after = await buyer.get("/rfqs/saved/ids")
    assert ids_after.status_code == 200
    assert rfq_id not in ids_after.json()

    list_after = await buyer.get("/rfqs/saved")
    assert list_after.status_code == 200
    assert len(list_after.json()["items"]) == 0


async def test_save_nonexistent_rfq_returns_404(make_actor):
    buyer = await make_actor("buyer")
    fake_id = str(uuid.uuid4())
    res = await buyer.post(f"/rfqs/{fake_id}/save")
    assert res.status_code == 404


async def test_unsave_unsaved_rfq_is_safe(make_actor):
    buyer = await make_actor("buyer")
    fake_id = str(uuid.uuid4())
    res = await buyer.delete(f"/rfqs/{fake_id}/save")
    assert res.status_code == 200
    assert res.json()["status"] == "unsaved"


async def test_multiple_saved_rfqs_and_pagination(make_actor):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")

    # Create 3 RFQs from seller
    ids = []
    for i in range(3):
        body = rfq_body("seller", title=f"Industrial Steel Grade {i+1}")
        rfq = (await seller.post("/rfqs", json=body)).json()
        ids.append(rfq["id"])

    # Buyer saves all 3
    for rfq_id in ids:
        await buyer.post(f"/rfqs/{rfq_id}/save")

    # Verify saved IDs count
    saved_ids = (await buyer.get("/rfqs/saved/ids")).json()
    for rfq_id in ids:
        assert rfq_id in saved_ids

    # Paginated query limit 2
    page1 = (await buyer.get("/rfqs/saved?limit=2&offset=0")).json()
    assert len(page1["items"]) == 2
    assert page1["total"] == 3

    page2 = (await buyer.get("/rfqs/saved?limit=2&offset=2")).json()
    assert len(page2["items"]) == 1
    assert page2["total"] == 3


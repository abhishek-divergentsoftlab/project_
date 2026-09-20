"""Unit and integration tests for B2B Quotations and Deal Room."""

import uuid
from decimal import Decimal
import pytest



async def test_quote_requires_accepted_connection(make_actor, make_rfq):
    buyer = await make_actor("buyer")
    seller = await make_actor("seller")
    listing = await make_rfq(seller, role="seller", title="GaN Chargers")

    conn_res = await buyer.post("/connections", json={"rfq_id": listing["id"]})
    conn_id = conn_res.json()["id"]

    # Still pending
    quote_res = await seller.post(
        f"/connections/{conn_id}/quotes",
        json={
            "unit_price": 450.0,
            "currency": "INR",
            "quantity": 1000.0,
            "quantity_unit": "pcs",
        },
    )
    assert quote_res.status_code == 409


async def test_create_quote_success(accepted_pair):
    buyer, seller, conn_id, _listing = accepted_pair

    quote_payload = {
        "unit_price": 125.50,
        "currency": "INR",
        "quantity": 2000.0,
        "quantity_unit": "pcs",
        "lead_time_days": 10,
        "incoterms": "FOB",
        "payment_terms": "30% Advance, 70% on Dispatch",
        "valid_days": 21,
        "notes": "Includes wooden crate pallet packaging.",
    }

    res = await seller.post(f"/connections/{conn_id}/quotes", json=quote_payload)
    assert res.status_code == 201

    data = res.json()
    assert data["quote_number"].startswith(f"QT-{conn_id[:8].upper()}-v1")
    assert data["version"] == 1
    assert data["status"] == "pending"
    assert Decimal(str(data["unit_price"])) == Decimal("125.5000")
    assert Decimal(str(data["quantity"])) == Decimal("2000.0000")
    assert Decimal(str(data["total_amount"])) == Decimal("251000.0000")
    assert data["incoterms"] == "FOB"
    assert data["is_sender"] is True

    # Buyer views the quote
    buyer_view = await buyer.get(f"/connections/{conn_id}/quotes")
    assert buyer_view.status_code == 200
    quotes = buyer_view.json()
    assert len(quotes) == 1
    assert quotes[0]["id"] == data["id"]
    assert quotes[0]["is_sender"] is False


async def test_counter_offer_increments_version_and_supersedes_previous(accepted_pair):
    buyer, seller, conn_id, _listing = accepted_pair

    # 1. Seller offers ₹150 for 5000 units
    q1 = (
        await seller.post(
            f"/connections/{conn_id}/quotes",
            json={"unit_price": 150.0, "currency": "INR", "quantity": 5000.0, "quantity_unit": "pcs"},
        )
    ).json()
    assert q1["version"] == 1
    assert q1["status"] == "pending"

    # 2. Buyer counter-offers ₹130 for 6000 units
    q2 = (
        await buyer.post(
            f"/connections/{conn_id}/quotes",
            json={"unit_price": 130.0, "currency": "INR", "quantity": 6000.0, "quantity_unit": "pcs", "notes": "Counter offer: higher volume"},
        )
    ).json()
    assert q2["version"] == 2
    assert q2["status"] == "pending"

    # Check that previous quote is now marked COUNTERED
    history = (await seller.get(f"/connections/{conn_id}/quotes")).json()
    assert len(history) == 2
    assert history[0]["id"] == q2["id"]
    assert history[0]["status"] == "pending"
    assert history[1]["id"] == q1["id"]
    assert history[1]["status"] == "countered"


async def test_accept_quote_generates_purchase_order(accepted_pair):
    buyer, seller, conn_id, _listing = accepted_pair

    # Seller sends quote
    q = (
        await seller.post(
            f"/connections/{conn_id}/quotes",
            json={"unit_price": 85.0, "currency": "INR", "quantity": 10000.0, "quantity_unit": "pcs"},
        )
    ).json()

    # Buyer accepts
    accept_res = await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/accept")
    assert accept_res.status_code == 200

    accepted = accept_res.json()
    assert accepted["status"] == "accepted"
    assert accepted["purchase_order_reference"] is not None
    assert accepted["purchase_order_reference"].startswith("PO-")


async def test_cannot_accept_own_quote(accepted_pair):
    _buyer, seller, conn_id, _listing = accepted_pair

    q = (
        await seller.post(
            f"/connections/{conn_id}/quotes",
            json={"unit_price": 85.0, "currency": "INR", "quantity": 1000.0, "quantity_unit": "pcs"},
        )
    ).json()

    # Seller tries to accept their own quote
    res = await seller.post(f"/connections/{conn_id}/quotes/{q['id']}/accept")
    assert res.status_code == 400
    assert "own quotation" in res.json()["detail"].lower()


async def test_cannot_accept_already_accepted_quote(accepted_pair):
    buyer, seller, conn_id, _listing = accepted_pair

    q = (
        await seller.post(
            f"/connections/{conn_id}/quotes",
            json={"unit_price": 50.0, "currency": "INR", "quantity": 500.0, "quantity_unit": "pcs"},
        )
    ).json()

    # First acceptance succeeds
    assert (await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/accept")).status_code == 200

    # Second acceptance fails
    second = await buyer.post(f"/connections/{conn_id}/quotes/{q['id']}/accept")
    assert second.status_code == 409


async def test_reject_quote_with_reason(accepted_pair):
    buyer, seller, conn_id, _listing = accepted_pair

    q = (
        await seller.post(
            f"/connections/{conn_id}/quotes",
            json={"unit_price": 990.0, "currency": "INR", "quantity": 100.0, "quantity_unit": "pcs"},
        )
    ).json()

    reject_res = await buyer.post(
        f"/connections/{conn_id}/quotes/{q['id']}/reject",
        json={"reason": "Price is significantly above our budget threshold."},
    )
    assert reject_res.status_code == 200
    rejected = reject_res.json()
    assert rejected["status"] == "rejected"
    assert "Price is significantly above" in rejected["notes"]


async def test_stranger_cannot_view_or_create_quotes(accepted_pair, make_actor):
    _buyer, _seller, conn_id, _listing = accepted_pair
    stranger = await make_actor("buyer")

    assert (await stranger.get(f"/connections/{conn_id}/quotes")).status_code == 403
    assert (
        await stranger.post(
            f"/connections/{conn_id}/quotes",
            json={"unit_price": 10.0, "currency": "INR", "quantity": 10.0, "quantity_unit": "pcs"},
        )
    ).status_code == 403

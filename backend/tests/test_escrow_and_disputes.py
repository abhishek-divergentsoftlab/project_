"""Automated tests for Milestone Escrow Vault, Fund Releases, and Formal Dispute Resolution."""

import pytest
from decimal import Decimal


async def test_escrow_full_lifecycle_and_security(accepted_pair, make_actor):
    buyer, seller, conn_id, _listing = accepted_pair
    outsider = await make_actor()

    # 1. Seller creates quotation
    quote_payload = {
        "unit_price": 100.00,
        "currency": "USD",
        "quantity": 100.0,
        "quantity_unit": "units",
        "lead_time_days": 10,
        "incoterms": "FOB",
        "payment_terms": "Escrow Milestones (30/40/30)",
        "valid_days": 14,
        "notes": "Premium quality batch.",
    }
    q_res = await seller.post(f"/connections/{conn_id}/quotes", json=quote_payload)
    assert q_res.status_code == 201
    quote = q_res.json()
    quote_id = quote["id"]

    # 2. Guard: Attempting to access escrow before quote acceptance returns 400
    early_escrow = await buyer.get(f"/connections/{conn_id}/escrow")
    assert early_escrow.status_code == 400
    assert "No accepted quotation" in early_escrow.json()["detail"]

    # 3. Buyer accepts the quotation
    accept_res = await buyer.post(f"/connections/{conn_id}/quotes/{quote_id}/accept")
    assert accept_res.status_code == 200

    # 4. Now Escrow is automatically initialized upon retrieval
    escrow_res = await buyer.get(f"/connections/{conn_id}/escrow")
    assert escrow_res.status_code == 200
    escrow = escrow_res.json()
    assert float(escrow["total_amount"]) == 10000.0
    assert escrow["currency"] == "USD"
    assert escrow["status"] == "pending_deposit"
    assert len(escrow["milestones"]) == 3
    assert escrow["milestones"][0]["status"] == "pending"
    assert float(escrow["milestones"][0]["amount"]) == 3000.0
    assert float(escrow["milestones"][1]["amount"]) == 4000.0
    assert float(escrow["milestones"][2]["amount"]) == 3000.0

    # 5. Guard: Outsider cannot view escrow
    outsider_view = await outsider.get(f"/connections/{conn_id}/escrow")
    assert outsider_view.status_code == 403

    # 6. Guard: Seller cannot deposit funds (Buyer-only operation)
    seller_fund = await seller.post(
        f"/connections/{conn_id}/escrow/fund",
        json={"payment_method": "mock_instant"},
    )
    assert seller_fund.status_code == 403
    assert "Only the registered Buyer" in seller_fund.json()["detail"]

    # 7. Buyer funds the Escrow Vault
    buyer_fund = await buyer.post(
        f"/connections/{conn_id}/escrow/fund",
        json={"payment_method": "mock_instant", "notes": "Approved for 100% escrow vault allocation."},
    )
    assert buyer_fund.status_code == 200
    funded_escrow = buyer_fund.json()
    assert funded_escrow["status"] == "funded"
    assert float(funded_escrow["funded_amount"]) == 10000.0
    assert funded_escrow["milestones"][0]["status"] == "funded"
    assert funded_escrow["milestones"][1]["status"] == "funded"

    m1_id = funded_escrow["milestones"][0]["id"]
    m2_id = funded_escrow["milestones"][1]["id"]
    m3_id = funded_escrow["milestones"][2]["id"]

    # 8. Milestone 1: Seller requests release, Buyer approves and releases
    # Buyer cannot request payout
    buyer_req = await buyer.post(
        f"/connections/{conn_id}/escrow/milestones/{m1_id}/request-release",
        json={"proof_note": "Production batch 1 completed"},
    )
    assert buyer_req.status_code == 403

    # Seller requests payout
    seller_req = await seller.post(
        f"/connections/{conn_id}/escrow/milestones/{m1_id}/request-release",
        json={"proof_note": "Raw materials procured and cutting started"},
    )
    assert seller_req.status_code == 200
    assert seller_req.json()["milestones"][0]["status"] == "release_requested"

    # Seller cannot approve own release
    seller_approve = await seller.post(
        f"/connections/{conn_id}/escrow/milestones/{m1_id}/release",
        json={"note": "Approved"},
    )
    assert seller_approve.status_code == 403

    # Buyer approves release of Milestone 1
    buyer_approve = await buyer.post(
        f"/connections/{conn_id}/escrow/milestones/{m1_id}/release",
        json={"note": "Workmanship verified by inspection agent."},
    )
    assert buyer_approve.status_code == 200
    escrow_after_m1 = buyer_approve.json()
    assert escrow_after_m1["status"] == "partially_released"
    assert float(escrow_after_m1["released_amount"]) == 3000.0
    assert escrow_after_m1["milestones"][0]["status"] == "released"

    # 9. Dispute Flow: Buyer raises a formal dispute regarding Milestone 2
    dispute_payload = {
        "title": "Packaging specification mismatch",
        "category": "specification_mismatch",
        "reason": "Goods dispatched with single-wall cartons instead of double-wall export boxes.",
        "severity": "high",
        "suggested_resolution": "Supplier provides carton reinforcement or 10% credit.",
    }
    dispute_res = await buyer.post(f"/connections/{conn_id}/disputes", json=dispute_payload)
    assert dispute_res.status_code == 201
    dispute = dispute_res.json()
    assert dispute["status"] == "open"
    assert dispute["severity"] == "high"
    dispute_id = dispute["id"]

    # Escrow is now frozen in DISPUTED state
    escrow_disputed = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    assert escrow_disputed["status"] == "disputed"
    assert escrow_disputed["milestones"][1]["status"] == "disputed"

    # Guard: Releases are strictly blocked while in dispute
    blocked_req = await seller.post(
        f"/connections/{conn_id}/escrow/milestones/{m2_id}/request-release",
        json={"proof_note": "Dispatch proof"},
    )
    assert blocked_req.status_code == 400
    assert "locked while a formal dispute is active" in blocked_req.json()["detail"]

    blocked_rel = await buyer.post(
        f"/connections/{conn_id}/escrow/milestones/{m2_id}/release",
        json={"note": "Release anyway"},
    )
    assert blocked_rel.status_code == 400
    assert "locked while a formal dispute is active" in blocked_rel.json()["detail"]

    # 10. Dispute Resolution: Counterparties agree to refund remaining funds to buyer
    resolve_payload = {
        "resolution": "refund_buyer",
        "resolution_notes": "Mutually agreed to terminate order and refund unreleased $7,000 balance to Buyer.",
    }
    resolve_res = await seller.post(
        f"/connections/{conn_id}/disputes/{dispute_id}/resolve",
        json=resolve_payload,
    )
    assert resolve_res.status_code == 200
    resolved_dispute = resolve_res.json()
    assert resolved_dispute["status"] == "resolved_refund_buyer"

    # Verify Escrow account reflects refund
    final_escrow = (await buyer.get(f"/connections/{conn_id}/escrow")).json()
    assert final_escrow["status"] == "refunded"
    assert float(final_escrow["released_amount"]) == 3000.0
    assert float(final_escrow["refunded_amount"]) == 7000.0


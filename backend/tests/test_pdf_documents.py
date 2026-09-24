"""Automated tests for Purchase Order and Commercial Invoice PDF generation."""

import pytest


async def test_pdf_generation_flow_and_guards(accepted_pair, make_actor):
    buyer, seller, conn_id, _listing = accepted_pair

    # 1. Seller issues a quotation
    quote_payload = {
        "unit_price": 250.00,
        "currency": "INR",
        "quantity": 500.0,
        "quantity_unit": "pcs",
        "lead_time_days": 7,
        "incoterms": "CIF",
        "payment_terms": "100% Escrow on Delivery",
        "valid_days": 14,
        "notes": "Custom branded packaging included.",
    }
    create_res = await seller.post(f"/connections/{conn_id}/quotes", json=quote_payload)
    assert create_res.status_code == 201
    quote = create_res.json()
    quote_id = quote["id"]

    # 2. Guard: Attempting to download PO or Invoice while quote is still pending should fail with 409
    po_early = await buyer.get(f"/connections/{conn_id}/quotes/{quote_id}/po-pdf")
    assert po_early.status_code == 409
    assert "accepted quotations" in po_early.json()["detail"]

    inv_early = await seller.get(f"/connections/{conn_id}/quotes/{quote_id}/invoice-pdf")
    assert inv_early.status_code == 409

    # 3. Buyer accepts the quotation
    accept_res = await buyer.post(f"/connections/{conn_id}/quotes/{quote_id}/accept")
    assert accept_res.status_code == 200
    accepted_quote = accept_res.json()
    assert accepted_quote["status"] == "accepted"
    assert accepted_quote["purchase_order_reference"].startswith("PO-")

    # 4. Buyer downloads Purchase Order PDF
    po_res = await buyer.get(f"/connections/{conn_id}/quotes/{quote_id}/po-pdf")
    assert po_res.status_code == 200
    assert po_res.headers["content-type"] == "application/pdf"
    assert "attachment" in po_res.headers["content-disposition"]
    assert "Purchase_Order_" in po_res.headers["content-disposition"]

    po_content = po_res.content
    assert po_content.startswith(b"%PDF-")
    assert len(po_content) > 1000

    # 5. Seller downloads Commercial Invoice PDF
    inv_res = await seller.get(f"/connections/{conn_id}/quotes/{quote_id}/invoice-pdf")
    assert inv_res.status_code == 200
    assert inv_res.headers["content-type"] == "application/pdf"
    assert "attachment" in inv_res.headers["content-disposition"]
    assert "Commercial_Invoice_" in inv_res.headers["content-disposition"]

    inv_content = inv_res.content
    assert inv_content.startswith(b"%PDF-")
    assert len(inv_content) > 1000

    # 6. Unauthorized access guard: Third party user cannot download documents
    stranger = await make_actor("buyer")
    unauth_po = await stranger.get(f"/connections/{conn_id}/quotes/{quote_id}/po-pdf")
    assert unauth_po.status_code == 403

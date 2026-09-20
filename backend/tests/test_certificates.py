"""Tests for seller certifications management."""

import pytest


@pytest.mark.asyncio
async def test_certificates_lifecycle(make_actor):
    seller = await make_actor("seller", name="ISO Certified Seller", company_name="ISO Tech")

    # 1. Initially empty
    list_resp = await seller.get("/certifications")
    assert list_resp.status_code == 200
    assert list_resp.json() == []

    # 2. Add ISO 9001 certificate
    create_resp = await seller.post(
        "/certifications",
        json={
            "name": "ISO 9001:2015 Quality Management",
            "issuing_body": "SGS International",
            "certificate_number": "IN-2026-9001-X",
            "issue_date": "2024-01-15T00:00:00Z",
            "expiry_date": "2027-01-15T00:00:00Z",
            "document_url": "https://certificates.example.com/iso9001.pdf",
        },
    )
    assert create_resp.status_code == 201
    cert = create_resp.json()
    assert cert["name"] == "ISO 9001:2015 Quality Management"
    assert cert["verification_status"] == "verified"
    cert_id = cert["id"]

    # 3. List shows the certificate
    list_resp2 = await seller.get("/certifications")
    assert len(list_resp2.json()) == 1
    assert list_resp2.json()[0]["id"] == cert_id

    # 4. Public endpoint allows anyone to view verified certificates
    stranger = await make_actor("buyer", name="Auditor Buyer")
    public_resp = await stranger.get(f"/certifications/user/{seller.id}")
    assert public_resp.status_code == 200
    assert len(public_resp.json()) == 1

    # 5. Stranger cannot delete seller's certificate
    del_stranger = await stranger.delete(f"/certifications/{cert_id}")
    assert del_stranger.status_code == 404

    # 6. Seller can delete own certificate
    del_resp = await seller.delete(f"/certifications/{cert_id}")
    assert del_resp.status_code == 204

    # 7. List is empty again
    list_resp3 = await seller.get("/certifications")
    assert list_resp3.json() == []

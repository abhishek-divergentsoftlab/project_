"""Tests for Company KYC, GST validation, and duplicate GST anti-fraud protection."""

import pytest


@pytest.mark.asyncio
async def test_kyc_gst_verification_and_anti_fraud(make_actor):
    seller1 = await make_actor("seller", name="Enterprise Supplier 1", company_name="Apex Solutions")

    # Maharashtra GSTIN with a correct check character. (The widely quoted
    # example 27AABCU9603R1ZM fails the GSTN checksum and is now rejected.)
    valid_gst = "27AABCU9603R1ZN"

    # 1. Invalid GST format rejected
    invalid_resp = await seller1.post(
        "/users/me/kyc/verify",
        json={
            "gst_number": "123INVALID",
            "legal_business_name": "Apex Industrial Ltd",
            "business_type": "Private Limited",
        },
    )
    assert invalid_resp.status_code == 422
    assert "format" in invalid_resp.json()["detail"].lower()

    # 2. Valid GST submitted and verified
    valid_resp = await seller1.post(
        "/users/me/kyc/verify",
        json={
            "gst_number": valid_gst,
            "legal_business_name": "Apex Industrial Solutions Pvt Ltd",
            "business_type": "Private Limited",
            "registration_number": "U72200MH2020PTC123456",
            "year_established": 2020,
            "website": "https://apexsolutions.example.com",
            "pan_number": "AABCU9603R",
            "signatory_name": "Rajesh Sharma",
        },
    )
    assert valid_resp.status_code == 200
    data = valid_resp.json()
    # Format-level verification (structure + checksum + PAN-in-GSTIN), and the
    # message says so rather than implying a registry lookup.
    assert data["kyc_status"] == "verified"
    assert "checksum" in data["message"].lower()
    assert data["gst_number"] == valid_gst
    assert data["trust_score"] >= 50

    # Verify profile now reflects KYC fields
    me_resp = await seller1.get("/auth/me")
    assert me_resp.status_code == 200
    profile = me_resp.json()["profile"]
    assert profile["kyc_status"] == "verified"
    assert profile["gst_number"] == valid_gst

    # 3. ANTI-FRAUD: A different user attempts to use the SAME GSTIN
    seller2 = await make_actor("seller", name="Fake Fraudster", company_name="Fraud Co")

    dup_resp = await seller2.post(
        "/users/me/kyc/verify",
        json={
            "gst_number": valid_gst,  # Reusing already verified GSTIN!
            "legal_business_name": "Impostor Fake Corp",
            "business_type": "Proprietorship",
        },
    )
    assert dup_resp.status_code == 409
    assert "anti-fraud" in dup_resp.json()["detail"].lower()

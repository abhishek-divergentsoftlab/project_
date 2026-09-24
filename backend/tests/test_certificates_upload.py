"""Tests for certificate file upload, validation, and serving."""

import pytest


@pytest.mark.asyncio
async def test_certificate_upload_and_serving(make_actor):
    seller = await make_actor("seller", name="Cert Uploader", company_name="Cert Corp")

    # 1. Valid PDF upload
    pdf_content = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    upload_resp = await seller.post(
        "/certifications/upload",
        files={"file": ("iso_certificate.pdf", pdf_content, "application/pdf")},
    )
    assert upload_resp.status_code == 201
    data = upload_resp.json()
    assert "document_url" in data
    assert data["filename"] == "iso_certificate.pdf"
    assert data["document_url"].startswith("/api/v1/media/certificates/cert_")
    doc_url = data["document_url"]

    # 2. Fetch media file and verify content-type and security headers
    media_resp = await seller.get(doc_url.replace("/api/v1", ""))
    assert media_resp.status_code == 200
    assert media_resp.headers.get("x-content-type-options") == "nosniff"
    assert "application/pdf" in media_resp.headers.get("content-type", "")
    assert media_resp.content == pdf_content

    # 3. Create certificate with the uploaded document URL
    create_resp = await seller.post(
        "/certifications",
        json={
            "name": "ISO 27001 Security",
            "issuing_body": "BSI Global",
            "certificate_number": "SEC-2026-99",
            "issue_date": "2024-06-01T00:00:00Z",
            "document_url": doc_url,
        },
    )
    assert create_resp.status_code == 201
    cert = create_resp.json()
    cert_id = cert["id"]
    assert cert["document_url"] == doc_url

    # 4. Attach a new PNG document to the existing certificate
    png_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    attach_resp = await seller.post(
        f"/certifications/{cert_id}/document",
        files={"file": ("badge.png", png_content, "image/png")},
    )
    assert attach_resp.status_code == 200
    updated_cert = attach_resp.json()
    assert updated_cert["document_url"] != doc_url
    assert updated_cert["document_url"].endswith(".png")

    # 5. Stranger cannot attach document to seller's certificate
    stranger = await make_actor("buyer", name="Stranger Buyer")
    stranger_resp = await stranger.post(
        f"/certifications/{cert_id}/document",
        files={"file": ("hacked.png", png_content, "image/png")},
    )
    assert stranger_resp.status_code == 404


@pytest.mark.asyncio
async def test_certificate_upload_security_validations(make_actor):
    seller = await make_actor("seller", name="Secure Seller")

    # Disallowed extension (.exe)
    resp = await seller.post(
        "/certifications/upload",
        files={"file": ("malware.exe", b"MZ\x90\x00", "application/x-msdownload")},
    )
    assert resp.status_code == 400
    assert "Unsupported file extension" in resp.json()["detail"]

    # Spoofed extension: .pdf with non-PDF content
    fake_pdf = b"This is plain text pretending to be a PDF"
    resp = await seller.post(
        "/certifications/upload",
        files={"file": ("fake.pdf", fake_pdf, "application/pdf")},
    )
    assert resp.status_code == 400
    assert "does not match expected format" in resp.json()["detail"]

    # File size limit (>10MB)
    huge_file = b"%PDF-" + b"0" * (10 * 1024 * 1024 + 1)
    resp = await seller.post(
        "/certifications/upload",
        files={"file": ("huge.pdf", huge_file, "application/pdf")},
    )
    assert resp.status_code == 400
    assert "exceeds maximum allowed size" in resp.json()["detail"]

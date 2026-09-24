"""Automated tests for Multi-Modal Freight Calculator, Shipment Dispatch, and Waybill PDF Generation."""

from decimal import Decimal
import uuid
import pytest
from httpx import AsyncClient

from models.enums import ConnectionStatus, QuotationStatus, RFQRole, UserRole
from models.connection import Connection
from models.quotation import Quotation
from models.rfq import RFQ
from models.user import User, UserProfile
from services.auth_service import create_access_token


async def _create_test_trader(db, email: str, role: UserRole, company_name: str, city: str = "Mumbai") -> User:
    user = User(email=email, password_hash="hash", role=role)
    db.add(user)
    await db.flush()
    profile = UserProfile(
        user_id=user.id,
        name=company_name,
        company_name=company_name,
        city=city,
        country="India",
        gst_number=f"27AABCT{str(uuid.uuid4())[:4].upper()}1Z5",
    )
    db.add(profile)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_accepted_deal(db, buyer: User, seller: User) -> tuple[Connection, Quotation]:
    rfq = RFQ(
        user_id=buyer.id,
        title="Industrial Grade Corrugated Boxes",
        category="Packaging",
        role=RFQRole.BUYER,
        search_text="Industrial Grade Corrugated Boxes",
        product_details={"name": "Corrugated Boxes", "flute": "5-ply"},
    )
    db.add(rfq)
    await db.flush()

    conn = Connection(
        sender_id=buyer.id,
        receiver_id=seller.id,
        rfq_id=rfq.id,
        status=ConnectionStatus.ACCEPTED,
    )
    db.add(conn)
    await db.flush()

    quote = Quotation(
        connection_id=conn.id,
        sender_id=seller.id,
        receiver_id=buyer.id,
        rfq_id=rfq.id,
        quote_number="QT-2026-LOG-01",
        version=1,
        status=QuotationStatus.ACCEPTED,
        unit_price=Decimal("15.0000"),
        currency="USD",
        quantity=Decimal("500.0000"),
        quantity_unit="pcs",
        total_amount=Decimal("7500.0000"),
        incoterms="FOB",
        payment_terms="30% Advance, 40% Dispatch, 30% Acceptance",
    )
    db.add(quote)
    await db.commit()
    await db.refresh(conn)
    await db.refresh(quote)
    return conn, quote


@pytest.mark.asyncio
async def test_freight_estimation_multi_modal(client: AsyncClient):
    """Test freight rating engine across domestic and cross-border lanes."""
    # 1. Domestic India: Mumbai to New Delhi
    payload_domestic = {
        "origin_city": "Mumbai",
        "origin_country": "India",
        "destination_city": "New Delhi",
        "destination_country": "India",
        "weight_kg": 500.0,
        "volume_cbm": 2.5,
        "cargo_value": 7500.0,
        "currency": "USD",
        "incoterm": "FOB",
    }
    res = await client.post("/logistics/estimate", json=payload_domestic)
    assert res.status_code == 200
    data = res.json()
    assert data["is_cross_border"] is False
    assert data["distance_km"] > 500
    assert float(data["gross_weight_kg"]) == 500.0
    assert len(data["rate_options"]) >= 3

    # Check Road Freight exists
    road_opt = next((o for o in data["rate_options"] if "road" in o["mode_id"]), None)
    assert road_opt is not None
    assert float(road_opt["rate_amount"]) > 0
    assert road_opt["transit_days_min"] >= 1

    # Check Incoterm breakdown
    incoterm_info = data["incoterm_breakdown"]
    assert incoterm_info["incoterm"] == "FOB"
    assert len(incoterm_info["seller_pays"]) > 0
    assert len(incoterm_info["buyer_pays"]) > 0

    # 2. Cross-border: Mumbai to Hamburg
    payload_intl = {
        "origin_city": "Mumbai",
        "origin_country": "India",
        "destination_city": "Hamburg",
        "destination_country": "Germany",
        "weight_kg": 4500.0,
        "volume_cbm": 18.0,
        "cargo_value": 35000.0,
        "currency": "EUR",
        "incoterm": "CIF",
    }
    res_intl = await client.post("/logistics/estimate", json=payload_intl)
    assert res_intl.status_code == 200
    data_intl = res_intl.json()
    assert data_intl["is_cross_border"] is True
    ocean_opt = next((o for o in data_intl["rate_options"] if "ocean" in o["mode_id"]), None)
    assert ocean_opt is not None
    assert float(ocean_opt["rate_amount"]) > 0
    assert ocean_opt["transit_days_min"] >= 7


@pytest.mark.asyncio
async def test_shipment_dispatch_and_tracking_lifecycle(client: AsyncClient, db):
    """Test dispatch creation, event logging, status transitions, and PDF waybill."""
    buyer = await _create_test_trader(db, f"buyer_log_{uuid.uuid4().hex[:6]}@b2b.com", UserRole.BUYER, "Metro Retailers", "Mumbai")
    seller = await _create_test_trader(db, f"seller_log_{uuid.uuid4().hex[:6]}@b2b.com", UserRole.SELLER, "Zenith Freightworks", "Pune")
    conn, quote = await _create_accepted_deal(db, buyer, seller)

    seller_token = create_access_token(str(seller.id))
    buyer_token = create_access_token(str(buyer.id))

    # 1. No active shipment initially
    res_none = await client.get(
        f"/logistics/connections/{conn.id}/shipment",
        headers={"Authorization": f"Bearer {seller_token}"},
    )
    assert res_none.status_code == 200
    assert res_none.json() is None

    # 2. Seller creates dispatch
    dispatch_payload = {
        "carrier_name": "Safexpress Surface Logistics",
        "carrier_service": "Dedicated FTL Container Truck",
        "shipping_mode": "road",
        "tracking_number": f"SAFEX-{uuid.uuid4().hex[:6].upper()}",
        "origin_city": "Pune",
        "origin_country": "India",
        "destination_city": "Mumbai",
        "destination_country": "India",
        "weight_kg": 750.5,
        "volume_cbm": 3.2,
        "package_count": 24,
        "package_type": "Palletized Cartons",
        "estimated_delivery_days": 3,
        "dispatch_note": "Batch A loaded and dispatched under E-Way bill 488219.",
        "trigger_escrow_milestone": False,
    }

    res_dispatch = await client.post(
        f"/logistics/connections/{conn.id}/dispatch",
        json=dispatch_payload,
        headers={"Authorization": f"Bearer {seller_token}"},
    )
    assert res_dispatch.status_code == 201
    shipment_data = res_dispatch.json()
    shipment_id = shipment_data["id"]
    assert shipment_data["tracking_number"] == dispatch_payload["tracking_number"]
    assert shipment_data["status"] == "dispatched"
    assert len(shipment_data["tracking_events"]) == 1
    assert "Safexpress" in shipment_data["carrier_name"]

    # 3. Buyer fetches connection shipment
    res_buyer = await client.get(
        f"/logistics/connections/{conn.id}/shipment",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert res_buyer.status_code == 200
    assert res_buyer.json()["id"] == shipment_id

    # 4. Append transit tracking checkpoint
    update_payload = {
        "status": "in_transit",
        "location": "Thane Tollway Checkpoint",
        "note": "Customs transit inspection cleared. Driver en route to warehouse.",
    }
    res_update = await client.post(
        f"/logistics/shipments/{shipment_id}/events",
        json=update_payload,
        headers={"Authorization": f"Bearer {seller_token}"},
    )
    assert res_update.status_code == 200
    updated_data = res_update.json()
    assert updated_data["status"] == "in_transit"
    assert len(updated_data["tracking_events"]) == 2

    # 5. Mark delivered
    deliv_payload = {
        "status": "delivered",
        "location": "Mumbai Buyer Warehouse Gate 3",
        "note": "Unloaded and signed by receiving warehouse manager.",
    }
    res_deliv = await client.post(
        f"/logistics/shipments/{shipment_id}/events",
        json=deliv_payload,
        headers={"Authorization": f"Bearer {seller_token}"},
    )
    assert res_deliv.status_code == 200
    assert res_deliv.json()["status"] == "delivered"
    assert res_deliv.json()["delivered_at"] is not None

    # 6. Download Waybill PDF
    res_pdf = await client.get(
        f"/logistics/shipments/{shipment_id}/waybill.pdf",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert res_pdf.status_code == 200
    assert res_pdf.headers["content-type"] == "application/pdf"
    assert res_pdf.content.startswith(b"%PDF")
    assert len(res_pdf.content) > 1000


@pytest.mark.asyncio
async def test_shipment_security_and_permissions(client: AsyncClient, db):
    """Test that unauthorized outsiders cannot access or tamper with shipments."""
    buyer = await _create_test_trader(db, f"b_{uuid.uuid4().hex[:6]}@test.com", UserRole.BUYER, "Buyer Corp")
    seller = await _create_test_trader(db, f"s_{uuid.uuid4().hex[:6]}@test.com", UserRole.SELLER, "Seller Corp")
    outsider = await _create_test_trader(db, f"out_{uuid.uuid4().hex[:6]}@test.com", UserRole.BUYER, "Outsider Corp")

    conn, _ = await _create_accepted_deal(db, buyer, seller)
    outsider_token = create_access_token(str(outsider.id))

    # Outsider viewing shipment
    res_view = await client.get(
        f"/logistics/connections/{conn.id}/shipment",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert res_view.status_code == 403

    # Outsider attempting dispatch
    res_disp = await client.post(
        f"/logistics/connections/{conn.id}/dispatch",
        json={"carrier_name": "Hacker Logistics"},
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert res_disp.status_code == 403

"""Tests for Marketplace Catalog and Sourcing Directory endpoints."""

import pytest
from tests.conftest import rfq_body


async def test_marketplace_catalog_basic_and_privacy(make_actor):
    seller = await make_actor("seller")
    buyer = await make_actor("buyer")

    # Seller creates and publishes an RFQ
    seller_rfq = (
        await seller.post(
            "/rfqs",
            json=rfq_body(
                role="seller",
                category="Electronics",
                title="Industrial USB-C Braided Cables",
                status="active",
            ),
        )
    ).json()

    # Buyer browses the marketplace catalog
    res = await buyer.get("/marketplace/catalog")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 1
    assert any(item["id"] == seller_rfq["id"] for item in data["items"])

    # Locate the seller's card
    card = next(item for item in data["items"] if item["id"] == seller_rfq["id"])
    assert card["title"] == "Industrial USB-C Braided Cables"
    assert card["category"] == "Electronics"
    assert card["role"] == "seller"
    assert card["counterparty"]["company_name"] is not None

    # Strict Privacy invariant: Counterparty projection must not leak phone or email
    assert "email" not in card["counterparty"]
    assert "phone" not in card["counterparty"]
    assert "address" not in card["counterparty"]

    # Seller browsing catalog must NOT see their own listing
    seller_view = (await seller.get("/marketplace/catalog")).json()
    assert all(item["id"] != seller_rfq["id"] for item in seller_view["items"])


async def test_marketplace_role_and_category_filtering(make_actor):
    seller = await make_actor("seller")
    buyer = await make_actor("buyer")
    viewer = await make_actor("both")

    await seller.post(
        "/rfqs",
        json=rfq_body(
            role="seller",
            category="Packaging",
            title="5-Ply Corrugated Heavy Duty Shipping Boxes",
            status="active",
        ),
    )

    await buyer.post(
        "/rfqs",
        json=rfq_body(
            role="buyer",
            category="Agriculture",
            title="Export Grade Organic Basmati Rice 1121",
            status="active",
        ),
    )

    # Filter by role=seller
    seller_only = (await viewer.get("/marketplace/catalog?role=seller")).json()
    assert all(item["role"] == "seller" for item in seller_only["items"])
    assert any(item["category"] == "Packaging" for item in seller_only["items"])

    # Filter by role=buyer
    buyer_only = (await viewer.get("/marketplace/catalog?role=buyer")).json()
    assert all(item["role"] == "buyer" for item in buyer_only["items"])
    assert any(item["category"] == "Agriculture" for item in buyer_only["items"])

    # Filter by category=Packaging
    pkg_only = (await viewer.get("/marketplace/catalog?category=Packaging")).json()
    assert all(item["category"] == "Packaging" for item in pkg_only["items"])

    # Search by text query
    search_res = (await viewer.get("/marketplace/catalog?q=Basmati")).json()
    assert any("Basmati" in item["title"] for item in search_res["items"])


async def test_marketplace_categories_summary(make_actor):
    actor = await make_actor("seller")
    await actor.post(
        "/rfqs",
        json=rfq_body(
            role="seller",
            category="Textiles",
            title="100% Pure Organic Raw Cotton Bales",
            status="active",
        ),
    )

    viewer = await make_actor("buyer")
    res = await viewer.get("/marketplace/categories")
    assert res.status_code == 200
    cats = res.json()
    assert isinstance(cats, list)
    assert any(c["category"] == "Textiles" and c["seller_count"] >= 1 for c in cats)

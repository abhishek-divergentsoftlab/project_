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


async def test_marketplace_single_listing_and_link(make_actor):
    seller = await make_actor("seller")
    buyer = await make_actor("buyer")

    seller_rfq = (
        await seller.post(
            "/rfqs",
            json=rfq_body(
                role="seller",
                category="Industrial",
                title="CNC Machined Precision Aluminum Brackets",
                status="active",
            ),
        )
    ).json()

    # Buyer accesses the single listing using the RFQ ID (e.g. from Chat AI link /marketplace?rfq=...)
    res = await buyer.get(f"/marketplace/catalog/{seller_rfq['id']}")
    assert res.status_code == 200
    listing = res.json()
    assert listing["id"] == seller_rfq["id"]
    assert listing["title"] == "CNC Machined Precision Aluminum Brackets"
    assert listing["category"] == "Industrial"
    assert listing["counterparty"]["company_name"] is not None


async def test_marketplace_search_rice_precision_and_category_count_alignment(make_actor):
    seller1 = await make_actor("seller")
    seller2 = await make_actor("seller")
    buyer = await make_actor("buyer")

    # Seller 1 creates non-rice listing with target price (search_text contains 'Target price:')
    chair_rfq = (
        await seller1.post(
            "/rfqs",
            json=rfq_body(
                role="seller",
                category="Furniture",
                title="Ergonomic High-Back Executive Office Chair",
                status="active",
                price=(150.0, "INR", "pcs"),
            ),
        )
    ).json()

    # Seller 2 creates genuine Basmati Rice listing
    rice_rfq = (
        await seller2.post(
            "/rfqs",
            json=rfq_body(
                role="seller",
                category="Agriculture",
                title="Premium Traditional Basmati Rice 1121",
                status="active",
                price=(80.0, "INR", "kg"),
            ),
        )
    ).json()

    # 1. Search for "rice" must ONLY return the rice listing, NOT the chair
    search_res = (await buyer.get("/marketplace/catalog?q=rice")).json()
    assert search_res["total"] == 1
    assert len(search_res["items"]) == 1
    assert search_res["items"][0]["id"] == rice_rfq["id"]
    assert all(item["id"] != chair_rfq["id"] for item in search_res["items"])

    # 2. Category summary for "rice" must only show Agriculture, not Furniture
    cat_res = (await buyer.get("/marketplace/categories?q=rice")).json()
    agri_cat = next((c for c in cat_res if c["category"] == "Agriculture"), None)
    assert agri_cat is not None
    assert agri_cat["total_count"] == 1
    assert not any(c["category"] == "Furniture" for c in cat_res)

    # 3. Viewer exclusion alignment: Buyer creates their own Agriculture rice RFQ
    buyer_own_rfq = (
        await buyer.post(
            "/rfqs",
            json=rfq_body(
                role="buyer",
                category="Agriculture",
                title="Need 50 Tons Basmati Rice",
                status="active",
                price=(75.0, "INR", "kg"),
            ),
        )
    ).json()

    # Buyer must not see their own RFQ in catalog
    buyer_catalog = (await buyer.get("/marketplace/catalog?q=rice")).json()
    assert buyer_catalog["total"] == 1
    assert all(item["id"] != buyer_own_rfq["id"] for item in buyer_catalog["items"])

    # Buyer's category count must match their catalog count (1, not 2)
    buyer_cats = (await buyer.get("/marketplace/categories?q=rice")).json()
    buyer_agri = next((c for c in buyer_cats if c["category"] == "Agriculture"), None)
    assert buyer_agri is not None
    assert buyer_agri["total_count"] == 1
    assert buyer_agri["total_count"] == buyer_catalog["total"]



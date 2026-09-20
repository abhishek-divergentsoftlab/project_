"""Tests for post-delivery reviews and ratings."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.enums import Incoterm, QuotationStatus
from models.quotation import Quotation


async def _create_accepted_quote(
    db: AsyncSession, connection_id: uuid.UUID, sender_id: uuid.UUID, receiver_id: uuid.UUID, rfq_id: uuid.UUID
) -> Quotation:
    quote = Quotation(
        connection_id=connection_id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        rfq_id=rfq_id,
        quote_number="QT-TEST-001",
        version=1,
        status=QuotationStatus.ACCEPTED,
        unit_price=Decimal("150.00"),
        currency="INR",
        quantity=Decimal("100"),
        quantity_unit="pcs",
        total_amount=Decimal("15000.00"),
        incoterms=Incoterm.FOB,
        payment_terms="Net 30",
        purchase_order_reference="PO-TEST-001",
    )
    db.add(quote)
    await db.commit()
    await db.refresh(quote)
    return quote


@pytest.mark.asyncio
async def test_delivery_and_mutual_rating(db: AsyncSession, accepted_pair, make_actor):
    buyer, seller, conn_id, listing = accepted_pair

    quote = await _create_accepted_quote(
        db,
        connection_id=uuid.UUID(conn_id),
        sender_id=uuid.UUID(seller.id),
        receiver_id=uuid.UUID(buyer.id),
        rfq_id=uuid.UUID(listing["id"]),
    )

    # 1. Rating before Step 3 (buyer acceptance) is refused
    rate_resp = await buyer.post(
        f"/connections/{conn_id}/quotes/{quote.id}/rate",
        json={"rating": 5, "comment": "Excellent goods"},
    )
    assert rate_resp.status_code == 400

    # Step 1: Seller marks order as dispatched
    dispatch_resp = await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/dispatch")
    assert dispatch_resp.status_code == 200
    assert dispatch_resp.json()["status"] == "dispatched"

    # Step 2: Seller marks order as delivered
    deliver_resp = await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/deliver")
    assert deliver_resp.status_code == 200
    assert deliver_resp.json()["status"] == "delivered"

    # Step 3: Buyer accepts order delivery
    accept_resp = await buyer.post(f"/connections/{conn_id}/quotes/{quote.id}/accept-delivery")
    assert accept_resp.status_code == 200
    assert accept_resp.json()["status"] == "received"

    # Step 4: Buyer rates seller
    rate_buyer_resp = await buyer.post(
        f"/connections/{conn_id}/quotes/{quote.id}/rate",
        json={
            "rating": 5,
            "communication_rating": 5,
            "delivery_rating": 4,
            "quality_rating": 5,
            "comment": "Goods arrived on time, top notch quality!",
        },
    )
    assert rate_buyer_resp.status_code == 201
    assert rate_buyer_resp.json()["rating"] == 5

    # 4. Duplicate rating by buyer for same quote is blocked
    dup_resp = await buyer.post(
        f"/connections/{conn_id}/quotes/{quote.id}/rate",
        json={"rating": 4},
    )
    assert dup_resp.status_code == 400
    assert "already reviewed" in dup_resp.json()["detail"].lower()

    # 5. Seller rates buyer in return
    rate_seller_resp = await seller.post(
        f"/connections/{conn_id}/quotes/{quote.id}/rate",
        json={
            "rating": 5,
            "communication_rating": 5,
            "comment": "Prompt payment and clear requirements.",
        },
    )
    assert rate_seller_resp.status_code == 201

    # 6. Fetch seller's public review statistics
    stats_resp = await buyer.get(f"/users/{seller.id}/reviews")
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats["total_reviews"] == 1
    assert stats["average_rating"] == 5.0
    assert stats["rating_breakdown"]["5"] == 1
    assert len(stats["recent_reviews"]) == 1


@pytest.mark.asyncio
async def test_stranger_cannot_rate(db: AsyncSession, accepted_pair, make_actor):
    buyer, seller, conn_id, listing = accepted_pair
    stranger = await make_actor("buyer", name="Stranger Buyer")

    quote = await _create_accepted_quote(
        db,
        connection_id=uuid.UUID(conn_id),
        sender_id=uuid.UUID(seller.id),
        receiver_id=uuid.UUID(buyer.id),
        rfq_id=uuid.UUID(listing["id"]),
    )

    # Move through steps to received
    await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/dispatch")
    await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/deliver")
    await buyer.post(f"/connections/{conn_id}/quotes/{quote.id}/accept-delivery")

    # Stranger attempts to rate
    resp = await stranger.post(
        f"/connections/{conn_id}/quotes/{quote.id}/rate",
        json={"rating": 1, "comment": "Trolling attempt"},
    )
    assert resp.status_code == 400
    assert "participants" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_strict_3_step_fulfillment_role_guards(db: AsyncSession, accepted_pair):
    buyer, seller, conn_id, listing = accepted_pair

    quote = await _create_accepted_quote(
        db,
        connection_id=uuid.UUID(conn_id),
        sender_id=uuid.UUID(seller.id),
        receiver_id=uuid.UUID(buyer.id),
        rfq_id=uuid.UUID(listing["id"]),
    )

    # 1. Buyer attempting to dispatch fails (only seller dispatches)
    buyer_dispatch = await buyer.post(f"/connections/{conn_id}/quotes/{quote.id}/dispatch")
    assert buyer_dispatch.status_code == 400
    assert "only the seller" in buyer_dispatch.json()["detail"].lower()

    # 2. Seller delivering before dispatching fails
    early_deliver = await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/deliver")
    assert early_deliver.status_code == 400
    assert "dispatched first" in early_deliver.json()["detail"].lower()

    # 3. Seller dispatches correctly (Step 1)
    ok_dispatch = await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/dispatch")
    assert ok_dispatch.status_code == 200
    assert ok_dispatch.json()["status"] == "dispatched"

    # 4. Buyer attempting to mark delivered fails (only seller marks delivered)
    buyer_deliver = await buyer.post(f"/connections/{conn_id}/quotes/{quote.id}/deliver")
    assert buyer_deliver.status_code == 400
    assert "only the seller" in buyer_deliver.json()["detail"].lower()

    # 5. Seller marks delivered (Step 2)
    ok_deliver = await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/deliver")
    assert ok_deliver.status_code == 200
    assert ok_deliver.json()["status"] == "delivered"

    # 6. Seller attempting to accept delivery fails (only buyer accepts)
    seller_accept = await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/accept-delivery")
    assert seller_accept.status_code == 400
    assert "only the buyer" in seller_accept.json()["detail"].lower()

    # 7. Rating before buyer accepts delivery fails
    early_rate = await buyer.post(f"/connections/{conn_id}/quotes/{quote.id}/rate", json={"rating": 5})
    assert early_rate.status_code == 400
    assert "accepted the order delivery" in early_rate.json()["detail"].lower()

    # 8. Buyer accepts delivery (Step 3)
    ok_accept = await buyer.post(f"/connections/{conn_id}/quotes/{quote.id}/accept-delivery")
    assert ok_accept.status_code == 200
    assert ok_accept.json()["status"] == "received"


@pytest.mark.asyncio
async def test_seller_strictly_blocked_until_buyer_rates(db: AsyncSession, accepted_pair):
    buyer, seller, conn_id, listing = accepted_pair

    quote = await _create_accepted_quote(
        db,
        connection_id=uuid.UUID(conn_id),
        sender_id=uuid.UUID(seller.id),
        receiver_id=uuid.UUID(buyer.id),
        rfq_id=uuid.UUID(listing["id"]),
    )

    # 3 fulfillment steps
    await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/dispatch")
    await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/deliver")
    await buyer.post(f"/connections/{conn_id}/quotes/{quote.id}/accept-delivery")

    # Seller tries to rate buyer before buyer has rated
    seller_early_resp = await seller.post(
        f"/connections/{conn_id}/quotes/{quote.id}/rate",
        json={"rating": 5, "comment": "Trying to rate first"},
    )
    assert seller_early_resp.status_code == 400
    assert "buyer must rate the order delivery first" in seller_early_resp.json()["detail"].lower()

    # Buyer rates seller
    buyer_rate_resp = await buyer.post(
        f"/connections/{conn_id}/quotes/{quote.id}/rate",
        json={"rating": 5, "comment": "Superb product delivered!"},
    )
    assert buyer_rate_resp.status_code == 201

    # Now seller is unlocked to rate buyer in return
    seller_rate_resp = await seller.post(
        f"/connections/{conn_id}/quotes/{quote.id}/rate",
        json={"rating": 4, "comment": "Great buyer, smooth transaction."},
    )
    assert seller_rate_resp.status_code == 201

    # Verify GET /connections/{conn_id}/quotes/{quote.id}/reviews endpoint
    list_rev_resp = await buyer.get(f"/connections/{conn_id}/quotes/{quote.id}/reviews")
    assert list_rev_resp.status_code == 200
    revs = list_rev_resp.json()
    assert len(revs) == 2


@pytest.mark.asyncio
async def test_ratings_affect_future_match_scoring(db: AsyncSession, accepted_pair):
    from models.rfq import RFQ
    from models.user import User, UserProfile
    from services.match_service import _score

    buyer, seller, conn_id, listing = accepted_pair

    quote = await _create_accepted_quote(
        db,
        connection_id=uuid.UUID(conn_id),
        sender_id=uuid.UUID(seller.id),
        receiver_id=uuid.UUID(buyer.id),
        rfq_id=uuid.UUID(listing["id"]),
    )
    await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/dispatch")
    await seller.post(f"/connections/{conn_id}/quotes/{quote.id}/deliver")
    await buyer.post(f"/connections/{conn_id}/quotes/{quote.id}/accept-delivery")
    await buyer.post(
        f"/connections/{conn_id}/quotes/{quote.id}/rate",
        json={"rating": 5, "comment": "Outstanding quality"},
    )

    await db.rollback()
    seller_profile = await db.scalar(select(UserProfile).where(UserProfile.user_id == uuid.UUID(seller.id)))
    assert seller_profile is not None
    assert seller_profile.total_reviews >= 1
    assert seller_profile.average_rating == Decimal("5.00")

    rfq_listing = await db.scalar(
        select(RFQ)
        .options(selectinload(RFQ.user).selectinload(User.profile))
        .where(RFQ.id == uuid.UUID(listing["id"]))
    )
    score, keep = _score(rfq_listing, rfq_listing, similarity=0.9)
    assert keep is True
    assert score.total > 0.0



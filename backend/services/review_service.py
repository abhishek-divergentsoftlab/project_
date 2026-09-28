"""Review and rating service for post-delivery mutual feedback."""

import uuid
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.connection import Connection
from models.enums import QuotationStatus, RFQRole
from models.quotation import Quotation
from models.review import Review
from models.rfq import RFQ
from models.user import User, UserProfile
from schemas.review import ReviewCreate, ReviewOut, UserReviewStatsOut
from services.websocket_manager import ws_manager


class ReviewError(Exception):
    """Domain-level rejection translated to 4xx."""


async def _get_quote_participants(db: AsyncSession, quote: Quotation) -> tuple[uuid.UUID, uuid.UUID]:
    """Determine (buyer_id, seller_id) for this quotation."""
    rfq = await db.scalar(select(RFQ).where(RFQ.id == quote.rfq_id))
    if rfq and rfq.role == RFQRole.BUYER:
        buyer_id = rfq.user_id
        seller_id = quote.sender_id if quote.sender_id != buyer_id else quote.receiver_id
    elif rfq and rfq.role == RFQRole.SELLER:
        seller_id = rfq.user_id
        buyer_id = quote.sender_id if quote.sender_id != seller_id else quote.receiver_id
    else:
        seller_id = quote.sender_id
        buyer_id = quote.receiver_id
    return buyer_id, seller_id


async def mark_quote_dispatched(
    db: AsyncSession, connection_id: uuid.UUID, quote_id: uuid.UUID, user_id: uuid.UUID
) -> Quotation:
    """Step 1: Seller marks the accepted order as dispatched."""
    query = select(Quotation).where(
        Quotation.id == quote_id,
        Quotation.connection_id == connection_id,
    )
    result = await db.execute(query)
    quote = result.scalar_one_or_none()

    if quote is None:
        raise ReviewError("Quotation not found")

    buyer_id, seller_id = await _get_quote_participants(db, quote)

    if user_id != seller_id:
        raise ReviewError("Only the seller can dispatch the order")

    if quote.status != QuotationStatus.ACCEPTED:
        raise ReviewError(
            f"Only accepted quotations can be dispatched (current status: {quote.status.value})"
        )

    quote.status = QuotationStatus.DISPATCHED
    await db.commit()
    await db.refresh(quote)

    await ws_manager.broadcast(
        connection_id,
        "quote_dispatched",
        {
            "quotation_id": str(quote.id),
            "quote_number": quote.quote_number,
            "status": quote.status.value,
        },
    )
    return quote


async def mark_quote_delivered(
    db: AsyncSession, connection_id: uuid.UUID, quote_id: uuid.UUID, user_id: uuid.UUID
) -> Quotation:
    """Step 2: Seller marks the dispatched order as delivered."""
    query = select(Quotation).where(
        Quotation.id == quote_id,
        Quotation.connection_id == connection_id,
    )
    result = await db.execute(query)
    quote = result.scalar_one_or_none()

    if quote is None:
        raise ReviewError("Quotation not found")

    buyer_id, seller_id = await _get_quote_participants(db, quote)

    if user_id != seller_id:
        raise ReviewError("Only the seller can mark the order as delivered")

    if quote.status != QuotationStatus.DISPATCHED:
        raise ReviewError(
            f"Order must be dispatched first before marking as delivered (current status: {quote.status.value})"
        )

    quote.status = QuotationStatus.DELIVERED
    await db.commit()
    await db.refresh(quote)

    # Broadcast event to Deal Room
    await ws_manager.broadcast(
        connection_id,
        "quote_delivered",
        {
            "quotation_id": str(quote.id),
            "quote_number": quote.quote_number,
            "status": quote.status.value,
        },
    )
    return quote


async def accept_quote_delivery(
    db: AsyncSession, connection_id: uuid.UUID, quote_id: uuid.UUID, user_id: uuid.UUID
) -> Quotation:
    """Step 3: Buyer accepts the delivered order."""
    query = select(Quotation).where(
        Quotation.id == quote_id,
        Quotation.connection_id == connection_id,
    )
    result = await db.execute(query)
    quote = result.scalar_one_or_none()

    if quote is None:
        raise ReviewError("Quotation not found")

    buyer_id, seller_id = await _get_quote_participants(db, quote)

    if user_id != buyer_id:
        raise ReviewError("Only the buyer can accept and confirm delivery for this order")

    if quote.status != QuotationStatus.DELIVERED:
        raise ReviewError(
            f"Only delivered orders can be accepted by the buyer (current status: {quote.status.value})"
        )

    quote.status = QuotationStatus.RECEIVED
    await db.commit()
    await db.refresh(quote)

    await ws_manager.broadcast(
        connection_id,
        "delivery_accepted",
        {
            "quotation_id": str(quote.id),
            "quote_number": quote.quote_number,
            "status": quote.status.value,
        },
    )
    return quote


async def create_review(
    db: AsyncSession,
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    payload: ReviewCreate,
) -> Review:
    query = select(Quotation).where(
        Quotation.id == quote_id,
        Quotation.connection_id == connection_id,
    )
    result = await db.execute(query)
    quote = result.scalar_one_or_none()

    if quote is None:
        raise ReviewError("Quotation not found")

    if reviewer_id not in (quote.sender_id, quote.receiver_id):
        raise ReviewError("Only transaction participants can rate this order")

    if quote.status not in (QuotationStatus.RECEIVED, QuotationStatus.COMPLETED):
        raise ReviewError(
            "Ratings are only unlocked after the buyer has accepted the order delivery (Step 3)"
        )

    # Determine buyer and seller roles for this transaction
    buyer_id, seller_id = await _get_quote_participants(db, quote)

    # Enforce sequential order: Buyer must rate first, then seller can rate in return
    if reviewer_id == seller_id:
        buyer_review = await db.scalar(
            select(Review).where(
                Review.quotation_id == quote_id,
                Review.reviewer_id == buyer_id,
            )
        )
        if buyer_review is None:
            raise ReviewError("The buyer must rate the order delivery first before the seller can review the buyer.")

    # Determine counterparty
    reviewee_id = quote.receiver_id if reviewer_id == quote.sender_id else quote.sender_id

    # Check for existing review
    existing_query = select(Review).where(
        Review.quotation_id == quote_id,
        Review.reviewer_id == reviewer_id,
    )
    existing = (await db.execute(existing_query)).scalar_one_or_none()
    if existing is not None:
        raise ReviewError("You have already reviewed this transaction")

    review = Review(
        quotation_id=quote_id,
        connection_id=connection_id,
        reviewer_id=reviewer_id,
        reviewee_id=reviewee_id,
        rating=payload.rating,
        communication_rating=payload.communication_rating,
        delivery_rating=payload.delivery_rating,
        quality_rating=payload.quality_rating,
        comment=payload.comment,
    )
    db.add(review)

    # Check if both sides have now reviewed
    other_review_query = select(Review).where(
        Review.quotation_id == quote_id,
        Review.reviewer_id == reviewee_id,
    )
    other_review = (await db.execute(other_review_query)).scalar_one_or_none()
    if other_review is not None:
        quote.status = QuotationStatus.COMPLETED

    # Trust moves only on the FIRST review a reviewer gives a reviewee. Two
    # colluding accounts looping tiny deals on one connection used to earn +5
    # per loop until they crossed the "verified only" threshold (60).
    # (The session does not autoflush, so the pending review is not counted.)
    prior_reviews_same_pair = await db.scalar(
        select(func.count(Review.id)).where(
            Review.reviewer_id == reviewer_id,
            Review.reviewee_id == reviewee_id,
        )
    )

    # Update reviewee's trust score and recalculate overall average rating
    profile_query = select(UserProfile).where(UserProfile.user_id == reviewee_id)
    profile = (await db.execute(profile_query)).scalar_one_or_none()
    if profile:
        if not prior_reviews_same_pair:
            bonus = 5 if payload.rating >= 4 else (1 if payload.rating == 3 else -5)
            profile.trust_score = max(0, min(100, profile.trust_score + bonus))

        existing_ratings = (await db.scalars(
            select(Review.rating).where(Review.reviewee_id == reviewee_id)
        )).all()
        all_ratings = list(existing_ratings) + [payload.rating]
        profile.total_reviews = len(all_ratings)
        profile.average_rating = Decimal(str(round(sum(all_ratings) / len(all_ratings), 2)))

    await db.commit()
    await db.refresh(review)

    # Broadcast event
    await ws_manager.broadcast(
        connection_id,
        "review_submitted",
        {
            "quotation_id": str(quote_id),
            "reviewer_id": str(reviewer_id),
            "reviewer_role": "buyer" if reviewer_id == buyer_id else "seller",
            "rating": review.rating,
        },
    )
    return review


class ReviewNotFoundError(ReviewError):
    """The quotation/user does not exist or is not visible to the caller (404)."""


async def list_quote_reviews(
    db: AsyncSession,
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    viewer_id: Optional[uuid.UUID] = None,
) -> list[ReviewOut]:
    """Fetch reviews submitted for a specific quotation.

    The quotation must belong to ``connection_id`` and, when ``viewer_id`` is
    given, the viewer must be a party to it; otherwise ``ReviewNotFoundError``
    (a 404, so quote ids cannot be probed).
    """
    quote = await db.scalar(
        select(Quotation).where(
            Quotation.id == quote_id,
            Quotation.connection_id == connection_id,
        )
    )
    if quote is None:
        raise ReviewNotFoundError("Quotation not found")
    if viewer_id is not None and viewer_id not in (quote.sender_id, quote.receiver_id):
        connection = await db.get(Connection, connection_id)
        if connection is None or viewer_id not in (connection.sender_id, connection.receiver_id):
            raise ReviewNotFoundError("Quotation not found")

    query = (
        select(Review)
        .options(selectinload(Review.reviewer).selectinload(User.profile))
        .where(Review.quotation_id == quote_id, Review.connection_id == connection_id)
    )
    result = await db.execute(query)
    reviews = list(result.scalars().all())
    items = []
    for r in reviews:
        reviewer_name = r.reviewer.profile.name if r.reviewer and r.reviewer.profile else None
        reviewer_company = r.reviewer.profile.company_name if r.reviewer and r.reviewer.profile else None
        items.append(
            ReviewOut(
                id=r.id,
                quotation_id=r.quotation_id,
                connection_id=r.connection_id,
                reviewer_id=r.reviewer_id,
                reviewee_id=r.reviewee_id,
                rating=r.rating,
                communication_rating=r.communication_rating,
                delivery_rating=r.delivery_rating,
                quality_rating=r.quality_rating,
                comment=r.comment,
                created_at=r.created_at,
                reviewer_name=reviewer_name,
                reviewer_company=reviewer_company,
            )
        )
    return items


async def get_user_review_stats(db: AsyncSession, user_id: uuid.UUID) -> UserReviewStatsOut:
    """Public rating summary for a user. ``ReviewNotFoundError`` for unknown ids."""
    if await db.get(User, user_id) is None:
        raise ReviewNotFoundError("User not found")
    query = (
        select(Review)
        .options(
            selectinload(Review.reviewer).selectinload(User.profile)
        )
        .where(Review.reviewee_id == user_id)
        .order_by(Review.created_at.desc())
    )
    result = await db.execute(query)
    reviews = list(result.scalars().all())

    breakdown = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    total = len(reviews)
    if total == 0:
        avg = 0.0
    else:
        total_stars = 0
        for r in reviews:
            stars = max(1, min(5, r.rating))
            breakdown[stars] = breakdown.get(stars, 0) + 1
            total_stars += stars
        avg = round(total_stars / total, 2)

    items = []
    for r in reviews[:20]:
        reviewer_name = None
        reviewer_company = None
        if r.reviewer and r.reviewer.profile:
            reviewer_name = r.reviewer.profile.name
            reviewer_company = r.reviewer.profile.company_name

        items.append(
            ReviewOut(
                id=r.id,
                quotation_id=r.quotation_id,
                connection_id=r.connection_id,
                reviewer_id=r.reviewer_id,
                reviewee_id=r.reviewee_id,
                rating=r.rating,
                communication_rating=r.communication_rating,
                delivery_rating=r.delivery_rating,
                quality_rating=r.quality_rating,
                comment=r.comment,
                created_at=r.created_at,
                reviewer_name=reviewer_name,
                reviewer_company=reviewer_company,
            )
        )

    return UserReviewStatsOut(
        user_id=user_id,
        average_rating=avg,
        total_reviews=total,
        rating_breakdown=breakdown,
        recent_reviews=items,
    )

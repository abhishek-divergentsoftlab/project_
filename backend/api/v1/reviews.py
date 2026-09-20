"""Review and rating endpoints for post-delivery mutual feedback."""

import uuid

from fastapi import APIRouter, HTTPException, status

from api.deps import CurrentUser, DbSession
from schemas.quotation import QuotationOut
from schemas.review import ReviewCreate, ReviewOut, UserReviewStatsOut
from services import quotation_service, review_service
from services.review_service import ReviewError

router = APIRouter()


@router.post(
    "/connections/{connection_id}/quotes/{quote_id}/dispatch",
    response_model=QuotationOut,
    summary="Step 1: Seller marks order as dispatched",
)
async def mark_dispatched(
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> QuotationOut:
    try:
        quote = await review_service.mark_quote_dispatched(
            db, connection_id=connection_id, quote_id=quote_id, user_id=current_user.id
        )
    except ReviewError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return quotation_service.to_out(quote, viewer_id=current_user.id)


@router.post(
    "/connections/{connection_id}/quotes/{quote_id}/deliver",
    response_model=QuotationOut,
    summary="Step 2: Seller marks order as delivered",
)
async def mark_delivered(
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> QuotationOut:
    try:
        quote = await review_service.mark_quote_delivered(
            db, connection_id=connection_id, quote_id=quote_id, user_id=current_user.id
        )
    except ReviewError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return quotation_service.to_out(quote, viewer_id=current_user.id)


@router.post(
    "/connections/{connection_id}/quotes/{quote_id}/accept-delivery",
    response_model=QuotationOut,
    summary="Step 3: Buyer accepts order delivery",
)
async def accept_delivery(
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> QuotationOut:
    try:
        quote = await review_service.accept_quote_delivery(
            db, connection_id=connection_id, quote_id=quote_id, user_id=current_user.id
        )
    except ReviewError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return quotation_service.to_out(quote, viewer_id=current_user.id)


@router.post(
    "/connections/{connection_id}/quotes/{quote_id}/rate",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
    summary="Submit counterparty review and ratings",
)
async def rate_transaction(
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    payload: ReviewCreate,
    current_user: CurrentUser,
    db: DbSession,
) -> ReviewOut:
    try:
        review = await review_service.create_review(
            db,
            connection_id=connection_id,
            quote_id=quote_id,
            reviewer_id=current_user.id,
            payload=payload,
        )
    except ReviewError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return ReviewOut(
        id=review.id,
        quotation_id=review.quotation_id,
        connection_id=review.connection_id,
        reviewer_id=review.reviewer_id,
        reviewee_id=review.reviewee_id,
        rating=review.rating,
        communication_rating=review.communication_rating,
        delivery_rating=review.delivery_rating,
        quality_rating=review.quality_rating,
        comment=review.comment,
        created_at=review.created_at,
        reviewer_name=current_user.profile.name if current_user.profile else None,
        reviewer_company=current_user.profile.company_name if current_user.profile else None,
    )


@router.get(
    "/connections/{connection_id}/quotes/{quote_id}/reviews",
    response_model=list[ReviewOut],
    summary="Get reviews submitted for a specific quotation",
)
async def list_quote_reviews(
    connection_id: uuid.UUID,
    quote_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> list[ReviewOut]:
    return await review_service.list_quote_reviews(db, connection_id=connection_id, quote_id=quote_id)


@router.get(
    "/users/{user_id}/reviews",
    response_model=UserReviewStatsOut,
    summary="Get public ratings and reviews for a user or enterprise",
)
async def get_user_reviews(
    user_id: uuid.UUID,
    db: DbSession,
) -> UserReviewStatsOut:
    return await review_service.get_user_review_stats(db, user_id)

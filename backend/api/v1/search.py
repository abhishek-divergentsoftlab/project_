"""Direct search: conversational matching that creates no RFQ."""

from fastapi import APIRouter, HTTPException, status

from api.deps import CurrentUser, DbSession
from schemas.common import Money, Quantity
from schemas.match import DirectSearchRequest, DirectSearchResponse, RequirementsOut
from services import direct_search_service
from services.direct_search_service import QUESTION_ORDER, ConversationNotFound
from services.query_extractor import Requirements

router = APIRouter()


def _to_requirements_out(requirements: Requirements) -> RequirementsOut:
    quantity = (
        Quantity(value=requirements.quantity_value, unit=requirements.quantity_unit or "units")
        if requirements.quantity_value is not None
        else None
    )
    price = (
        Money(
            amount=requirements.price_amount,
            currency=requirements.price_currency or "INR",
            per_unit=requirements.price_per_unit,
        )
        if requirements.price_amount is not None
        else None
    )
    return RequirementsOut(
        role=requirements.role,
        product=requirements.product,
        category=requirements.category,
        attributes=requirements.attributes,
        quantity=quantity,
        price=price,
        city=requirements.city,
        state=requirements.state,
        country=requirements.country,
        deadline_days=requirements.deadline_days,
        skipped=requirements.skipped,
    )


@router.post("/search", response_model=DirectSearchResponse, tags=["search"])
async def direct_search(
    payload: DirectSearchRequest, current_user: CurrentUser, db: DbSession
) -> DirectSearchResponse:
    """Send one message. Returns the reply, what is understood, and the matches.

    Pass the returned ``conversation_id`` back on the next message to refine the
    same search instead of starting over.
    """
    try:
        outcome = await direct_search_service.handle_message(
            db, current_user, payload.message, conversation_id=payload.conversation_id
        )
    except ConversationNotFound as exc:
        # 404 rather than 403 for someone else's conversation: a 403 would
        # confirm the id exists.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "conversation not found"
        ) from exc
    except direct_search_service.ModerationSessionBlockedError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
        ) from exc

    requirements = outcome.requirements
    missing = [
        field
        for field in QUESTION_ORDER
        if not requirements.known(field) and field not in requirements.skipped
    ]

    return DirectSearchResponse(
        conversation_id=outcome.conversation.id,
        reply=outcome.reply,
        requirements=_to_requirements_out(requirements),
        pending_question=outcome.pending,
        missing=missing,
        results=outcome.results,
        total=outcome.total,
        search_id=outcome.search_id,
        blocked=outcome.blocked,
        block_reason=outcome.block_reason,
        block_category=outcome.block_category,
    )

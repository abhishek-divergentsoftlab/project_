"""Matching endpoints."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from api.deps import CurrentUser, DbSession
from models.enums import RFQStatus
from schemas.match import MatchResponse
from services import match_service, rfq_service

router = APIRouter()


@router.post("/rfqs/{rfq_id}/matches", response_model=MatchResponse, tags=["matching"])
async def match_rfq(
    rfq_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MatchResponse:
    """Find counterparties for one of your RFQs.

    A buyer RFQ returns sellers and a seller RFQ returns buyers; the caller does
    not choose the direction.
    """
    rfq = await rfq_service.get_rfq(db, rfq_id)
    # 404 rather than 403, to avoid confirming that the id exists.
    if rfq is None or rfq.user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFQ not found")

    if rfq.status is RFQStatus.DRAFT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "publish this RFQ before searching for counterparties",
        )
    # A closed or lapsed listing is off the market; matching it would record
    # searches and surface counterparties for a deal nobody can make.
    expires_at = rfq.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if rfq.status in (RFQStatus.CLOSED, RFQStatus.EXPIRED) or (
        expires_at is not None and expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this RFQ is closed or expired; reopen or repost it to search for counterparties",
        )

    return await match_service.match_for_rfq(
        db, current_user, rfq, limit=limit, offset=offset
    )

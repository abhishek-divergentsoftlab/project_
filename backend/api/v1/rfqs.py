"""RFQ CRUD.

Ownership is checked on every single-RFQ route. Matching will later expose
other people's RFQs through a search endpoint that returns a deliberately
narrower projection -- these routes only ever serve the caller's own rows.
"""

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Query, status

from api.deps import CurrentUser, DbSession
from models.enums import RFQStatus
from models.rfq import RFQ
from schemas.rfq import RFQCreate, RFQListOut, RFQOut, RFQUpdate
from schemas.marketplace import CatalogListOut
from schemas.connection import ConnectionOut
from services import connection_service, rfq_service, saved_rfq_service
from services.saved_rfq_service import SavedRFQNotFoundError
from services.rfq_service import RFQError

router = APIRouter()


async def _get_owned(db: DbSession, rfq_id: uuid.UUID, user_id: uuid.UUID) -> RFQ:
    rfq = await rfq_service.get_rfq(db, rfq_id)
    # 404 rather than 403 for someone else's RFQ: a 403 would confirm the id
    # exists, which is an enumeration oracle.
    if rfq is None or rfq.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFQ not found")
    return rfq


@router.post("", response_model=RFQOut, status_code=status.HTTP_201_CREATED)
async def create_rfq(
    payload: RFQCreate, current_user: CurrentUser, db: DbSession
) -> RFQOut:
    try:
        rfq = await rfq_service.create_rfq(db, current_user, payload)
    except RFQError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return rfq_service.to_out(rfq)


@router.get("", response_model=RFQListOut)
async def list_rfqs(
    current_user: CurrentUser,
    db: DbSession,
    status_filter: Annotated[Optional[RFQStatus], Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RFQListOut:
    rows, total = await rfq_service.list_rfqs(
        db, current_user.id, status=status_filter, limit=limit, offset=offset
    )
    
    pending_counts = await connection_service.get_rfq_pending_counts(db, current_user.id)
    
    return RFQListOut(
        items=[rfq_service.to_out(rfq, pending_connections=pending_counts.get(rfq.id, 0)) for rfq in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/saved/ids", response_model=list[str])
async def get_saved_rfq_ids(
    current_user: CurrentUser,
    db: DbSession,
) -> list[str]:
    """Return all RFQ IDs bookmarked by the current user."""
    ids = await saved_rfq_service.get_saved_rfq_ids(db, current_user.id)
    return [str(i) for i in ids]


@router.get("/saved", response_model=CatalogListOut)
async def list_saved_rfqs(
    current_user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CatalogListOut:
    """List all RFQs bookmarked by the current user for later review."""
    return await saved_rfq_service.list_saved_rfqs(
        db, current_user, limit=limit, offset=offset
    )


@router.get("/{rfq_id}", response_model=RFQOut)
async def get_rfq(rfq_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> RFQOut:
    rfq = await _get_owned(db, rfq_id, current_user.id)
    pending_counts = await connection_service.get_rfq_pending_counts(db, current_user.id)
    return rfq_service.to_out(rfq, pending_connections=pending_counts.get(rfq.id, 0))


@router.patch("/{rfq_id}", response_model=RFQOut)
async def update_rfq(
    rfq_id: uuid.UUID, payload: RFQUpdate, current_user: CurrentUser, db: DbSession
) -> RFQOut:
    rfq = await _get_owned(db, rfq_id, current_user.id)
    try:
        rfq = await rfq_service.update_rfq(db, rfq, payload)
    except RFQError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    pending_counts = await connection_service.get_rfq_pending_counts(db, current_user.id)
    return rfq_service.to_out(rfq, pending_connections=pending_counts.get(rfq.id, 0))


@router.post("/{rfq_id}/publish", response_model=RFQOut)
async def publish_rfq(
    rfq_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> RFQOut:
    """Move a draft to active, making it eligible for matching."""
    rfq = await _get_owned(db, rfq_id, current_user.id)
    pending_counts = await connection_service.get_rfq_pending_counts(db, current_user.id)
    if rfq.status is RFQStatus.ACTIVE:
        return rfq_service.to_out(rfq, pending_connections=pending_counts.get(rfq.id, 0))
    if rfq.status is not RFQStatus.DRAFT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"only a draft can be published; this RFQ is {rfq.status.value}",
        )
    rfq = await rfq_service.update_rfq(db, rfq, RFQUpdate(status=RFQStatus.ACTIVE))
    return rfq_service.to_out(rfq, pending_connections=pending_counts.get(rfq.id, 0))


@router.post("/{rfq_id}/close", response_model=RFQOut)
async def close_rfq(
    rfq_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> RFQOut:
    rfq = await _get_owned(db, rfq_id, current_user.id)
    try:
        rfq = await rfq_service.update_rfq(db, rfq, RFQUpdate(status=RFQStatus.CLOSED))
    except RFQError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    pending_counts = await connection_service.get_rfq_pending_counts(db, current_user.id)
    return rfq_service.to_out(rfq, pending_connections=pending_counts.get(rfq.id, 0))

@router.get("/{rfq_id}/connections", response_model=list[ConnectionOut])
async def list_rfq_connections(
    rfq_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> list[ConnectionOut]:
    """Who has asked to be put in touch about one of your listings."""
    rows = await connection_service.list_connections_for_rfq(
        db, rfq_id, current_user.id
    )
    return [connection_service.to_out(row, current_user.id) for row in rows]


@router.post("/{rfq_id}/save", status_code=status.HTTP_200_OK)
async def save_rfq(
    rfq_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> dict[str, str]:
    """Bookmark / save an RFQ to revisit later."""
    try:
        await saved_rfq_service.save_rfq(db, current_user.id, rfq_id)
    except SavedRFQNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFQ not found")
    return {"status": "saved", "rfq_id": str(rfq_id)}


@router.delete("/{rfq_id}/save", status_code=status.HTTP_200_OK)
async def unsave_rfq(
    rfq_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> dict[str, str]:
    """Remove an RFQ from the user's saved list."""
    await saved_rfq_service.unsave_rfq(db, current_user.id, rfq_id)
    return {"status": "unsaved", "rfq_id": str(rfq_id)}


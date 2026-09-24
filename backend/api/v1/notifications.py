"""Notification API endpoints."""

import uuid

from fastapi import APIRouter

from api.deps import CurrentUser, DbSession
from services import notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", summary="List notifications for the authenticated user")
async def list_notifications(
    current_user: CurrentUser,
    db: DbSession,
    limit: int = 30,
    offset: int = 0,
    unread_only: bool = False,
) -> dict:
    """Paginated notifications, newest first."""
    items, total = await notification_service.list_notifications(
        db, current_user.id, limit=min(limit, 100), offset=offset, unread_only=unread_only
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/unread-count", summary="Count of unread notifications")
async def unread_count(
    current_user: CurrentUser,
    db: DbSession,
) -> dict:
    count = await notification_service.unread_count(db, current_user.id)
    return {"count": count}


@router.post("/{notification_id}/read", summary="Mark a notification as read")
async def mark_read(
    notification_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> dict:
    found = await notification_service.mark_read(db, current_user.id, notification_id)
    return {"ok": found}


@router.post("/read-all", summary="Mark all notifications as read")
async def mark_all_read(
    current_user: CurrentUser,
    db: DbSession,
) -> dict:
    count = await notification_service.mark_all_read(db, current_user.id)
    return {"marked": count}

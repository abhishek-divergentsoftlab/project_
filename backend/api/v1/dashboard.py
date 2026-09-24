"""Dashboard API endpoints — aggregated business metrics and activity feed."""

from fastapi import APIRouter

from api.deps import CurrentUser, DbSession
from services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats", summary="Aggregated business metrics for the authenticated user")
async def dashboard_stats(
    current_user: CurrentUser,
    db: DbSession,
) -> dict:
    """RFQ counts, connection stats, quotation pipeline, profile completeness."""
    return await dashboard_service.get_dashboard_stats(db, current_user.id)


@router.get("/activity", summary="Chronological activity feed")
async def dashboard_activity(
    current_user: CurrentUser,
    db: DbSession,
    limit: int = 20,
) -> list[dict]:
    """Recent connections, quotations, messages — ordered by timestamp."""
    return await dashboard_service.get_activity_feed(db, current_user.id, limit=min(limit, 50))

"""Moderation pre-flight check endpoint for client-side validation."""

import asyncio

from fastapi import APIRouter, Depends

from api.deps import CurrentUser
from core.rate_limit import RateLimit
from schemas.moderation import ModerationCheckRequest, ModerationCheckResult
from services.moderation_service import AIContentModerator

router = APIRouter()


@router.post(
    "/check",
    response_model=ModerationCheckResult,
    summary="Check listing text against AI safety policies",
    dependencies=[Depends(RateLimit("moderation-check", "RATE_LIMIT_MODERATION", per="user"))],
)
async def check_content_safety(
    payload: ModerationCheckRequest,
    current_user: CurrentUser,
) -> ModerationCheckResult:
    full_text = f"{payload.title}\n{payload.description or ''}\n{payload.category or ''}"
    return await asyncio.to_thread(AIContentModerator.check_text, full_text)

"""Moderation pre-flight check endpoint for client-side validation."""

from fastapi import APIRouter

from schemas.moderation import ModerationCheckRequest, ModerationCheckResult
from services.moderation_service import AIContentModerator

router = APIRouter()


@router.post(
    "/check",
    response_model=ModerationCheckResult,
    summary="Check listing text against AI safety policies",
)
async def check_content_safety(
    payload: ModerationCheckRequest,
) -> ModerationCheckResult:
    full_text = f"{payload.title}\n{payload.description or ''}\n{payload.category or ''}"
    return AIContentModerator.check_text(full_text)

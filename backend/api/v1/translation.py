"""API endpoints for real-time Deal Room multilingual translation."""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status
from sqlalchemy import select

from api.deps import CurrentUser, DbSession
from models.connection import Connection
from schemas.translation import (
    BatchTranslationRequest,
    BatchTranslationResponse,
    LanguageInfo,
    TranslationRequest,
    TranslationResponse,
)
from services import translation_service

router = APIRouter(prefix="/translation", tags=["translation"])


@router.get("/languages", response_model=list[LanguageInfo])
async def get_supported_languages() -> list[LanguageInfo]:
    """Get list of supported languages for Deal Room real-time translation."""
    return translation_service.SUPPORTED_LANGUAGES


@router.post("/translate", response_model=TranslationResponse)
async def translate_text_endpoint(
    payload: TranslationRequest,
    current_user: CurrentUser,
) -> TranslationResponse:
    """Translate arbitrary commercial text or message draft before sending."""
    return await translation_service.translate_text(
        text=payload.text,
        target_language=payload.target_language,
        source_language=payload.source_language,
    )


@router.post("/batch", response_model=BatchTranslationResponse)
async def batch_translate_endpoint(
    payload: BatchTranslationRequest,
    current_user: CurrentUser,
) -> BatchTranslationResponse:
    """Translate multiple messages in a batch."""
    results = []
    for item_text in payload.texts:
        res = await translation_service.translate_text(
            text=item_text,
            target_language=payload.target_language,
            source_language=payload.source_language,
        )
        results.append(res)
    return BatchTranslationResponse(translations=results)


@router.post("/connections/{connection_id}/translate", response_model=TranslationResponse)
async def translate_connection_message(
    connection_id: Annotated[uuid.UUID, Path(description="Connection UUID")],
    payload: TranslationRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> TranslationResponse:
    """Translate a counterparty message within an active Deal Room.

    Verifies that the authenticated user is an authorized participant of the connection.
    """
    stmt = select(Connection).where(Connection.id == connection_id)
    result = await db.execute(stmt)
    conn = result.scalar_one_or_none()

    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deal Room connection not found",
        )

    if current_user.id not in (conn.sender_id, conn.receiver_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an authorized participant in this Deal Room",
        )

    return await translation_service.translate_text(
        text=payload.text,
        target_language=payload.target_language,
        source_language=payload.source_language,
    )

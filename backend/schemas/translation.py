"""Pydantic schemas for real-time Deal Room multilingual translation."""

from typing import Annotated, Optional

from pydantic import BaseModel, Field, field_validator

# Per-item cap, shared by the single and the batch endpoints: every item is a
# separate LLM call, so an unbounded batch item is a cheap way to tie up the
# translator.
MAX_TRANSLATION_CHARS = 5000
MAX_BATCH_ITEMS = 50


def _validate_language(value: Optional[str], *, allow_auto: bool) -> Optional[str]:
    if value is None:
        return None
    from services.translation_service import normalize_supported_language

    cleaned = value.strip().lower()
    if allow_auto and cleaned in ("", "auto"):
        return None
    code = normalize_supported_language(cleaned)
    if code is None:
        raise ValueError(f"Unsupported language '{value}'")
    return code


class LanguageInfo(BaseModel):
    code: str = Field(..., description="ISO 639-1 language code, e.g. 'en', 'es', 'zh'")
    name: str = Field(..., description="English display name of the language")
    native_name: str = Field(..., description="Native script display name of the language")
    flag: str = Field(..., description="Emoji flag representing language/region")


class TranslationRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TRANSLATION_CHARS, description="Text to translate")
    target_language: str = Field(..., min_length=2, max_length=10, description="Target language code, e.g. 'es', 'zh', 'hi'")
    source_language: Optional[str] = Field(None, max_length=10, description="Optional source language code if known ('auto' to detect)")

    @field_validator("target_language")
    @classmethod
    def _target_supported(cls, value: str) -> str:
        return _validate_language(value, allow_auto=False)  # type: ignore[return-value]

    @field_validator("source_language")
    @classmethod
    def _source_supported(cls, value: Optional[str]) -> Optional[str]:
        return _validate_language(value, allow_auto=True)


class TranslationResponse(BaseModel):
    original_text: str
    translated_text: str
    source_language: str
    target_language: str
    cached: bool = False


class BatchTranslationRequest(BaseModel):
    texts: list[Annotated[str, Field(max_length=MAX_TRANSLATION_CHARS)]] = Field(
        ..., min_length=1, max_length=MAX_BATCH_ITEMS, description="Batch of texts to translate"
    )
    target_language: str = Field(..., min_length=2, max_length=10)
    source_language: Optional[str] = Field(None, max_length=10)

    @field_validator("target_language")
    @classmethod
    def _target_supported(cls, value: str) -> str:
        return _validate_language(value, allow_auto=False)  # type: ignore[return-value]

    @field_validator("source_language")
    @classmethod
    def _source_supported(cls, value: Optional[str]) -> Optional[str]:
        return _validate_language(value, allow_auto=True)


class BatchTranslationResponse(BaseModel):
    translations: list[TranslationResponse]

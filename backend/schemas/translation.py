"""Pydantic schemas for real-time Deal Room multilingual translation."""

from typing import Optional
from pydantic import BaseModel, Field


class LanguageInfo(BaseModel):
    code: str = Field(..., description="ISO 639-1 language code, e.g. 'en', 'es', 'zh'")
    name: str = Field(..., description="English display name of the language")
    native_name: str = Field(..., description="Native script display name of the language")
    flag: str = Field(..., description="Emoji flag representing language/region")


class TranslationRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000, description="Text to translate")
    target_language: str = Field(..., min_length=2, max_length=10, description="Target language code, e.g. 'es', 'zh', 'hi'")
    source_language: Optional[str] = Field(None, max_length=10, description="Optional source language code if known")


class TranslationResponse(BaseModel):
    original_text: str
    translated_text: str
    source_language: str
    target_language: str
    cached: bool = False


class BatchTranslationRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=50, description="Batch of texts to translate")
    target_language: str = Field(..., min_length=2, max_length=10)
    source_language: Optional[str] = Field(None, max_length=10)


class BatchTranslationResponse(BaseModel):
    translations: list[TranslationResponse]

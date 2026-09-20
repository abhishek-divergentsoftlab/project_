"""Moderation check schemas."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ModerationCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=5000)
    category: Optional[str] = Field(default=None, max_length=120)


class ModerationCheckResult(BaseModel):
    is_safe: bool
    category: Optional[str] = None
    reason: Optional[str] = None
    flagged_terms: list[str] = []

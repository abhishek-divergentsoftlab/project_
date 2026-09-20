"""Review and rating schemas."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: int = Field(ge=1, le=5, description="Overall star rating from 1 to 5")
    communication_rating: Optional[int] = Field(default=None, ge=1, le=5)
    delivery_rating: Optional[int] = Field(default=None, ge=1, le=5)
    quality_rating: Optional[int] = Field(default=None, ge=1, le=5)
    comment: Optional[str] = Field(default=None, max_length=2000)


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    quotation_id: uuid.UUID
    connection_id: uuid.UUID
    reviewer_id: uuid.UUID
    reviewee_id: uuid.UUID
    rating: int
    communication_rating: Optional[int] = None
    delivery_rating: Optional[int] = None
    quality_rating: Optional[int] = None
    comment: Optional[str] = None
    created_at: datetime
    reviewer_name: Optional[str] = None
    reviewer_company: Optional[str] = None


class UserReviewStatsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    average_rating: float
    total_reviews: int
    rating_breakdown: dict[int, int]
    recent_reviews: list[ReviewOut]

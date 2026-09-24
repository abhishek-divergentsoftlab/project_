"""RFQ request and response bodies.

``product_details`` is deliberately an open dict: a cable, a dining table and a
pallet of apples do not share an attribute set, and requiring a migration per
product type would make the marketplace unusable.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from models.enums import EmbeddingStatus, RFQRole, RFQStatus
from schemas.common import Deadline, DeadlineOut, Location, Money, Quantity
from schemas.rfq_structure import RFQStructure


class RFQCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: RFQRole = Field(description="Which side of the market this RFQ sits on.")
    category: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    description: Optional[str] = None

    quantity: Optional[Quantity] = None
    minimum_order: Optional[Quantity] = None
    price_target: Optional[Money] = None
    location: Optional[Location] = None
    deadline: Optional[Deadline] = None

    product_details: dict[str, Any] = Field(default_factory=dict)

    # Drafts are invisible to matching; publish explicitly.
    status: RFQStatus = Field(
        default=RFQStatus.DRAFT,
        description="Only 'draft' or 'active' may be set at creation.",
    )


class RFQUpdate(BaseModel):
    """Every field optional -- only what is sent gets changed.

    Note that ``role`` is absent: flipping an RFQ from buy-side to sell-side
    would silently invert its matching direction, so it is immutable.
    """

    model_config = ConfigDict(extra="forbid")

    category: Optional[str] = Field(default=None, min_length=1, max_length=120)
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    description: Optional[str] = None
    quantity: Optional[Quantity] = None
    minimum_order: Optional[Quantity] = None
    price_target: Optional[Money] = None
    location: Optional[Location] = None
    deadline: Optional[Deadline] = None
    product_details: Optional[dict[str, Any]] = None
    status: Optional[RFQStatus] = None


class RFQOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    role: RFQRole
    status: RFQStatus

    category: str
    title: str
    description: Optional[str] = None

    quantity: Optional[Quantity] = None
    minimum_order: Optional[Quantity] = None
    price_target: Optional[Money] = None
    location: Optional[Location] = None
    deadline: Optional[DeadlineOut] = None

    product_details: dict[str, Any]
    search_tags: list[str]
    pending_connections: int = 0

    # Exposed so the UI can show "indexing..." rather than silently returning
    # an RFQ that is not yet findable.
    embedding_status: EmbeddingStatus

    expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class RFQListOut(BaseModel):
    items: list[RFQOut]
    total: int
    limit: int
    offset: int

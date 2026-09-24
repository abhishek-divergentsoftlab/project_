"""Marketplace catalog request and response schemas."""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from models.enums import KYCStatus, RFQRole
from schemas.common import DeadlineOut, Location, Money, Quantity


class CatalogCounterpartyOut(BaseModel):
    """Public counterparty projection for marketplace catalog.
    
    Privacy rule: Does NOT expose phone, email, or exact address until
    a connection request between these two accounts has been accepted.
    """

    model_config = ConfigDict(from_attributes=True)

    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    kyc_status: KYCStatus = KYCStatus.UNVERIFIED
    trust_score: int = Field(default=20, ge=0, le=100)
    verified_certs: list[str] = Field(default_factory=list)
    connection_id: Optional[uuid.UUID] = None
    connection_status: Optional[str] = None  # None | "pending" | "accepted" | "rejected"
    connection_direction: Optional[str] = None  # "sent" | "received" | None


class CatalogItemOut(BaseModel):
    """An individual RFQ card in the marketplace catalog."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    role: RFQRole
    category: str
    title: str
    description: Optional[str] = None
    quantity: Optional[Quantity] = None
    minimum_order: Optional[Quantity] = None
    price_target: Optional[Money] = None
    location: Optional[Location] = None
    deadline: Optional[DeadlineOut] = None
    product_details: dict[str, Any] = Field(default_factory=dict)
    search_tags: list[str] = Field(default_factory=list)
    created_at: datetime
    counterparty: CatalogCounterpartyOut
    distance_km: Optional[float] = None


class CatalogListOut(BaseModel):
    """Paginated catalog response."""

    items: list[CatalogItemOut]
    total: int
    limit: int
    offset: int


class CategoryCountOut(BaseModel):
    """Category listing count breakdown."""

    category: str
    total_count: int
    seller_count: int
    buyer_count: int

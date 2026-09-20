"""Pydantic schemas for B2B Quotations and Deal Room."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from models.enums import Incoterm, QuotationStatus


class QuotationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_price: Decimal = Field(..., gt=0, description="Quoted unit price")
    currency: str = Field("INR", min_length=3, max_length=3)
    quantity: Decimal = Field(..., gt=0, description="Quoted volume")
    quantity_unit: str = Field("pcs", min_length=1, max_length=32)
    lead_time_days: Optional[int] = Field(None, ge=1, le=365)
    incoterms: Optional[Incoterm] = None
    payment_terms: Optional[str] = Field(None, max_length=120)
    valid_days: Optional[int] = Field(14, ge=1, le=90, description="Validity window in days")
    notes: Optional[str] = Field(None, max_length=2000)


class QuotationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connection_id: uuid.UUID
    sender_id: uuid.UUID
    receiver_id: uuid.UUID
    rfq_id: uuid.UUID
    quote_number: str
    version: int
    status: QuotationStatus
    unit_price: Decimal
    currency: str
    quantity: Decimal
    quantity_unit: str
    total_amount: Decimal
    lead_time_days: Optional[int] = None
    incoterms: Optional[Incoterm] = None
    payment_terms: Optional[str] = None
    valid_until: Optional[datetime] = None
    notes: Optional[str] = None
    purchase_order_reference: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    is_sender: bool = False


class QuotationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(None, max_length=500)

"""Pydantic schemas for B2B Quotations and Deal Room."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.enums import Incoterm, QuotationStatus

# unit_price, quantity and total_amount are Numeric(18, 4): at most 14 digits
# before the decimal point. Bound the inputs and their product so an
# oversized quote is a 422, not a database overflow (500).
_NUMERIC_18_4_LIMIT = Decimal("99999999999999")  # 1e14 - 1
MAX_UNIT_PRICE = Decimal("1000000000000")  # 1e12
MAX_QUANTITY = Decimal("1000000000000")  # 1e12


class QuotationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_price: Decimal = Field(..., gt=0, le=MAX_UNIT_PRICE, allow_inf_nan=False, description="Quoted unit price")
    currency: str = Field("INR", min_length=3, max_length=3)
    quantity: Decimal = Field(..., gt=0, le=MAX_QUANTITY, allow_inf_nan=False, description="Quoted volume")
    quantity_unit: str = Field("pcs", min_length=1, max_length=32)
    lead_time_days: Optional[int] = Field(None, ge=1, le=365)
    incoterms: Optional[Incoterm] = None
    payment_terms: Optional[str] = Field(None, max_length=120)
    valid_days: Optional[int] = Field(14, ge=1, le=90, description="Validity window in days")
    notes: Optional[str] = Field(None, max_length=2000)

    @field_validator("currency")
    @classmethod
    def _supported_currency(cls, value: str) -> str:
        from services import currency as currency_service

        code = currency_service.normalize_currency(value)
        if not code or code not in currency_service.RATES:
            raise ValueError(f"Unsupported currency '{value}'")
        return code

    @model_validator(mode="after")
    def _total_fits_storage(self) -> "QuotationCreate":
        if self.unit_price * self.quantity > _NUMERIC_18_4_LIMIT:
            raise ValueError("Quotation total (unit_price x quantity) is too large")
        return self


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

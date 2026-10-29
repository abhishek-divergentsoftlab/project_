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
MIN_TOTAL_AMOUNT = Decimal("0.01")

# India's GST slabs, in percent.
GST_SLABS = (Decimal("0"), Decimal("0.1"), Decimal("0.25"), Decimal("1.5"), Decimal("3"), Decimal("5"),
             Decimal("12"), Decimal("18"), Decimal("28"))


def validate_gst_rate(value: Optional[Decimal]) -> Optional[Decimal]:
    if value is None:
        return None
    if value not in GST_SLABS:
        allowed = ", ".join(f"{s.normalize():f}" for s in GST_SLABS)
        raise ValueError(f"gst_rate must be one of India's GST slabs (percent): {allowed}")
    return value


class QuotationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # At most 4 decimal places (the column is Numeric(18, 4)): finer values
    # would be silently rounded, so a 0.00001 x 0.00001 quote became 0.0000.
    unit_price: Decimal = Field(
        ..., gt=0, le=MAX_UNIT_PRICE, decimal_places=4, allow_inf_nan=False, description="Quoted unit price"
    )
    # Omitted currency / unit default to the listing's own (filled in by the service).
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    quantity: Decimal = Field(
        ..., gt=0, le=MAX_QUANTITY, decimal_places=4, allow_inf_nan=False, description="Quoted volume"
    )
    quantity_unit: Optional[str] = Field(None, min_length=1, max_length=32)
    lead_time_days: Optional[int] = Field(None, ge=1, le=365)
    incoterms: Optional[Incoterm] = None
    payment_terms: Optional[str] = Field(None, max_length=120)
    valid_days: Optional[int] = Field(14, ge=1, le=90, description="Validity window in days")
    notes: Optional[str] = Field(None, max_length=2000)
    gst_rate: Optional[Decimal] = Field(None, allow_inf_nan=False, description="GST percent: 0, 0.1, 0.25, 1.5, 3, 5, 12, 18 or 28")

    @field_validator("gst_rate")
    @classmethod
    def _gst_slab(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        return validate_gst_rate(value)

    @field_validator("currency")
    @classmethod
    def _supported_currency(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        from services import currency as currency_service

        code = currency_service.normalize_currency(value)
        if not code or code not in currency_service.RATES:
            raise ValueError(f"Unsupported currency '{value}'")
        return code

    @model_validator(mode="after")
    def _total_fits_storage(self) -> "QuotationCreate":
        if self.unit_price * self.quantity > _NUMERIC_18_4_LIMIT:
            raise ValueError("Quotation total (unit_price x quantity) is too large")
        if self.unit_price * self.quantity < MIN_TOTAL_AMOUNT:
            raise ValueError(f"Quotation total (unit_price x quantity) must be at least {MIN_TOTAL_AMOUNT}")
        return self


class ConvertedAmount(BaseModel):
    """A quote's price restated in the RFQ's currency (FX rates are indicative)."""

    currency: str
    unit_price: Decimal
    total_amount: Decimal
    rate: Decimal


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
    gst_rate: Optional[Decimal] = None
    lead_time_days: Optional[int] = None
    incoterms: Optional[Incoterm] = None
    payment_terms: Optional[str] = None
    valid_until: Optional[datetime] = None
    notes: Optional[str] = None
    purchase_order_reference: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    is_sender: bool = False
    # Listing context (only filled where the RFQ is known).
    rfq_currency: Optional[str] = None
    rfq_unit: Optional[str] = None
    # Amount in the RFQ's currency when it differs from the quote's; null if the
    # rate is unavailable (or the currencies are the same).
    converted: Optional[ConvertedAmount] = None
    # Set on accept: quantity already awarded on the RFQ across accepted-or-later quotes.
    over_award: Optional[bool] = None
    awarded_quantity: Optional[Decimal] = None
    rfq_quantity: Optional[Decimal] = None


class QuotationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(None, max_length=500)

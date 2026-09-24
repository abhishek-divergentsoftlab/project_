"""Logistics and Freight schemas for rating, booking, tracking, and waybill generation."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field


class TrackingEventOut(BaseModel):
    status: str
    location: str
    timestamp: str
    note: Optional[str] = None


class FreightRateOption(BaseModel):
    mode_id: str
    mode_name: str
    carrier_sample: str
    rate_amount: Decimal
    currency: str
    transit_days_min: int
    transit_days_max: int
    chargeable_weight_kg: Decimal
    is_recommended: bool = False
    description: str


class IncotermCostBreakdown(BaseModel):
    incoterm: str
    seller_pays: list[str]
    buyer_pays: list[str]
    seller_estimated_cost: Decimal
    buyer_estimated_cost: Decimal
    currency: str
    risk_transfer_point: str


class FreightEstimateRequest(BaseModel):
    origin_city: str = Field(..., min_length=1)
    origin_country: str = Field(default="India")
    destination_city: str = Field(..., min_length=1)
    destination_country: str = Field(default="India")
    weight_kg: Decimal = Field(..., gt=0)
    volume_cbm: Optional[Decimal] = Field(default=None, ge=0)
    length_cm: Optional[Decimal] = Field(default=None, ge=0)
    width_cm: Optional[Decimal] = Field(default=None, ge=0)
    height_cm: Optional[Decimal] = Field(default=None, ge=0)
    cargo_value: Optional[Decimal] = Field(default=None, ge=0)
    currency: str = Field(default="USD", max_length=3)
    incoterm: Optional[str] = Field(default="FOB")


class FreightEstimateResponse(BaseModel):
    origin: str
    destination: str
    distance_km: float
    is_cross_border: bool
    gross_weight_kg: Decimal
    volumetric_weight_kg: Decimal
    chargeable_weight_kg: Decimal
    volume_cbm: Decimal
    currency: str
    rate_options: list[FreightRateOption]
    incoterm_breakdown: IncotermCostBreakdown


class ShipmentCreatePayload(BaseModel):
    carrier_name: str = Field(..., min_length=1, max_length=120)
    carrier_service: Optional[str] = Field(default=None, max_length=120)
    shipping_mode: str = Field(default="road")
    tracking_number: Optional[str] = Field(default=None, max_length=64)
    bill_of_lading_number: Optional[str] = Field(default=None, max_length=100)
    origin_address: Optional[str] = None
    origin_city: Optional[str] = None
    origin_country: Optional[str] = None
    destination_address: Optional[str] = None
    destination_city: Optional[str] = None
    destination_country: Optional[str] = None
    weight_kg: Optional[Decimal] = Field(default=None, ge=0)
    volume_cbm: Optional[Decimal] = Field(default=None, ge=0)
    package_count: int = Field(default=1, ge=1)
    package_type: str = Field(default="Boxes", max_length=60)
    estimated_delivery_days: Optional[int] = Field(default=5, ge=1, le=120)
    dispatch_note: Optional[str] = None
    trigger_escrow_milestone: bool = True


class ShipmentStatusUpdatePayload(BaseModel):
    status: str = Field(..., max_length=32)
    location: str = Field(..., max_length=120)
    note: Optional[str] = None


class ShipmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connection_id: uuid.UUID
    quotation_id: Optional[uuid.UUID] = None
    sender_id: uuid.UUID
    receiver_id: uuid.UUID
    tracking_number: str
    carrier_name: str
    carrier_service: Optional[str] = None
    shipping_mode: str
    status: str
    origin_city: str
    origin_state: Optional[str] = None
    origin_country: str
    origin_address: Optional[str] = None
    destination_city: str
    destination_state: Optional[str] = None
    destination_country: str
    destination_address: Optional[str] = None
    weight_kg: Decimal
    volume_cbm: Optional[Decimal] = None
    package_count: int
    package_type: str
    bill_of_lading_number: Optional[str] = None
    estimated_delivery_date: Optional[datetime] = None
    dispatched_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    tracking_events: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

"""Logistics and Freight schemas for rating, booking, tracking, and waybill generation."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TrackingEventOut(BaseModel):
    status: str
    location: str
    timestamp: str
    note: Optional[str] = None


class FreightRateOption(BaseModel):
    mode_id: str
    mode: Optional[str] = None
    mode_name: str
    mode_label: Optional[str] = None
    carrier_sample: str = ""
    rate_amount: Decimal
    total_estimated_usd: Optional[Decimal] = None
    base_freight_usd: Optional[Decimal] = None
    fuel_surcharge_usd: Optional[Decimal] = None
    documentation_fee_usd: Optional[Decimal] = None
    customs_clearance_usd: Optional[Decimal] = None
    basis: Optional[str] = None
    currency: str = "USD"
    transit_days_min: int
    transit_days_max: int
    chargeable_weight_kg: Decimal
    is_recommended: bool = False
    recommended: Optional[bool] = None
    description: str = ""

    @model_validator(mode="after")
    def populate_mirrors(self) -> "FreightRateOption":
        if self.mode is None:
            self.mode = self.mode_id.replace("_freight", "").replace("_cargo", "").replace("express_", "")
        if self.mode_label is None:
            self.mode_label = self.mode_name
        if self.total_estimated_usd is None:
            self.total_estimated_usd = self.rate_amount
        if self.recommended is None:
            self.recommended = self.is_recommended
        return self


class IncotermCostBreakdown(BaseModel):
    incoterm: str
    full_name: Optional[str] = None
    seller_pays: list[str] = Field(default_factory=list)
    buyer_pays: list[str] = Field(default_factory=list)
    seller_responsibility: Optional[str] = None
    buyer_responsibility: Optional[str] = None
    seller_estimated_cost: Decimal
    buyer_estimated_cost: Decimal
    estimated_seller_logistics_usd: Optional[Decimal] = None
    estimated_buyer_logistics_usd: Optional[Decimal] = None
    currency: str = "USD"
    risk_transfer_point: str

    @model_validator(mode="after")
    def populate_mirrors(self) -> "IncotermCostBreakdown":
        if self.seller_responsibility is None:
            self.seller_responsibility = "; ".join(self.seller_pays) if self.seller_pays else "None"
        if self.buyer_responsibility is None:
            self.buyer_responsibility = "; ".join(self.buyer_pays) if self.buyer_pays else "None"
        if self.estimated_seller_logistics_usd is None:
            self.estimated_seller_logistics_usd = self.seller_estimated_cost
        if self.estimated_buyer_logistics_usd is None:
            self.estimated_buyer_logistics_usd = self.buyer_estimated_cost
        return self


class FreightEstimateRequest(BaseModel):
    origin_city: Optional[str] = Field(default=None)
    origin_country: str = Field(default="India")
    destination_city: Optional[str] = Field(default=None)
    destination_country: str = Field(default="India")
    weight_kg: Decimal = Field(..., gt=0)
    gross_weight_kg: Optional[Decimal] = Field(default=None, gt=0)
    volume_cbm: Optional[Decimal] = Field(default=None, ge=0)
    cbm: Optional[Decimal] = Field(default=None, ge=0)
    length_cm: Optional[Decimal] = Field(default=None, ge=0)
    width_cm: Optional[Decimal] = Field(default=None, ge=0)
    height_cm: Optional[Decimal] = Field(default=None, ge=0)
    cargo_value: Optional[Decimal] = Field(default=None, ge=0)
    currency: str = Field(default="USD", max_length=3)
    incoterm: Optional[str] = Field(default="FOB")

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Normalize weight_kg / gross_weight_kg
            if data.get("weight_kg") is None:
                if data.get("gross_weight_kg") is not None:
                    data["weight_kg"] = data["gross_weight_kg"]
                elif data.get("weight") is not None:
                    data["weight_kg"] = data["weight"]
            elif data.get("gross_weight_kg") is None:
                data["gross_weight_kg"] = data.get("weight_kg")

            # Normalize volume_cbm / cbm
            if data.get("volume_cbm") is None and data.get("cbm") is not None:
                data["volume_cbm"] = data["cbm"]
            elif data.get("cbm") is None and data.get("volume_cbm") is not None:
                data["cbm"] = data.get("volume_cbm")

            # Sensible fallbacks for origin_city and destination_city
            if not data.get("origin_city"):
                country = str(data.get("origin_country") or "").upper().strip()
                data["origin_city"] = "Mumbai" if country in ("IN", "INDIA") else (data.get("origin_country") or "Origin City")
            if not data.get("destination_city"):
                country = str(data.get("destination_country") or "").upper().strip()
                data["destination_city"] = "Rotterdam" if country in ("NL", "NETHERLANDS") else (data.get("destination_country") or "Destination City")
        return data


class FreightEstimateResponse(BaseModel):
    origin: str
    destination: str
    distance_km: float
    is_cross_border: bool
    gross_weight_kg: Decimal
    volumetric_weight_kg: Decimal
    chargeable_weight_kg: Decimal
    volume_cbm: Decimal
    cbm: Optional[Decimal] = None
    currency: str
    rate_options: list[FreightRateOption]
    rates: list[FreightRateOption] = Field(default_factory=list)
    incoterm_breakdown: IncotermCostBreakdown

    @model_validator(mode="after")
    def populate_mirrors(self) -> "FreightEstimateResponse":
        if self.cbm is None:
            self.cbm = self.volume_cbm
        if not self.rates:
            self.rates = self.rate_options
        return self


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
    gross_weight_kg: Optional[Decimal] = Field(default=None, ge=0)
    volume_cbm: Optional[Decimal] = Field(default=None, ge=0)
    cbm: Optional[Decimal] = Field(default=None, ge=0)
    package_count: int = Field(default=1, ge=1)
    packages_count: Optional[int] = Field(default=None, ge=1)
    package_type: str = Field(default="Boxes", max_length=60)
    estimated_delivery_days: Optional[int] = Field(default=5, ge=1, le=120)
    dispatch_note: Optional[str] = None
    trigger_escrow_milestone: bool = True

    @model_validator(mode="before")
    @classmethod
    def normalize_shipment_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get("weight_kg") is None and data.get("gross_weight_kg") is not None:
                data["weight_kg"] = data["gross_weight_kg"]
            elif data.get("gross_weight_kg") is None and data.get("weight_kg") is not None:
                data["gross_weight_kg"] = data.get("weight_kg")

            if data.get("volume_cbm") is None and data.get("cbm") is not None:
                data["volume_cbm"] = data["cbm"]
            elif data.get("cbm") is None and data.get("volume_cbm") is not None:
                data["cbm"] = data.get("volume_cbm")

            if data.get("package_count") is None and data.get("packages_count") is not None:
                data["package_count"] = data["packages_count"]
            elif data.get("packages_count") is None and data.get("package_count") is not None:
                data["packages_count"] = data.get("package_count")
        return data


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
    gross_weight_kg: Optional[Decimal] = None
    volume_cbm: Optional[Decimal] = None
    cbm: Optional[Decimal] = None
    package_count: int
    packages_count: Optional[int] = None
    package_type: str
    bill_of_lading_number: Optional[str] = None
    estimated_delivery_date: Optional[datetime] = None
    estimated_delivery: Optional[str] = None
    dispatched_at: Optional[datetime] = None
    actual_dispatch_date: Optional[str] = None
    delivered_at: Optional[datetime] = None
    tracking_events: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def populate_mirrors(self) -> "ShipmentOut":
        if self.gross_weight_kg is None:
            self.gross_weight_kg = self.weight_kg
        if self.cbm is None:
            self.cbm = self.volume_cbm
        if self.packages_count is None:
            self.packages_count = self.package_count
        if self.estimated_delivery is None and self.estimated_delivery_date:
            self.estimated_delivery = self.estimated_delivery_date.isoformat()
        if self.actual_dispatch_date is None and self.dispatched_at:
            self.actual_dispatch_date = self.dispatched_at.isoformat()
        return self

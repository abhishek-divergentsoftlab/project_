"""User and profile responses."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_serializer

from models.enums import KYCStatus, UserRole, UserStatus
from schemas.common import normalise_decimal


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    company_name: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=32)
    address: Optional[str] = Field(default=None, max_length=400)
    city: Optional[str] = Field(default=None, max_length=120)
    state: Optional[str] = Field(default=None, max_length=120)
    country: Optional[str] = Field(default=None, max_length=120)
    latitude: Optional[Decimal] = Field(default=None, ge=-90, le=90)
    longitude: Optional[Decimal] = Field(default=None, ge=-180, le=180)

    # KYC & Anti-Fraud fields
    gst_number: Optional[str] = Field(default=None, max_length=20)
    legal_business_name: Optional[str] = Field(default=None, max_length=200)
    business_type: Optional[str] = Field(default=None, max_length=60)
    registration_number: Optional[str] = Field(default=None, max_length=100)
    year_established: Optional[int] = Field(default=None, ge=1800, le=2100)
    website: Optional[str] = Field(default=None, max_length=255)
    pan_number: Optional[str] = Field(default=None, max_length=20)
    signatory_name: Optional[str] = Field(default=None, max_length=120)


class AccountUpdate(BaseModel):
    """The parts of an account that are not profile fields."""

    model_config = ConfigDict(extra="forbid")

    # An account that signed up to buy and later wants to sell had no way to say
    # so, and `can_post_as` refuses the other side outright -- so the only route
    # was a second account.
    role: UserRole


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    company_name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None

    gst_number: Optional[str] = None
    legal_business_name: Optional[str] = None
    business_type: Optional[str] = None
    registration_number: Optional[str] = None
    year_established: Optional[int] = None
    website: Optional[str] = None
    pan_number: Optional[str] = None
    signatory_name: Optional[str] = None
    kyc_status: KYCStatus = KYCStatus.UNVERIFIED
    trust_score: int = 20

    @field_serializer("latitude", "longitude")
    def _serialise_coord(self, value: Optional[Decimal]) -> int | float | None:
        return normalise_decimal(value)


class UserOut(BaseModel):
    """Never carries password_hash -- the field does not exist on this model."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    role: UserRole
    status: UserStatus
    created_at: datetime
    profile: Optional[ProfileOut] = None

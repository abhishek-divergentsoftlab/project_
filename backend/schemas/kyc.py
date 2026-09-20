"""KYC and Anti-Fraud Verification schemas."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from models.enums import KYCStatus


class KYCVerificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gst_number: str = Field(min_length=5, max_length=20, description="GSTIN or Tax ID")
    legal_business_name: str = Field(min_length=2, max_length=200)
    business_type: str = Field(min_length=2, max_length=60, description="e.g. Private Limited, LLC, Proprietorship")
    registration_number: Optional[str] = Field(default=None, max_length=100)
    year_established: Optional[int] = Field(default=None, ge=1800, le=2100)
    website: Optional[str] = Field(default=None, max_length=255)
    pan_number: Optional[str] = Field(default=None, max_length=20)
    signatory_name: Optional[str] = Field(default=None, max_length=120)


class KYCStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kyc_status: KYCStatus
    trust_score: int
    gst_number: Optional[str] = None
    legal_business_name: Optional[str] = None
    message: str

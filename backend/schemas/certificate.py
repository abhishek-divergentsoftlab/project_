"""Certificate schemas."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from models.enums import CertificationStatus


class CertificateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=120, description="Certificate name, e.g. ISO 9001:2015, CE Mark")
    issuing_body: str = Field(min_length=2, max_length=160, description="Issuing organization, e.g. SGS, TÜV")
    certificate_number: str = Field(min_length=2, max_length=100)
    issue_date: datetime
    expiry_date: Optional[datetime] = None
    document_url: Optional[str] = Field(default=None, max_length=500)


class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    issuing_body: str
    certificate_number: str
    issue_date: datetime
    expiry_date: Optional[datetime] = None
    document_url: Optional[str] = None
    verification_status: CertificationStatus
    created_at: datetime
 
 
class CertificateDocumentUploadOut(BaseModel):
    document_url: str = Field(description="Relative media URL of the uploaded certificate document")
    filename: str = Field(description="Original sanitized filename")
    file_size: int = Field(description="Size of uploaded file in bytes")

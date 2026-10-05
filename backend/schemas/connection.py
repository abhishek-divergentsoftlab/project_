"""Connection request and message bodies.

A connection is the consent step between two accounts. Until the receiver
accepts, neither side gets the other's email or phone -- that rule is carried by
``Counterparty``, which this module reuses from the match schemas so a card and
a connection row can never disagree about what is visible.
"""

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.enums import ConnectionStatus, RFQRole
from schemas.match import Counterparty


class ConnectionMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bounded on the way in: an unbounded Text column reachable from an
    # authenticated POST is a cheap way to fill the disk.
    content: str = Field(min_length=1, max_length=4000)
    image_url: Optional[str] = Field(default=None, max_length=512)
    is_live_capture: bool = Field(default=False)

    @field_validator("is_live_capture")
    @classmethod
    def reject_client_live_capture(cls, v: bool) -> bool:
        if v:
            raise ValueError("is_live_capture cannot be set via client JSON payload; use live capture upload endpoint")
        return False

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v_clean = v.strip()
        if not v_clean:
            return None
        lower = v_clean.lower()
        if lower.startswith("javascript:") or lower.startswith("data:") or lower.startswith("vbscript:"):
            raise ValueError("Unsafe image_url scheme")
        if not (lower.startswith("http://") or lower.startswith("https://") or lower.startswith("/api/v1/media/")):
            raise ValueError("image_url must be an HTTP/HTTPS URL or /api/v1/media/ path")
        return v_clean


class ConnectionMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connection_id: uuid.UUID
    sender_id: uuid.UUID
    content: str
    image_url: Optional[str] = None
    is_live_capture: bool = False
    created_at: datetime


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rfq_id: uuid.UUID


class ConnectionOut(BaseModel):
    id: uuid.UUID
    rfq_id: uuid.UUID
    sender_id: uuid.UUID
    receiver_id: uuid.UUID
    status: ConnectionStatus
    created_at: datetime
    updated_at: datetime

    # Which way this request went, from the caller's point of view. The UI needs
    # it to decide whether to offer Accept/Reject or "waiting for approval", and
    # deriving it from sender_id in the browser invited off-by-one mistakes.
    direction: Literal["sent", "received"]

    # Enough about the listing to recognise the thread without a second call.
    rfq_title: Optional[str] = None
    rfq_role: Optional[RFQRole] = None

    counterparty: Counterparty
    message_count: int = 0
    last_message_at: Optional[datetime] = None

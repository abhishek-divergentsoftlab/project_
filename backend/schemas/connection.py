"""Connection request and message bodies.

A connection is the consent step between two accounts. Until the receiver
accepts, neither side gets the other's email or phone -- that rule is carried by
``Counterparty``, which this module reuses from the match schemas so a card and
a connection row can never disagree about what is visible.
"""

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from models.enums import ConnectionStatus, RFQRole
from schemas.match import Counterparty


class ConnectionMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bounded on the way in: an unbounded Text column reachable from an
    # authenticated POST is a cheap way to fill the disk.
    content: str = Field(min_length=1, max_length=4000)


class ConnectionMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connection_id: uuid.UUID
    sender_id: uuid.UUID
    content: str
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

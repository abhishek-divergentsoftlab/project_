"""Match request and response bodies.

These describe *someone else's* RFQ, so the projection is deliberately narrower
than RFQOut: enough to evaluate the offer, and contact details only once the
counterparty has agreed to be contacted.

The unlock rule lives in ``Counterparty``. Email and phone are populated only
when a connection between the two accounts has been accepted, which is what the
connection request flow is for -- publishing every seller's phone number to
every signed-up account would make the request step pointless and would turn the
listing table into a scrapeable lead list.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from models.enums import ConnectionStatus, RFQRole
from schemas.common import DeadlineOut, Location, Money, Quantity


class MatchScore(BaseModel):
    """Per-dimension breakdown, so a match can be explained rather than asserted.

    A dimension is null when it could not be evaluated -- one side left the
    field empty, units were incomparable, or the currencies differed.
    """

    total: float = Field(ge=0, le=1)
    relevance: Optional[float] = Field(
        default=None,
        description="Textual similarity. Term overlap today; vector similarity once Qdrant lands.",
    )
    category: Optional[float] = None
    attributes: Optional[float] = Field(
        default=None, description="How well the candidate matches the attributes you named."
    )
    price: Optional[float] = None
    quantity: Optional[float] = None
    location: Optional[float] = None
    deadline: Optional[float] = None


class Counterparty(BaseModel):
    """Who is on the other side, and how much of them you may see.

    ``connection_status`` is this viewer's standing with them:

    * ``None``      -- no request has been sent yet
    * ``pending``   -- sent, awaiting their answer
    * ``accepted``  -- they agreed; ``email``/``phone``/``contact_name`` are filled
    * ``rejected``  -- they declined; contact stays hidden
    """

    company_name: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    member_since: Optional[datetime] = None

    connection_id: Optional[uuid.UUID] = None
    connection_status: Optional[ConnectionStatus] = None

    # Populated only when connection_status is ACCEPTED.
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None

    # Public enterprise trust & reputation fields
    average_rating: Optional[float] = None
    total_reviews: int = 0
    trust_score: Optional[int] = None
    gst_verified: bool = False

    @property
    def contact_unlocked(self) -> bool:
        return self.connection_status is ConnectionStatus.ACCEPTED


class MatchCandidate(BaseModel):
    rfq_id: uuid.UUID
    role: RFQRole
    rank: int

    title: str
    category: str
    description: Optional[str] = None

    quantity: Optional[Quantity] = None
    price: Optional[Money] = None
    location: Optional[Location] = None
    deadline: Optional[DeadlineOut] = None

    product_details: dict[str, Any]
    search_tags: list[str]

    # Great-circle km between the two listings, when both are geocoded. The
    # location score alone left "73%" unexplainable on the card.
    distance_km: Optional[float] = None
    logistics: Optional[dict[str, Any]] = None

    counterparty: Counterparty
    score: MatchScore



class MatchResponse(BaseModel):
    search_id: uuid.UUID
    requester_role: RFQRole
    # Always the counterpart of requester_role: buyers are shown sellers.
    target_role: RFQRole

    results: list[MatchCandidate]
    total: int = Field(description="Total viable candidates, not just this page.")
    limit: int
    offset: int

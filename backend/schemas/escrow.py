"""Pydantic schemas for Escrow accounts, milestone payments, and deal disputes."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Funding is simulated (there is no payment gateway yet), but it must at least
# name a real payment rail and carry the payer's reference (UTR, card auth id,
# LC number...) so the vault is never "secured" by an arbitrary string.
PaymentMethod = Literal["bank_transfer", "upi", "card", "letter_of_credit"]

DisputeCategory = Literal[
    "quality",
    "non_delivery",
    "delay",
    "delivery",
    "specification_mismatch",
    "packaging",
    "payment",
    "documentation",
    "other",
]
DisputeSeverity = Literal["low", "medium", "high", "critical"]
DisputeResolution = Literal["release_funds", "refund_buyer", "mutual_settlement"]


class EscrowMilestoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    escrow_account_id: uuid.UUID
    title: str
    percentage: Decimal
    amount: Decimal
    order_index: int
    status: str
    released_at: Optional[datetime] = None
    release_note: Optional[str] = None
    created_at: datetime


class DealDisputeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connection_id: uuid.UUID
    escrow_account_id: Optional[uuid.UUID] = None
    raised_by_id: uuid.UUID
    title: str
    category: str
    reason: str
    severity: str
    status: str
    suggested_resolution: Optional[str] = None
    resolution_notes: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime


class EscrowAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connection_id: uuid.UUID
    quotation_id: uuid.UUID
    buyer_id: uuid.UUID
    seller_id: uuid.UUID
    currency: str
    total_amount: Decimal
    funded_amount: Decimal
    released_amount: Decimal
    refunded_amount: Decimal
    status: str
    milestones: list[EscrowMilestoneOut] = []
    disputes: list[DealDisputeOut] = []
    created_at: datetime
    updated_at: datetime


class EscrowDepositPayload(BaseModel):
    payment_method: PaymentMethod = Field(
        ..., description="Payment rail: bank_transfer, upi, card, letter_of_credit"
    )
    payment_reference: str = Field(
        ..., min_length=1, max_length=120,
        description="Payer's transaction reference (UTR / card auth id / LC number)",
    )
    notes: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("payment_reference")
    @classmethod
    def _reference_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("payment_reference must not be blank")
        return cleaned


class MilestoneReleaseRequestPayload(BaseModel):
    proof_note: Optional[str] = Field(default=None, max_length=1000, description="Notes, tracking numbers, or verification details from seller")


class MilestoneReleaseApprovePayload(BaseModel):
    note: Optional[str] = Field(default=None, max_length=1000, description="Approval or release acceptance note from buyer")


class DealDisputeCreatePayload(BaseModel):
    title: str = Field(..., min_length=3, max_length=255, description="Dispute summary")
    category: DisputeCategory = Field(default="quality", description="Dispute category")
    reason: str = Field(..., min_length=10, max_length=4000, description="Detailed explanation of the grievance or breach")
    severity: DisputeSeverity = Field(default="medium", description="low, medium, high, critical")
    suggested_resolution: Optional[str] = Field(default=None, max_length=2000)


class DealDisputeResolvePayload(BaseModel):
    # Concession rule (enforced in the service): release_funds may only be
    # chosen by the buyer, refund_buyer only by the seller -- the side giving
    # up money -- and mutual_settlement by either.
    resolution: DisputeResolution = Field(..., description="release_funds, refund_buyer, mutual_settlement")
    resolution_notes: str = Field(..., min_length=5, max_length=2000, description="Settlement explanation agreed by counterparties")

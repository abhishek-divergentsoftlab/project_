"""Pydantic schemas for Escrow accounts, milestone payments, and deal disputes."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# There is no payment gateway yet: the buyer REPORTS a deposit (rail + payer
# reference: UTR, card auth id, LC number...) and an operator confirms it. A
# reported deposit never counts as funded.
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
    # Two-step mutual settlement: who proposed it and the proposed split.
    settlement_proposed_by_id: Optional[uuid.UUID] = None
    settlement_release_percent: Optional[Decimal] = None
    settlement_proposed_at: Optional[datetime] = None
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
    # pending_deposit | deposit_reported | funded | partially_released | completed | disputed | refunded
    status: str
    deposit_method: Optional[str] = None
    deposit_reference: Optional[str] = None
    deposit_reported_at: Optional[datetime] = None
    deposit_confirmed_at: Optional[datetime] = None
    deposit_rejection_reason: Optional[str] = None
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
    # mutual_settlement only: share (0-100) of the remaining vault balance paid to the seller;
    # the rest is refunded. Omit to resume the deal unchanged.
    release_percent: Optional[Decimal] = Field(default=None, ge=0, le=100, decimal_places=2)

    @model_validator(mode="after")
    def _percent_only_for_settlement(self) -> "DealDisputeResolvePayload":
        if self.release_percent is not None and self.resolution != "mutual_settlement":
            raise ValueError("release_percent applies only to mutual_settlement")
        return self


class EscrowDepositRejectPayload(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500, description="Why the reported payment was not confirmed")


class EscrowDepositConfirmPayload(BaseModel):
    note: Optional[str] = Field(default=None, max_length=500)

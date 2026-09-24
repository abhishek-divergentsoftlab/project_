"""Pydantic schemas for Escrow accounts, milestone payments, and deal disputes."""

from datetime import datetime
from decimal import Decimal
from typing import Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field


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
    payment_method: str = Field(default="mock_instant", description="Payment method: mock_instant, wire_transfer, lc")
    notes: Optional[str] = Field(default=None, max_length=1000)


class MilestoneReleaseRequestPayload(BaseModel):
    proof_note: Optional[str] = Field(default=None, max_length=1000, description="Notes, tracking numbers, or verification details from seller")


class MilestoneReleaseApprovePayload(BaseModel):
    note: Optional[str] = Field(default=None, max_length=1000, description="Approval or release acceptance note from buyer")


class DealDisputeCreatePayload(BaseModel):
    title: str = Field(..., min_length=3, max_length=255, description="Dispute summary")
    category: str = Field(default="quality", description="quality, non_delivery, delay, specification_mismatch, payment")
    reason: str = Field(..., min_length=10, max_length=4000, description="Detailed explanation of the grievance or breach")
    severity: str = Field(default="medium", description="low, medium, high, critical")
    suggested_resolution: Optional[str] = Field(default=None, max_length=2000)


class DealDisputeResolvePayload(BaseModel):
    resolution: str = Field(..., description="release_funds, refund_buyer, mutual_settlement")
    resolution_notes: str = Field(..., min_length=5, max_length=2000, description="Settlement explanation agreed by counterparties")

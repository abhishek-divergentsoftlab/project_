"""Escrow and Dispute models: Milestone escrow vaults, fund release stages, and formal dispute settlement."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.connection import Connection
    from models.quotation import Quotation
    from models.user import User


class EscrowAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "escrow_accounts"
    __table_args__ = (
        Index("ix_escrow_accounts_connection_id", "connection_id"),
        Index("ix_escrow_accounts_quotation_id", "quotation_id"),
        Index("ix_escrow_accounts_buyer_id", "buyer_id"),
        Index("ix_escrow_accounts_seller_id", "seller_id"),
        Index("ix_escrow_accounts_status", "status"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    quotation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False
    )
    buyer_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    seller_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    funded_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=Decimal("0.00"))
    released_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=Decimal("0.00"))
    refunded_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=Decimal("0.00"))

    # status: pending_deposit, funded, partially_released, completed, disputed, refunded
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_deposit")

    connection: Mapped["Connection"] = relationship()
    quotation: Mapped["Quotation"] = relationship()
    buyer: Mapped["User"] = relationship(foreign_keys=[buyer_id])
    seller: Mapped["User"] = relationship(foreign_keys=[seller_id])
    milestones: Mapped[list["EscrowMilestone"]] = relationship(
        back_populates="escrow_account",
        cascade="all, delete-orphan",
        order_by="EscrowMilestone.order_index",
        lazy="selectin",
    )
    disputes: Mapped[list["DealDispute"]] = relationship(
        back_populates="escrow_account",
        cascade="all, delete-orphan",
        order_by="DealDispute.created_at.desc()",
        lazy="selectin",
    )


class EscrowMilestone(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "escrow_milestones"
    __table_args__ = (
        Index("ix_escrow_milestones_escrow_id", "escrow_account_id"),
        Index("ix_escrow_milestones_status", "status"),
    )

    escrow_account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("escrow_accounts.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    percentage: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # status: pending, funded, release_requested, released, disputed, refunded
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    release_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    escrow_account: Mapped["EscrowAccount"] = relationship(back_populates="milestones")


class DealDispute(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "deal_disputes"
    __table_args__ = (
        Index("ix_deal_disputes_connection_id", "connection_id"),
        Index("ix_deal_disputes_escrow_account_id", "escrow_account_id"),
        Index("ix_deal_disputes_status", "status"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    escrow_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("escrow_accounts.id", ondelete="CASCADE"), nullable=True
    )
    raised_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="quality")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="medium")

    # status: open, under_review, resolved_release, resolved_refund, cancelled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    suggested_resolution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    connection: Mapped["Connection"] = relationship()
    escrow_account: Mapped[Optional["EscrowAccount"]] = relationship(back_populates="disputes")
    raised_by: Mapped["User"] = relationship()

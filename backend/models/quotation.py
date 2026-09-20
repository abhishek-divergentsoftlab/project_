"""Quotation model: structured B2B pricing, terms, and counter-offers."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from models.enums import Incoterm, QuotationStatus

if TYPE_CHECKING:
    from models.connection import Connection
    from models.rfq import RFQ
    from models.user import User


class Quotation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "quotations"
    __table_args__ = (
        Index("ix_quotations_connection_created", "connection_id", "created_at"),
        Index("ix_quotations_sender_id", "sender_id"),
        Index("ix_quotations_receiver_id", "receiver_id"),
        Index("ix_quotations_status", "status"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    receiver_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    rfq_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="CASCADE"), nullable=False
    )

    quote_number: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[QuotationStatus] = mapped_column(
        Enum(
            QuotationStatus,
            name="quotation_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=QuotationStatus.PENDING,
    )

    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    quantity_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="pcs")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)

    lead_time_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    incoterms: Mapped[Optional[Incoterm]] = mapped_column(
        Enum(
            Incoterm,
            name="incoterm",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=True,
    )
    payment_terms: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Generated upon acceptance
    purchase_order_reference: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    connection: Mapped["Connection"] = relationship("Connection", lazy="noload")
    sender: Mapped["User"] = relationship("User", foreign_keys=[sender_id], lazy="noload")
    receiver: Mapped["User"] = relationship("User", foreign_keys=[receiver_id], lazy="noload")
    rfq: Mapped["RFQ"] = relationship("RFQ", lazy="noload")

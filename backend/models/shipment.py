"""Shipment and Logistics models: Carrier dispatch, multi-modal tracking, and waybill records."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.connection import Connection
    from models.quotation import Quotation
    from models.user import User


class Shipment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "shipments"
    __table_args__ = (
        Index("ix_shipments_connection_id", "connection_id"),
        Index("ix_shipments_tracking_number", "tracking_number", unique=True),
        Index("ix_shipments_sender_id", "sender_id"),
        Index("ix_shipments_receiver_id", "receiver_id"),
        Index("ix_shipments_status", "status"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    quotation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("quotations.id", ondelete="SET NULL"), nullable=True
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    receiver_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    tracking_number: Mapped[str] = mapped_column(String(64), nullable=False)
    carrier_name: Mapped[str] = mapped_column(String(120), nullable=False)
    carrier_service: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    shipping_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="road")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="booked")

    origin_city: Mapped[str] = mapped_column(String(120), nullable=False)
    origin_state: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    origin_country: Mapped[str] = mapped_column(String(120), nullable=False)
    origin_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    destination_city: Mapped[str] = mapped_column(String(120), nullable=False)
    destination_state: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    destination_country: Mapped[str] = mapped_column(String(120), nullable=False)
    destination_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    weight_kg: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0.000"))
    volume_cbm: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 3), nullable=True)
    package_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    package_type: Mapped[str] = mapped_column(String(60), nullable=False, default="Boxes")

    bill_of_lading_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    estimated_delivery_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    tracking_events: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    connection: Mapped["Connection"] = relationship()
    quotation: Mapped[Optional["Quotation"]] = relationship()
    sender: Mapped["User"] = relationship(foreign_keys=[sender_id])
    receiver: Mapped["User"] = relationship(foreign_keys=[receiver_id])

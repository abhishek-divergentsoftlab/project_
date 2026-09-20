import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from models.enums import ConnectionStatus

if TYPE_CHECKING:
    from models.rfq import RFQ
    from models.user import User


class Connection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "connections"
    __table_args__ = (
        Index("ix_connections_rfq_id", "rfq_id"),
        Index("ix_connections_sender_id", "sender_id"),
        Index("ix_connections_receiver_id", "receiver_id"),
        # One request per person per listing. Enforced here rather than by the
        # service's read-then-insert, which two clicks in flight can both pass.
        UniqueConstraint("sender_id", "rfq_id", name="uq_connections_sender_rfq"),
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
    status: Mapped[ConnectionStatus] = mapped_column(
        Enum(
            ConnectionStatus,
            name="connection_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ConnectionStatus.PENDING,
    )

    # All three are noload by default: the matching hot path reads thousands of
    # rows and must never trigger a per-row join. Callers that need them ask
    # with selectinload.
    rfq: Mapped["RFQ"] = relationship("RFQ", lazy="noload")
    sender: Mapped["User"] = relationship(
        "User", foreign_keys=[sender_id], lazy="noload"
    )
    receiver: Mapped["User"] = relationship(
        "User", foreign_keys=[receiver_id], lazy="noload"
    )

    messages: Mapped[list["ConnectionMessage"]] = relationship(
        back_populates="connection",
        cascade="all, delete-orphan",
        order_by="ConnectionMessage.created_at",
        lazy="noload",
    )


class ConnectionMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "connection_messages"
    __table_args__ = (Index("ix_connection_messages_connection_created", "connection_id", "created_at"),)

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    is_live_capture: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    connection: Mapped["Connection"] = relationship(back_populates="messages")

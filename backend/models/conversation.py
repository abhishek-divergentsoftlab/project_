"""Chat persistence: conversations, messages, and the stream events per message.

``message_events`` is the durable record of what was streamed to the client --
status lines, tool calls, tool results, result payloads. Replaying a
conversation from the database reproduces exactly what the user saw, which
matters for debugging an agent you cannot step through.
"""

import uuid
from typing import Any, Optional

from sqlalchemy import (
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from models.enums import ConversationType, MessageRole


class Conversation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_updated", "user_id", text("updated_at DESC")),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[ConversationType] = mapped_column(
        Enum(
            ConversationType,
            name="conversation_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    # Everything understood so far in this conversation: the accumulated search
    # requirements, which fields the user declined to give, and the id of the
    # last search run. Direct search has no RFQ to hang this on, and each turn
    # refines the previous picture rather than restarting it.
    state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # Set once an onboarding conversation produces an RFQ. Null for direct
    # search, which is allowed to end without creating one.
    rfq_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="SET NULL"), nullable=True
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="noload",
    )


class Message(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(
            MessageRole,
            name="message_role",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    events: Mapped[list["MessageEvent"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="MessageEvent.sequence",
        lazy="noload",
    )


class MessageEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "message_events"
    __table_args__ = (
        # Sequence numbers are per message, so a reconnecting client can resume
        # from the last one it saw.
        UniqueConstraint("message_id", "sequence", name="uq_message_events_seq"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    # Intentionally a plain string, not a PG enum: the event protocol is meant
    # to gain new types without a migration.
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ``metadata`` is reserved by SQLAlchemy's declarative API, so the attribute
    # is renamed while the column keeps the name the protocol uses.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    message: Mapped["Message"] = relationship(back_populates="events")

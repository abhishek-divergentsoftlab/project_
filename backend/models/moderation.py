"""Moderation log model: tracks AI detection of prohibited and illegal items."""

import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.user import User


class ModerationLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "moderation_logs"
    __table_args__ = (
        Index("ix_moderation_logs_user_id", "user_id"),
        Index("ix_moderation_logs_category", "category"),
    )

    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "rfq_create", "rfq_update"
    category: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "firearms_and_weapons"
    flagged_terms: Mapped[str] = mapped_column(String(255), nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped[Optional["User"]] = relationship("User", lazy="noload")

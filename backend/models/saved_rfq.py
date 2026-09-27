"""Saved / Bookmarked RFQs model for buyers and sellers to bookmark listings for later access."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.rfq import RFQ
    from models.user import User


class SavedRFQ(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "saved_rfqs"
    __table_args__ = (
        UniqueConstraint("user_id", "rfq_id", name="uq_user_saved_rfq"),
        Index("ix_saved_rfqs_user_id", "user_id"),
        Index("ix_saved_rfqs_rfq_id", "rfq_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    rfq_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("rfqs.id", ondelete="CASCADE"),
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="saved_rfqs", lazy="noload")
    rfq: Mapped["RFQ"] = relationship(back_populates="saved_by", lazy="selectin")

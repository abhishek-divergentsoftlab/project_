"""Review and rating model for post-delivery counterparty evaluation."""

import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.connection import Connection
    from models.quotation import Quotation
    from models.user import User


class Review(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "reviews"
    __table_args__ = (
        Index("ix_reviews_reviewee_id", "reviewee_id"),
        Index("ix_reviews_reviewer_id", "reviewer_id"),
        Index("ix_reviews_quotation_id", "quotation_id"),
        # Enforce exactly one review per counterparty per delivered quotation
        UniqueConstraint("quotation_id", "reviewer_id", name="uq_review_quotation_reviewer"),
    )

    quotation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reviewee_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    communication_rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    delivery_rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    quality_rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    quotation: Mapped["Quotation"] = relationship("Quotation", lazy="noload")
    connection: Mapped["Connection"] = relationship("Connection", lazy="noload")
    reviewer: Mapped["User"] = relationship("User", foreign_keys=[reviewer_id], lazy="noload")
    reviewee: Mapped["User"] = relationship("User", foreign_keys=[reviewee_id], lazy="noload")

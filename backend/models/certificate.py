"""Seller certification model: ISO, CE, FDA, GMP, and compliance badges."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from models.enums import CertificationStatus

if TYPE_CHECKING:
    from models.user import User


class Certificate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "certificates"
    __table_args__ = (
        Index("ix_certificates_user_id", "user_id"),
        Index("ix_certificates_verification_status", "verification_status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)  # e.g. "ISO 9001:2015", "CE Mark"
    issuing_body: Mapped[str] = mapped_column(String(160), nullable=False)  # e.g. "SGS", "TÜV"
    certificate_number: Mapped[str] = mapped_column(String(100), nullable=False)
    issue_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expiry_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    document_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    verification_status: Mapped[CertificationStatus] = mapped_column(
        Enum(
            CertificationStatus,
            name="certification_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=CertificationStatus.VERIFIED,
    )

    user: Mapped["User"] = relationship("User", lazy="noload")

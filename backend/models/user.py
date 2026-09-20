"""Account and profile tables.

Credentials live on ``users``; everything a counterparty might see lives on
``user_profiles``. Keeping them apart means profile reads during matching never
touch the password hash.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from models.enums import KYCStatus, UserRole, UserStatus

if TYPE_CHECKING:
    from models.rfq import RFQ


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (Index("uq_users_email", "email", unique=True),)

    # Emails are lowercased by the auth layer before they ever reach here, so a
    # plain unique index is effectively case-insensitive and no citext
    # extension or functional index is needed.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=UserRole.BUYER,
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(
            UserStatus,
            name="user_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=UserStatus.ACTIVE,
    )

    email_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    profile: Mapped[Optional["UserProfile"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    rfqs: Mapped[list["RFQ"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="noload"
    )

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE

    def can_post_as(self, rfq_role: "object") -> bool:
        """A ``BOTH`` account may post on either side; others only their own."""
        if self.role is UserRole.BOTH:
            return True
        return self.role.value == getattr(rfq_role, "value", rfq_role)


class UserProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    company_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    address: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    state: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    # Numeric rather than float: coordinates are compared and stored exactly.
    latitude: Mapped[Optional[float]] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Numeric(9, 6), nullable=True)

    # Company Verification & Anti-Fraud (KYC)
    gst_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    legal_business_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    business_type: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    registration_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    year_established: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    website: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    pan_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    signatory_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    kyc_status: Mapped[KYCStatus] = mapped_column(
        Enum(KYCStatus, name="kyc_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=KYCStatus.UNVERIFIED,
        server_default="unverified",
    )
    trust_score: Mapped[int] = mapped_column(Integer, nullable=False, default=20, server_default="20")
    average_rating: Mapped[Optional[Decimal]] = mapped_column(Numeric(3, 2), nullable=True, default=None)
    total_reviews: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    user: Mapped["User"] = relationship(back_populates="profile")

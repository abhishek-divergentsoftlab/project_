"""The RFQ table -- the source of truth for everything that gets matched.

Three representations of the same request live side by side, per the data
strategy:

* typed columns      -- for hard filters and business-rule matching
* ``product_details`` -- JSONB, so a cable, a dining table and a pallet of
  apples can each carry their own attributes without a migration
* ``search_text`` / ``search_tags`` -- the canonical rendering that the
  embedding is generated from, and the keyword aid beside it

``embedding_status`` records how far this row has propagated to Qdrant, so a
failed index write is retryable and the collection is always rebuildable from
PostgreSQL alone.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from models.enums import EmbeddingStatus, RFQRole, RFQStatus

if TYPE_CHECKING:
    from models.user import User


def _pg_enum(enum_cls: type, name: str) -> Enum:
    return Enum(
        enum_cls, name=name, values_callable=lambda e: [m.value for m in e]
    )


class RFQ(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "rfqs"
    __table_args__ = (
        # The matching hot path: "active RFQs on the opposite side of the market".
        Index("ix_rfqs_role_status", "role", "status"),
        Index("ix_rfqs_category_status", "category", "status"),
        Index("ix_rfqs_user_created", "user_id", text("created_at DESC")),
        # Attribute lookups inside the dynamic payload.
        Index("ix_rfqs_product_details", "product_details", postgresql_using="gin"),
        Index("ix_rfqs_search_tags", "search_tags", postgresql_using="gin"),
        # Worker queues: what still needs indexing, and what needs expiring.
        Index("ix_rfqs_embedding_status", "embedding_status"),
        Index("ix_rfqs_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    role: Mapped[RFQRole] = mapped_column(_pg_enum(RFQRole, "rfq_role"), nullable=False)
    status: Mapped[RFQStatus] = mapped_column(
        _pg_enum(RFQStatus, "rfq_status"), nullable=False, default=RFQStatus.DRAFT
    )

    category: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- quantity ------------------------------------------------------------
    quantity_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    quantity_unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    min_order_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    min_order_unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # --- price ---------------------------------------------------------------
    price_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    price_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    # The unit the price is quoted *per* -- "kg" in "Rs 200/kg". Often but not
    # always equal to quantity_unit.
    price_per_unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # --- location ------------------------------------------------------------
    location_city: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    location_state: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    location_country: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    latitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 6), nullable=True)
    # What the user actually typed, kept for audit and re-geocoding.
    location_raw: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    # --- deadline ------------------------------------------------------------
    # Resolved to an absolute instant at write time: "within 3 days" is
    # meaningless once the row is a week old.
    deadline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deadline_raw: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)

    # When the listing itself stops being matchable. Distinct from deadline_at,
    # which is a delivery requirement.
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- flexible payload ----------------------------------------------------
    product_details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    search_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    search_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- index convergence ---------------------------------------------------
    embedding_status: Mapped[EmbeddingStatus] = mapped_column(
        _pg_enum(EmbeddingStatus, "embedding_status"),
        nullable=False,
        default=EmbeddingStatus.PENDING,
    )
    embedded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    embedding_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="rfqs", lazy="selectin")

    @property
    def is_matchable(self) -> bool:
        """Only active, unexpired rows may appear in anyone's results."""
        if self.status is not RFQStatus.ACTIVE:
            return False
        now = datetime.now(UTC)
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return True

    def mark_for_reindex(self) -> None:
        """Call after any edit that changes what the embedding should encode."""
        self.embedding_status = EmbeddingStatus.STALE
        self.embedding_error = None

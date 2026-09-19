"""Search sessions and their results.

A ``MatchSearch`` is the durable handle for one search: it remembers the
filters, which RFQ ids have already been shown, and how far the cursor has
advanced. "Show me 20 more" continues from there instead of re-returning the
first page, and "only organic" refines the same session rather than starting a
new one.
"""

import uuid
from typing import Any, Optional

from sqlalchemy import (
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from models.enums import RFQRole


def _role_enum(name: str) -> Enum:
    return Enum(RFQRole, name=name, values_callable=lambda e: [m.value for m in e])


class MatchSearch(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "match_searches"
    __table_args__ = (Index("ix_match_searches_user_created", "user_id", text("created_at DESC")),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Null when the search came from the direct-search page without an RFQ
    # attached -- the distinction the whole second journey rests on.
    rfq_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    query: Mapped[str] = mapped_column(Text, nullable=False)
    # Lets a refinement be recognised as the same logical search.
    query_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    requester_role: Mapped[RFQRole] = mapped_column(
        _role_enum("match_requester_role"), nullable=False
    )
    # Always the counterpart of requester_role. Stored explicitly so a result
    # set can be audited without re-deriving the rule.
    target_role: Mapped[RFQRole] = mapped_column(
        _role_enum("match_target_role"), nullable=False
    )

    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    shown_rfq_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)),
        nullable=False,
        default=list,
        server_default=text("'{}'::uuid[]"),
    )
    last_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    results: Mapped[list["MatchResult"]] = relationship(
        back_populates="search",
        cascade="all, delete-orphan",
        order_by="MatchResult.rank",
        lazy="selectin",
    )


class MatchResult(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "match_results"
    __table_args__ = (
        UniqueConstraint("search_id", "rfq_id", name="uq_match_results_search_rfq"),
        Index("ix_match_results_search_rank", "search_id", "rank"),
    )

    search_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("match_searches.id", ondelete="CASCADE"),
        nullable=False,
    )
    rfq_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="CASCADE"), nullable=False
    )

    # Final rank after reranking; ``score`` is the blended score behind it.
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    semantic_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Per-dimension breakdown (price, quantity, location, deadline...) so a
    # match can be explained to the user rather than asserted.
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    search: Mapped["MatchSearch"] = relationship(back_populates="results")

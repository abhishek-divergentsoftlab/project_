"""The spec catalogue: one row per product family, listing the spec names (and their known values) RFQs use.

Specs written by the chat, the RFQ form and read by matching all go through the same catalogue, so
"Apple Variety", "variety" and "product type" are one spec called ``variety``. A family's attributes are
stored as JSONB (see ``services/spec_catalog.py`` for the attribute shape); ``source`` says where the row
came from: ``llm`` (generated once by the attribute recommender), ``seed`` or ``admin``.
"""

from typing import Any, Optional

from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SpecCatalog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "spec_catalog"

    family_key: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    hsn_heading: Mapped[Optional[str]] = mapped_column(String(4), index=True, nullable=True)
    attributes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="llm", server_default="llm")

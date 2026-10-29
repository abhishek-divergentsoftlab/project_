"""RFQ request and response bodies.

``product_details`` is deliberately an open dict: a cable, a dining table and a
pallet of apples do not share an attribute set, and requiring a migration per
product type would make the marketplace unusable.
"""

import json
import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Any, Optional, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_serializer, field_validator, model_validator

from models.enums import EmbeddingStatus, RFQRole, RFQStatus
from schemas.common import Deadline, DeadlineOut, Location, Money, NoNulModel, Quantity, clean_user_text, strip_markup
from schemas.rfq_structure import RFQStructure

# Surrounding whitespace is stripped *before* the length check, so a title of
# "     " is rejected instead of stored as a blank listing.
def _strip_format_chars(text: str) -> str:
    """Drop Cf characters, except ZWNJ/ZWJ (U+200C/U+200D) when the text has Indic letters (U+0900-U+0DFF):
    those scripts need them for correct conjuncts and half-forms."""
    keep_joiners = any("\u0900" <= ch <= "\u0dff" and ch.isalpha() for ch in text)
    return "".join(
        ch
        for ch in text
        if unicodedata.category(ch) != "Cf" or (keep_joiners and ch in ("\u200c", "\u200d"))
    )


MIN_TITLE_CHARS = 2


def _is_junk_title(text: str) -> bool:
    """One character repeated ("aa", "0000", "xxxx"). A two-letter UPPERCASE code ("PP", "CC") is a real product code."""
    chars = [ch for ch in text if ch.isalnum()]
    if len(set(chars)) != 1:
        return False
    return not (len(chars) == 2 and chars[0].isalpha() and chars[0].isupper())


def clean_title(value: str) -> str:
    """Canonical form of a listing title.

    NFKC-normalised, every Unicode "format" character (category Cf: zero-width
    spaces, joiners (kept only next to Indic letters), bidi marks and overrides U+202A-202E / U+2066-2069, BOM,
    soft hyphen...) removed, whitespace collapsed, and at least one letter or
    digit required -- so an invisible-only or RTL-spoofed title is refused.
    """
    text = unicodedata.normalize("NFKC", value)
    text = _strip_format_chars(text)
    text = " ".join(strip_markup(text).split())
    if not any(ch.isalnum() for ch in text):
        raise ValueError("title must contain at least one letter or digit")
    if sum(1 for ch in text if ch.isalnum()) < MIN_TITLE_CHARS:
        raise ValueError(f"title must have at least {MIN_TITLE_CHARS} meaningful characters (describe the product)")
    if _is_junk_title(text):
        raise ValueError("title must describe the product, not repeat one character")
    return text


def public_product_details(details: Any) -> Any:
    """Output view of product_details: internal "_"-prefixed bookkeeping keys (_canon, _canon_pending, _hsn_pending...)
    are dropped. Storage is never changed."""
    if not isinstance(details, dict):
        return details
    return {k: v for k, v in details.items() if not (isinstance(k, str) and k.startswith("_"))}


def title_key(value: Optional[str]) -> str:
    """Comparison key for duplicate detection: cleaned and case-folded."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = _strip_format_chars(text)
    return " ".join(text.split()).casefold()


def _clean_title_field(value: Optional[str]) -> Optional[str]:
    return None if value is None else clean_title(value)


TitleStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
CategoryStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]

DESCRIPTION_MAX_CHARS = 5000
PRODUCT_DETAILS_MAX_KEYS = 50
PRODUCT_DETAILS_MAX_KEY_CHARS = 100
PRODUCT_DETAILS_MAX_VALUE_CHARS = 500
PRODUCT_DETAILS_MAX_BYTES = 10_000


def _check_product_details_value(value: Any, path: str) -> None:
    if isinstance(value, str):
        if len(value) > PRODUCT_DETAILS_MAX_VALUE_CHARS:
            raise ValueError(
                f"product_details.{path} is longer than {PRODUCT_DETAILS_MAX_VALUE_CHARS} characters"
            )
    elif isinstance(value, dict):
        for key, inner in value.items():
            _check_product_details_value(inner, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, inner in enumerate(value):
            _check_product_details_value(inner, f"{path}[{index}]")
    elif isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError(f"product_details.{path} must be a finite number")


def validate_product_details(details: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Bound the open attribute dict so it cannot bloat JSONB and search_text."""
    if details is None:
        return None
    if len(details) > PRODUCT_DETAILS_MAX_KEYS:
        raise ValueError(f"product_details may have at most {PRODUCT_DETAILS_MAX_KEYS} keys")
    for key, value in details.items():
        if len(str(key)) > PRODUCT_DETAILS_MAX_KEY_CHARS:
            raise ValueError(
                f"product_details keys may be at most {PRODUCT_DETAILS_MAX_KEY_CHARS} characters"
            )
        _check_product_details_value(value, str(key))
    try:
        size = len(json.dumps(details, default=str, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("product_details must be JSON-serialisable") from exc
    if size > PRODUCT_DETAILS_MAX_BYTES:
        raise ValueError(f"product_details may be at most {PRODUCT_DETAILS_MAX_BYTES} bytes of JSON")
    return clean_user_text(details)


def _clean_description(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = strip_markup(value)
    return cleaned or None


class RFQCreate(NoNulModel):
    model_config = ConfigDict(extra="forbid")

    role: RFQRole = Field(description="Which side of the market this RFQ sits on.")
    category: CategoryStr
    title: TitleStr
    description: Optional[str] = Field(default=None, max_length=DESCRIPTION_MAX_CHARS)

    quantity: Optional[Quantity] = None
    minimum_order: Optional[Quantity] = None
    price_target: Optional[Money] = None
    location: Optional[Location] = None
    deadline: Optional[Deadline] = None

    product_details: dict[str, Any] = Field(default_factory=dict)

    # Drafts are invisible to matching; publish explicitly.
    status: RFQStatus = Field(
        default=RFQStatus.DRAFT,
        description="Only 'draft' or 'active' may be set at creation.",
    )

    @field_validator("title")
    @classmethod
    def _normalise_title(cls, value: str) -> str:
        return clean_title(value)

    @field_validator("description")
    @classmethod
    def _plain_description(cls, value: Optional[str]) -> Optional[str]:
        return _clean_description(value)

    @field_validator("product_details")
    @classmethod
    def _bound_product_details(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_product_details(value) or {}

    @model_validator(mode="after")
    def _creatable_status(self) -> Self:
        if self.status not in (RFQStatus.DRAFT, RFQStatus.ACTIVE):
            raise ValueError("a new RFQ may only be created as 'draft' or 'active'")
        return self


class RFQPatch(NoNulModel):
    """Body of ``PATCH /rfqs/{id}``. Every field optional -- only what is sent
    gets changed.

    ``role`` is absent: flipping an RFQ from buy-side to sell-side would
    silently invert its matching direction, so it is immutable.

    ``status`` is absent too: lifecycle changes go through the explicit
    transitions ``POST /rfqs/{id}/publish`` (draft -> active) and
    ``POST /rfqs/{id}/close`` (draft/active -> closed). Sending ``status`` is
    rejected with 422 (``extra="forbid"``), so PATCH can no longer reopen a
    closed listing or fake an expiry.
    """

    model_config = ConfigDict(extra="forbid")

    category: Optional[CategoryStr] = None
    title: Optional[TitleStr] = None
    description: Optional[str] = Field(default=None, max_length=DESCRIPTION_MAX_CHARS)
    quantity: Optional[Quantity] = None
    minimum_order: Optional[Quantity] = None
    price_target: Optional[Money] = None
    location: Optional[Location] = None
    deadline: Optional[Deadline] = None
    product_details: Optional[dict[str, Any]] = None

    @field_validator("title")
    @classmethod
    def _normalise_title(cls, value: Optional[str]) -> Optional[str]:
        return _clean_title_field(value)

    @field_validator("description")
    @classmethod
    def _plain_description(cls, value: Optional[str]) -> Optional[str]:
        return _clean_description(value)

    @field_validator("product_details")
    @classmethod
    def _bound_product_details(cls, value: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        return validate_product_details(value)


class RFQUpdate(RFQPatch):
    """Internal update object (not an API body).

    Kept with an optional ``status`` for backwards compatibility with in-process
    callers such as the AI chat's close tool (``RFQUpdate(status=CLOSED)``).
    ``rfq_service.update_rfq`` routes a status through the same transition
    table as ``/publish`` and ``/close``; it is never set directly.
    """

    status: Optional[RFQStatus] = None


class RFQOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    role: RFQRole
    status: RFQStatus

    category: str
    title: str
    description: Optional[str] = None

    quantity: Optional[Quantity] = None
    minimum_order: Optional[Quantity] = None
    price_target: Optional[Money] = None
    location: Optional[Location] = None
    deadline: Optional[DeadlineOut] = None

    product_details: dict[str, Any]
    search_tags: list[str]
    pending_connections: int = 0

    # Exposed so the UI can show "indexing..." rather than silently returning
    # an RFQ that is not yet findable.
    embedding_status: EmbeddingStatus

    expires_at: Optional[datetime] = None
    is_saved: bool = False
    created_at: datetime
    updated_at: datetime

    @field_serializer("product_details")
    def _hide_internal_details(self, value: dict[str, Any]) -> dict[str, Any]:
        return public_product_details(value)



class RFQListOut(BaseModel):
    items: list[RFQOut]
    total: int
    limit: int
    offset: int


class SuggestedAttribute(BaseModel):
    key: str
    label: str
    placeholder: str
    input_type: str = "text"  # "numeric", "text", "select"
    options: list[str] = Field(default_factory=list)
    explanation: Optional[str] = None
    must_match: bool = False
    # Catalogue fields: ``key`` is the canonical spec name; ``unit`` is the canonical unit of a numeric spec,
    # ``multi`` marks a spec that takes several of its options (certifications), ``synonyms`` are other names for it.
    unit: Optional[str] = None
    multi: bool = False
    synonyms: list[str] = Field(default_factory=list)


class SuggestedAttributesResponse(BaseModel):
    product_name: str
    category: Optional[str] = None
    suggested_attributes: list[SuggestedAttribute]
    source: str = "llm"  # "llm", "expert_rules", "category_defaults"


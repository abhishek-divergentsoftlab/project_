"""RFQ request and response bodies.

``product_details`` is deliberately an open dict: a cable, a dining table and a
pallet of apples do not share an attribute set, and requiring a migration per
product type would make the marketplace unusable.
"""

import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Optional, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from models.enums import EmbeddingStatus, RFQRole, RFQStatus
from schemas.common import Deadline, DeadlineOut, Location, Money, NoNulModel, Quantity
from schemas.rfq_structure import RFQStructure

# Surrounding whitespace is stripped *before* the length check, so a title of
# "     " is rejected instead of stored as a blank listing.
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
    return details


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


class RFQListOut(BaseModel):
    items: list[RFQOut]
    total: int
    limit: int
    offset: int

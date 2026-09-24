"""Pydantic schema representing the exact RFQ structure without title and notes.

Includes explicit required and non-required flags specified in both Pydantic
model fields and json_schema_extra metadata.
"""

from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from models.enums import RFQRole, RFQStatus
from schemas.common import Deadline, Location, Money, Quantity


class RFQStructure(BaseModel):
    """RFQ representation with exact marketplace structure, excluding title and notes.

    Each field explicitly defines its requirement status via Pydantic Field
    definitions, descriptions, and json_schema_extra flags.
    """

    model_config = ConfigDict(extra="ignore")

    # =========================================================================
    # REQUIRED FIELDS
    # =========================================================================
    role: RFQRole = Field(
        ...,
        description="[REQUIRED] Market side: 'buyer' (seeking goods) or 'seller' (offering goods).",
        json_schema_extra={"required": True, "is_required": True},
    )

    category: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="[REQUIRED] Industry product category (e.g. Packaging, Electronics, Agriculture, Furniture, Textiles).",
        json_schema_extra={"required": True, "is_required": True},
    )

    # =========================================================================
    # NON-REQUIRED (OPTIONAL) FIELDS
    # =========================================================================
    description: Optional[str] = Field(
        default=None,
        description="[NOT REQUIRED] Detailed requirements, specifications, or terms.",
        json_schema_extra={"required": False, "is_required": False},
    )

    quantity: Optional[Quantity] = Field(
        default=None,
        description="[NOT REQUIRED] Quantity required or available with value and unit.",
        json_schema_extra={"required": False, "is_required": False},
    )

    minimum_order: Optional[Quantity] = Field(
        default=None,
        description="[NOT REQUIRED] Minimum order quantity (MOQ) threshold.",
        json_schema_extra={"required": False, "is_required": False},
    )

    price_target: Optional[Money] = Field(
        default=None,
        description="[NOT REQUIRED] Target or unit price with amount, currency, and per_unit.",
        json_schema_extra={"required": False, "is_required": False},
    )

    location: Optional[Location] = Field(
        default=None,
        description="[NOT REQUIRED] Delivery or shipping location (city, state, country).",
        json_schema_extra={"required": False, "is_required": False},
    )

    deadline: Optional[Deadline] = Field(
        default=None,
        description="[NOT REQUIRED] Target delivery date or timeline in days.",
        json_schema_extra={"required": False, "is_required": False},
    )

    product_details: dict[str, Any] = Field(
        default_factory=dict,
        description="[NOT REQUIRED] Custom product attributes, technical specifications, materials, or certifications.",
        json_schema_extra={"required": False, "is_required": False},
    )

    status: Optional[RFQStatus] = Field(
        default=RFQStatus.DRAFT,
        description="[NOT REQUIRED] RFQ status ('draft', 'active', 'closed'). Defaults to 'draft'.",
        json_schema_extra={"required": False, "is_required": False},
    )

    @classmethod
    def get_field_requirements(cls) -> dict[str, bool]:
        """Convenience method returning {field_name: is_required_boolean}."""
        return {name: field.is_required() for name, field in cls.model_fields.items()}

"""Value objects shared across request and response bodies.

The wire format is nested -- ``quantity: {value, unit}`` -- while the database
keeps flat typed columns. The mapping happens in the RFQ service, so neither
side has to compromise: the API stays readable, the columns stay indexable.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Optional, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


def normalise_decimal(value: Decimal | None) -> int | float | None:
    """Render a Decimal as a plain JSON number.

    Numeric(18,4) round-trips out of Postgres carrying its scale, so a quantity
    of 8000 comes back as Decimal("8000.0000") and Pydantic would emit the
    string "8000.0000". Callers want the number 8000.
    """
    if value is None:
        return None
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        return int(normalised)
    return float(normalised)


class Quantity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Decimal = Field(gt=0, description="How much. Always positive.")
    unit: str = Field(min_length=1, max_length=32, description="pcs, kg, tonnes, cartons...")

    @field_serializer("value")
    def _serialise_value(self, value: Decimal) -> int | float | None:
        return normalise_decimal(value)


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    per_unit: Optional[str] = Field(
        default=None,
        max_length=32,
        description="The unit the price is quoted per -- 'kg' in 'Rs 200/kg'.",
    )

    @model_validator(mode="after")
    def _upper_currency(self) -> Self:
        self.currency = self.currency.upper()
        return self

    @field_serializer("amount")
    def _serialise_amount(self, value: Decimal) -> int | float | None:
        return normalise_decimal(value)


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: Optional[str] = Field(default=None, max_length=120)
    state: Optional[str] = Field(default=None, max_length=120)
    country: Optional[str] = Field(default=None, max_length=120)
    latitude: Optional[Decimal] = Field(default=None, ge=-90, le=90)
    longitude: Optional[Decimal] = Field(default=None, ge=-180, le=180)
    raw: Optional[str] = Field(
        default=None, max_length=300, description="What the user typed, kept for re-geocoding."
    )

    @field_serializer("latitude", "longitude")
    def _serialise_coord(self, value: Optional[Decimal]) -> int | float | None:
        return normalise_decimal(value)


class Deadline(BaseModel):
    """A deadline resolves to an absolute instant before it is stored.

    A caller may send an absolute ``date`` or a relative ``in_days``; 'within 3
    days' stops meaning anything once the row is a week old, so only the
    resolved date is persisted. ``raw`` keeps the original phrasing for audit.
    """

    model_config = ConfigDict(extra="forbid")

    date: Optional[date] = None
    in_days: Optional[int] = Field(default=None, ge=0, le=3650)
    raw: Optional[str] = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def _require_one(self) -> Self:
        if self.date is None and self.in_days is None:
            raise ValueError("deadline needs either 'date' or 'in_days'")
        return self

    def resolve(self, now: datetime | None = None) -> datetime:
        """Absolute deadline instant, in UTC.

        A calendar date resolves to the *end* of that day. "I need it by the
        30th" includes the 30th; combining with midnight made a counterparty who
        could deliver that afternoon score as having missed the date.
        """
        if self.date is not None:
            return datetime.combine(self.date, datetime.max.time(), tzinfo=UTC)
        reference = now or datetime.now(UTC)
        return reference + timedelta(days=self.in_days or 0)


class DeadlineOut(BaseModel):
    """Responses expose the resolved instant plus the original phrasing."""

    date: Optional[datetime] = None
    raw: Optional[str] = None

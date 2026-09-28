"""Value objects shared across request and response bodies.

The wire format is nested -- ``quantity: {value, unit}`` -- while the database
keeps flat typed columns. The mapping happens in the RFQ service, so neither
side has to compromise: the API stays readable, the columns stay indexable.
"""

# ``datetime`` is imported as a module: ``Deadline`` has a field called
# ``date``, and annotating it ``Optional[date]`` inside the class body made the
# field name shadow the type, so Pydantic saw ``Optional[None]`` and rejected
# every absolute date.
import datetime as dt
from decimal import Decimal
from typing import Any, Optional, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

UTC = dt.UTC

# Numeric(18,4) holds at most 14 integer digits. Anything larger overflowed in
# Postgres and surfaced as a 500; the bound turns it into a 422 with headroom.
MAX_NUMERIC_VALUE = Decimal("1e13")


def find_nul(value: Any) -> bool:
    """True when any string inside ``value`` (recursively) contains a NUL byte.

    Postgres text columns reject the NUL character outright, so an unchecked NUL in any
    field became an unhandled DB error (500) instead of a validation error.
    """
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(find_nul(k) or find_nul(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(find_nul(v) for v in value)
    return False


def reject_nul(value: Any) -> Any:
    if find_nul(value):
        raise ValueError("text must not contain NUL (\\x00) characters")
    return value


class NoNulModel(BaseModel):
    """Base for request bodies: rejects NUL bytes in every text field."""

    @model_validator(mode="before")
    @classmethod
    def _reject_nul_bytes(cls, data: Any) -> Any:
        return reject_nul(data)


def normalise_decimal(value: Decimal | None) -> int | float | None:
    """Render a Decimal as a plain JSON number.

    Numeric(18,4) round-trips out of Postgres carrying its scale, so a quantity
    of 8000 comes back as Decimal("8000.0000") and Pydantic would emit the
    string "8000.0000". Callers want the number 8000.
    """
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        return int(normalised)
    return float(normalised)


class Quantity(NoNulModel):
    model_config = ConfigDict(extra="forbid")

    value: Decimal = Field(
        gt=0,
        le=MAX_NUMERIC_VALUE,
        allow_inf_nan=False,
        description="How much. Always positive.",
    )
    unit: str = Field(min_length=1, max_length=32, description="pcs, kg, tonnes, cartons...")

    @classmethod
    def from_stored(cls, value: Any, unit: Optional[str]) -> "Quantity":
        """Response object from DB columns; never 500s on legacy out-of-range rows."""
        value_dec = value if isinstance(value, Decimal) else Decimal(str(value))
        try:
            return cls(value=value_dec, unit=unit or "units")
        except ValueError:
            return cls.model_construct(value=value_dec, unit=unit or "units")

    @field_serializer("value")
    def _serialise_value(self, value: Decimal) -> int | float | None:
        return normalise_decimal(value)


def supported_currencies() -> set[str]:
    """ISO codes the platform can price and convert (live table, read at call time)."""
    from services import currency as currency_service

    return set(currency_service.RATES)


class Money(NoNulModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(ge=0, le=MAX_NUMERIC_VALUE, allow_inf_nan=False)
    currency: str = Field(default="INR", max_length=64)
    per_unit: Optional[str] = Field(
        default=None,
        max_length=32,
        description="The unit the price is quoted per -- 'kg' in 'Rs 200/kg'.",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_currency_input(cls, data: Any) -> Any:
        if isinstance(data, dict) and "currency" in data and data["currency"]:
            from services.currency import normalize_currency
            raw_cur = str(data["currency"]).strip()
            norm = normalize_currency(raw_cur)
            # No truncation: "FOOBAR" used to be silently stored as "FOO".
            data = {**data, "currency": norm if norm else raw_cur.upper()}
        return data

    @model_validator(mode="after")
    def _ensure_valid_currency(self) -> Self:
        from services.currency import normalize_currency
        norm = normalize_currency(self.currency)
        code = norm if norm else self.currency.upper()
        if code not in supported_currencies():
            raise ValueError(
                f"unsupported currency '{self.currency}'; use an ISO code such as INR, USD or EUR"
            )
        self.currency = code
        return self

    @classmethod
    def from_stored(
        cls, amount: Any, currency: Optional[str], per_unit: Optional[str]
    ) -> "Money":
        """Build a response object from DB columns without re-validating them.

        Rows written before currency validation existed may carry codes such as
        "FOO"; a read must never 500 on data that is already stored.
        """
        amount_dec = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        try:
            return cls(amount=amount_dec, currency=currency or "INR", per_unit=per_unit)
        except ValueError:
            return cls.model_construct(
                amount=amount_dec, currency=(currency or "INR"), per_unit=per_unit
            )

    @field_serializer("amount")
    def _serialise_amount(self, value: Decimal) -> int | float | None:
        return normalise_decimal(value)


class Location(NoNulModel):
    model_config = ConfigDict(extra="forbid")

    city: Optional[str] = Field(default=None, max_length=120)
    state: Optional[str] = Field(default=None, max_length=120)
    country: Optional[str] = Field(default=None, max_length=120)
    latitude: Optional[Decimal] = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: Optional[Decimal] = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    raw: Optional[str] = Field(
        default=None, max_length=300, description="What the user typed, kept for re-geocoding."
    )

    @field_serializer("latitude", "longitude")
    def _serialise_coord(self, value: Optional[Decimal]) -> int | float | None:
        return normalise_decimal(value)


class Deadline(NoNulModel):
    """A deadline resolves to an absolute instant before it is stored.

    A caller may send an absolute ``date`` or a relative ``in_days``; 'within 3
    days' stops meaning anything once the row is a week old, so only the
    resolved date is persisted. ``raw`` keeps the original phrasing for audit.

    A deadline must lie in the future: a date before today, or ``in_days`` of
    0, produced a listing that expired the instant it was created.
    """

    model_config = ConfigDict(extra="forbid")

    date: Optional[dt.date] = None
    in_days: Optional[int] = Field(default=None, ge=1, le=3650)
    raw: Optional[str] = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def _require_one(self) -> Self:
        if self.date is None and self.in_days is None:
            raise ValueError("deadline needs either 'date' or 'in_days'")
        if self.date is not None:
            # Compared against the UTC calendar day; resolve() makes a date mean
            # the end of that day, so "today" is still a valid deadline.
            today = dt.datetime.now(UTC).date()
            if self.date < today:
                raise ValueError("deadline date must not be in the past")
            if self.date > today + dt.timedelta(days=3650):
                raise ValueError("deadline date must be within 10 years")
        return self

    def resolve(self, now: dt.datetime | None = None) -> dt.datetime:
        """Absolute deadline instant, in UTC.

        A calendar date resolves to the *end* of that day. "I need it by the
        30th" includes the 30th; combining with midnight made a counterparty who
        could deliver that afternoon score as having missed the date.
        """
        if self.date is not None:
            return dt.datetime.combine(self.date, dt.datetime.max.time(), tzinfo=UTC)
        reference = now or dt.datetime.now(UTC)
        return reference + dt.timedelta(days=self.in_days or 0)


class DeadlineOut(BaseModel):
    """Responses expose the resolved instant plus the original phrasing.

    In accordance with B2B trade standards, deadlines reflect the dispatch /
    production readiness milestone and exclude transport/transit days.
    """

    date: Optional[dt.datetime] = None
    raw: Optional[str] = None
    excludes_transport: bool = True
    estimated_delivery_at: Optional[dt.datetime] = None


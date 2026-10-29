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
import html
import re
from decimal import Decimal
from typing import Any, Optional, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

UTC = dt.UTC

# Numeric(18,4) holds at most 14 integer digits. Anything larger overflowed in
# Postgres and surfaced as a 500; the bound turns it into a 422 with headroom.
MAX_NUMERIC_VALUE = Decimal("1e13")

# ---------------------------------------------------------------------------------------------
# Units and free-text hygiene shared by every request body
# ---------------------------------------------------------------------------------------------

# Preferred spelling per physical dimension: an alias (kilo, tons, MT, ltr...) resolves to the first
# entry here that has the same conversion factor in the same table.
_PREFERRED_SYMBOLS = (
    "tonne", "quintal", "kg", "g", "lb", "oz",
    "m", "km", "cm", "mm", "in", "ft", "yd",
    "l", "ml", "kl", "m3", "gallon", "barrel",
    "w", "kw", "mw", "hp", "v", "kv", "mv", "ah", "mah",
)
_COUNT_GROUPS = {
    "pcs": {"pc", "pcs", "piece", "pieces", "unit", "units", "no", "nos", "number", "numbers", "each", "ea", "item", "items"},
    "dozen": {"dozen", "dozens", "doz", "dz"},
    "pair": {"pair", "pairs"},
    "set": {"set", "sets"},
}
# Count / packaging / trade units the conversion tables do not list but a listing legitimately uses.
_EXTRA_UNITS = frozenset(
    """container containers teu fcl lcl truck trucks truckload truckloads lorry trip trips load loads lot lots kit kits
    bottle bottles can cans tin tins jar jars tube tubes tub tubs packet packets strip strips tablet tablets capsule
    capsules vial vials ampoule ampoules sachet sachets bucket buckets pail pails cylinder cylinders tank tanks tray
    trays bunch bunches head heads cone cones spool spools coil coils reel reels skein hank ream reams roll rolls
    sheet sheets bundle bundles bag bags box boxes carton cartons crate crates drum drums pallet pallets sack sacks
    bale bales barrel barrels bora boras bori boris katta kattas pack packs case cases
    sqft sq.ft sq ft sqm sq m sqmt sq mt square foot square feet square meter square metre square meters square metres
    cum cbm cft cubic foot cubic feet m2 acre acres hectare hectares ha bigha bighas gunta guntha
    wp kwp mwp gwp kva mva kwh mwh amp amps ampere amperes hour hours hr hrs day days week weeks month months year years
    job jobs license licenses licence licences user users seat seats service services license
    mg milligram milligrams litre liter meter metre""".split()
)


def _unit_tables() -> list[dict[str, Decimal]]:
    from services import unit_converter as uc

    return [
        uc.MASS_CONVERSION_TO_KG, uc.LENGTH_CONVERSION_TO_METER, uc.VOLUME_CONVERSION_TO_LITER,
        uc.POWER_CONVERSION_TO_WATT, uc.VOLTAGE_CONVERSION_TO_VOLT, uc.CAPACITY_CONVERSION_TO_AH,
    ]


def _learned_units() -> set[str]:
    try:
        from services import learned_vocabulary

        return {str(u).strip().lower() for u in learned_vocabulary.known_units()}
    except Exception:  # noqa: BLE001 - optional vocabulary
        return set()


def known_unit_names() -> list[str]:
    from services import unit_converter as uc

    names: set[str] = set(_PREFERRED_SYMBOLS) | set(_COUNT_GROUPS) | set(_EXTRA_UNITS)
    for table in _unit_tables():
        names |= set(table)
    names |= set(uc.COUNT_CONVERSION_TO_PIECES) | set(uc.PACKAGING_UNITS) | _learned_units()
    return sorted(names)


_NUM_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(.+)$")


_AREA_RE = re.compile(
    r"^(?:sq\.?\s*|square\s+)(ft|feet|foot|m|mt|meters?|metres?|yd|yards?|cm|mm|km|in|inch|inches)\.?$"
    r"|^sq\.?(ft|m|mt|yd|cm|mm|km)$"
)


def canonical_unit(raw: Optional[str]) -> Optional[str]:
    """The canonical spelling of a unit, or None when it is not a unit we know.

    kilo/kgs -> kg, tons/MT/metric tons -> tonne, ltr -> l, pieces/nos -> pcs, "Rs/kg" -> kg. A numeric
    pack size ("50 kg", "1000 pcs") is kept as "<n> <unit>". Units we cannot resolve are not guessed.
    """
    from services import unit_converter as uc

    if raw is None:
        return None
    area = _AREA_RE.match(re.sub(r"^(?:per|/|rs\.?/?|₹/?)\s*", "", str(raw).strip().lower()))
    if area:
        base = area.group(1) or area.group(2)
        return "sq " + {"feet": "ft", "foot": "ft", "meter": "m", "metre": "m", "mt": "m", "meters": "m",
                        "metres": "m", "inch": "in", "inches": "in", "yard": "yd", "yards": "yd"}.get(base, base)
    cleaned = uc.clean_unit(raw)
    if not cleaned:
        return None
    cleaned = cleaned.replace("sq.", "sq ").replace("  ", " ")
    for table in _unit_tables():
        if cleaned in table:
            factor = table[cleaned]
            for sym in _PREFERRED_SYMBOLS:
                if sym in table and table[sym] == factor:
                    # only merge spellings that are not themselves the pack-size forms
                    return sym if not cleaned[:1].isdigit() else cleaned
            return cleaned
    for sym, group in _COUNT_GROUPS.items():
        if cleaned in group:
            return sym
    if cleaned in uc.COUNT_CONVERSION_TO_PIECES or cleaned in uc.PACKAGING_UNITS or cleaned in _EXTRA_UNITS:
        return cleaned
    if cleaned in _learned_units():
        return cleaned
    m = _NUM_PREFIX_RE.match(cleaned)
    if m and not cleaned.startswith("per"):
        inner = canonical_unit(m.group(2))
        if inner:
            return f"{m.group(1)} {inner}"
    return None


def unit_suggestion(raw: str) -> Optional[str]:
    import difflib

    base = (raw or "").strip().lower()
    hit = difflib.get_close_matches(base, known_unit_names(), n=1, cutoff=0.75)
    return hit[0] if hit else None


def require_unit(raw: str) -> str:
    unit = canonical_unit(raw)
    if unit is None:
        hint = unit_suggestion(raw)
        msg = f"unknown unit '{str(raw)[:32]}'"
        msg += f"; did you mean '{hint}'?" if hint else "; use a unit such as kg, tonne, pcs, m, l, box, bag, set"
        raise ValueError(msg)
    return unit


_SCRIPT_BLOCK_RE = re.compile(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def strip_markup(value: str) -> str:
    """Remove HTML tags / script blocks and unescape entities from user text; keeps plain text and newlines.

    A lone "<" or ">" in prose ("size < 5 mm", "a > b") survives: only tag-shaped sequences go.
    """
    text = html.unescape(value)
    text = _SCRIPT_BLOCK_RE.sub(" ", text)
    text = re.sub(r"<\s*/?\s*[a-zA-Z!?][^>]*>?", " ", text)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


def clean_user_text(value: Any) -> Any:
    """strip_markup applied recursively to every string inside a str / list / dict."""
    if isinstance(value, str):
        return strip_markup(value)
    if isinstance(value, dict):
        return {k: clean_user_text(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_user_text(v) for v in value]
    return value


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

    @field_validator("unit", mode="before")
    @classmethod
    def _known_unit(cls, value: Any) -> Any:
        return require_unit(value) if isinstance(value, str) else value

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

    amount: Decimal = Field(gt=0, le=MAX_NUMERIC_VALUE, allow_inf_nan=False)
    currency: str = Field(default="INR", max_length=64)
    per_unit: Optional[str] = Field(
        default=None,
        max_length=32,
        description="The unit the price is quoted per -- 'kg' in 'Rs 200/kg'.",
    )

    @field_validator("per_unit", mode="before")
    @classmethod
    def _known_per_unit(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return require_unit(value) if isinstance(value, str) else value

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

    @field_validator("city", "state", "country", "raw", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = strip_markup(value)
            return value or None
        return value

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


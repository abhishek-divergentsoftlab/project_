"""The RFQ draft: merge validated field updates into it, say what is still missing, and build the RFQ create payload from it."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from models.enums import RFQRole, RFQStatus
from schemas.common import Deadline, Location, Money, Quantity
from schemas.rfq import RFQCreate
from services import chat_specs, query_extractor, query_grounding
from services.chat_agent import draft_assist
from services.chat_agent import issues

logger = logging.getLogger(__name__)


# =============================================================================
# Extensible Configuration for Future Expansions
# =============================================================================
class RFQCollectorConfig(BaseModel):
    """Configuration governing conversational RFQ collection and field requirements."""

    model_config = ConfigDict(extra="allow")

    # Sequence of required fields to collect before asking for optional details
    required_fields: list[str] = Field(
        default_factory=lambda: ["role", "category"],
        description="Fields that must be collected before optional fields are solicited.",
    )

    # Optional fields to offer to the user once required fields are satisfied
    optional_fields: list[str] = Field(
        default_factory=lambda: [
            "quantity",
            "price_target",
            "location",
            "deadline",
            "description",
            "product_details",
        ],
        description="Secondary fields to suggest after required fields are complete.",
    )

    # Defaults & behavioral flags
    auto_preprocess: bool = Field(
        default=True,
        description="Run deterministic regex/NLP parser on user inputs alongside LLM tool calling.",
    )
    default_currency: str = Field(
        default="INR",
        description="Default currency for price targets when unspecified.",
    )
    infer_category_from_product: bool = Field(
        default=True,
        description="Automatically map product names (e.g. apple) to categories (e.g. Agriculture).",
    )
    allow_custom_attributes: bool = Field(
        default=True,
        description="Allow dynamic key-value attributes in product_details.",
    )


DEFAULT_CONFIG = RFQCollectorConfig()


def _parse_iso_date(raw: Any) -> Optional[date]:
    try:
        return datetime.strptime(str(raw).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _sane_numbers_and_dates(kwargs: dict[str, Any], draft: dict[str, Any], note: Any) -> dict[str, Any]:
    """Drop what cannot be real from tool arguments, whoever wrote them (the rule parser or the model).

    A quantity or price that is zero, negative or absurd is not stored; neither is a delivery date that is invalid or
    already past. Each drop says why, so the user is asked instead of silently getting a guess."""
    flags: dict[str, Any] = {}
    for key in ("quantity_value", "min_quantity", "max_quantity"):
        value = kwargs.get(key)
        if value is None:
            continue
        try:
            number = Decimal(str(value))
        except Exception:
            kwargs.pop(key, None)
            continue
        unit = kwargs.get("quantity_unit") or (draft.get("quantity") or {}).get("unit")
        if number <= 0:
            flags["quantity_nonpositive"] = f"{value} {unit or ''}".strip()
        elif query_grounding.quantity_is_absurd(number, unit):
            flags["quantity_absurd"] = f"{value} {unit or ''}".strip()
        else:
            continue
        kwargs.pop(key, None)
    if "quantity_nonpositive" in flags or "quantity_absurd" in flags:
        kwargs.pop("quantity_unit", None)
    price = kwargs.get("price_amount")
    if price is not None:
        try:
            amount = Decimal(str(price))
        except Exception:
            amount = Decimal(0)
        per = chat_specs.normalize_per_unit(kwargs.get("price_per_unit"))
        base = query_extractor.unit_converter.base_unit(per) if per else None
        per_base = amount / base[0] if base else amount
        if amount <= 0 or (base and (per_base < query_extractor._MIN_PRICE_PER_BASE or per_base > query_extractor._MAX_PRICE_PER_BASE)):
            flags["price_absurd"] = " ".join(
                part for part in (str(price), str(kwargs.get("price_currency") or ""), f"per {per}" if per else "") if part
            )
            for key in ("price_amount", "price_currency", "price_per_unit"):
                kwargs.pop(key, None)
    for key in ("deadline_date",):
        raw = kwargs.get(key)
        if not raw:
            continue
        parsed = _parse_iso_date(raw)
        if parsed is None:
            flags["date_invalid"] = str(raw)
        elif parsed < datetime.now().date():
            flags["date_past"] = str(raw)
        else:
            continue
        kwargs.pop(key, None)
    city = kwargs.get("city")
    if city and not kwargs.get("state") and not kwargs.get("country"):
        # A bare town must be one the gazetteer or a cached model lookup knows; an unknown one is asked about.
        from services import locations

        if locations.find_location(str(city)) is None:
            flags["location_unresolved"] = str(city)
            kwargs.pop("city", None)
    days = kwargs.get("deadline_days")
    if days is not None:
        try:
            if int(days) < 0 or int(days) > 3650:
                kwargs.pop("deadline_days", None)
        except Exception:
            kwargs.pop("deadline_days", None)
    for text in issues.notes_for_flags(flags, "en"):
        note(text)
    return kwargs


# =============================================================================
# Python Tool Implementation with **kwargs
# =============================================================================
def update_rfq_draft(
    existing_draft: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Tool function accepting **kwargs to update the structured RFQ draft.

    Args:
        existing_draft: Dict of current RFQ draft state (or None).
        **kwargs: Extracted fields passed by LLM tool call or preprocessing.

    Returns:
        Updated dict conforming to RFQStructure format.
    """
    draft = dict(existing_draft or {})
    notes_out = kwargs.get("_notes") if isinstance(kwargs.get("_notes"), list) else None

    def note(text: str) -> None:
        if notes_out is not None and text not in notes_out:
            notes_out.append(text)

    kwargs = _sane_numbers_and_dates(dict(kwargs), draft, note)

    # The form's own fields never become specs: they fill the matching draft field when it is still empty and
    # are dropped otherwise; nested values are flattened to text.
    kwargs = dict(kwargs)
    if isinstance(kwargs.get("product_details"), dict):
        clean_details, core = chat_specs.clean_product_details(kwargs["product_details"])
        kwargs["product_details"] = clean_details
        for key, value in chat_specs.core_as_fields(core, draft).items():
            if kwargs.get(key) is None:
                kwargs[key] = value

    # 1. Role: 'buyer' or 'seller'
    role = kwargs.get("role")
    if role:
        norm_role = str(role).strip().lower()
        if norm_role in ("buyer", "buy", "purchase", "purchaser"):
            draft["role"] = RFQRole.BUYER.value
        elif norm_role in ("seller", "sell", "supplier", "vendor"):
            draft["role"] = RFQRole.SELLER.value

    # 2. Product Name & Category
    product_name = kwargs.get("product_name")
    category = kwargs.get("category")

    details = dict(draft.get("product_details") or {})
    if product_name and str(product_name).strip():  # the model's tool call is grounded in the user's words (guards.py)
        details["name"] = str(product_name).strip()

    # Merge any custom product_details passed (already cleaned above).
    extra_details = kwargs.get("product_details")
    if isinstance(extra_details, dict):
        details.update(extra_details)
        details = chat_specs.drop_unit_variants(details)

    if details:
        draft["product_details"] = details

    if category and str(category).strip():
        draft["category"] = str(category).strip().capitalize()
    elif not draft.get("category") and product_name:
        # Infer category using marketplace dictionary
        inferred = query_extractor._infer_category(str(product_name))
        if inferred:
            draft["category"] = inferred

    # Attributes the user marked as must-match (only ones already present are flagged).
    if kwargs.get("must_match"):
        draft_assist.set_must_match(details, kwargs["must_match"])
        draft["product_details"] = details

    # An HSN code the user typed: validated like GET /hsn/code/{code}; a known code is a manual pick.
    if kwargs.get("hsn_code"):
        hsn_status, _item = draft_assist.apply_explicit_hsn(
            draft, kwargs["hsn_code"], kwargs["_notes"] if isinstance(kwargs.get("_notes"), list) else None
        )
        if hsn_status in ("unknown", "invalid") and isinstance(kwargs.get("_notes"), list):
            kwargs["_notes"].append(draft_assist.unrecognised_hsn_note(kwargs["hsn_code"]))

    # 3. Description
    description = kwargs.get("description")
    if description and str(description).strip():
        draft["description"] = str(description).strip()

    # 4. Quantity & Range
    q_val = kwargs.get("quantity_value")
    q_unit = kwargs.get("quantity_unit")
    min_q = kwargs.get("min_quantity")
    max_q = kwargs.get("max_quantity")
    if min_q is not None:
        try:
            val_min = float(Decimal(str(min_q)))
            details["min_quantity"] = val_min
            draft["min_quantity"] = val_min
        except Exception:
            pass
    if max_q is not None:
        try:
            val_max = float(Decimal(str(max_q)))
            details["max_quantity"] = val_max
            draft["max_quantity"] = val_max
        except Exception:
            pass

    # A unit is never invented ("10" is not "10 pcs"): without one the user is asked, unless the draft already has one.
    known_unit = str(q_unit or (draft.get("quantity") or {}).get("unit") or "").strip()
    if q_val is not None:
        if not known_unit:
            note(issues.notes_for_flags({"quantity_unit_missing": q_val}, "en")[0])
        else:
            try:
                draft["quantity"] = {"value": float(Decimal(str(q_val))), "unit": known_unit}
            except Exception:
                pass
    elif min_q is not None or max_q is not None:
        base_val = max_q if max_q is not None else min_q
        if known_unit:
            try:
                draft["quantity"] = {"value": float(Decimal(str(base_val))), "unit": known_unit}
            except Exception:
                pass

    # Negated attributes
    negated_attrs = kwargs.get("negated_attributes")
    if negated_attrs:
        if isinstance(negated_attrs, (str, bytes)):
            negated_attrs = [negated_attrs]
        existing_neg = set(details.get("_negated") or details.get("negated_attributes") or [])
        existing_neg.update(str(x).strip().lower() for x in negated_attrs if str(x).strip())
        # A product cannot be its own negated attribute
        if details.get("name"):
            prod_clean = str(details["name"]).strip().lower()
            existing_neg.discard(prod_clean)
            for token in prod_clean.split():
                existing_neg.discard(token)
        if existing_neg:
            sorted_neg = sorted(list(existing_neg))
            details["_negated"] = sorted_neg
            details["negated_attributes"] = sorted_neg
            draft["negated_attributes"] = sorted_neg
        else:
            details.pop("_negated", None)
            details.pop("negated_attributes", None)
            draft.pop("negated_attributes", None)

    if details:
        # ``details`` was copied before the HSN step, which writes into the draft directly;
        # without this a code the user just set ("hsn 1006") was silently dropped.
        current = draft.get("product_details") or {}
        for key in ("hsn_code", "hsn_source"):
            if key in current:
                details[key] = current[key]
        draft["product_details"] = details

    # Minimum Order
    moq_val = kwargs.get("min_order_value")
    moq_unit = kwargs.get("min_order_unit")
    if moq_val is not None:
        try:
            val = Decimal(str(moq_val))
            unit = str(moq_unit or q_unit or "pcs").strip()
            draft["minimum_order"] = {"value": float(val), "unit": unit}
        except Exception:
            pass

    # 5. Price Target
    price_val = kwargs.get("price_amount")
    if price_val is not None:
        try:
            amount = Decimal(str(price_val))
            cur = str(kwargs.get("price_currency") or draft.get("price_target", {}).get("currency") or "INR").upper()
            per_u = chat_specs.normalize_per_unit(kwargs.get("price_per_unit"))
            if per_u is None:
                per_u = chat_specs.normalize_per_unit((draft.get("price_target") or {}).get("per_unit"))
            draft["price_target"] = {
                "amount": float(amount),
                "currency": cur,
                "per_unit": per_u,
            }
        except Exception:
            pass

    # 6. Location
    city = kwargs.get("city")
    state = kwargs.get("state")
    country = kwargs.get("country")
    if city or state or country:
        loc_dict = dict(draft.get("location") or {})
        if city and not state and not country and str(loc_dict.get("city") or "").strip().lower() != str(city).strip().lower():
            # A different city is not in the previous city's state or country: nothing is inherited from it.
            loc_dict = {}
        if city:
            loc_dict["city"] = str(city).strip().title()
        if state:
            loc_dict["state"] = str(state).strip().title()
        if country:
            loc_dict["country"] = str(country).strip().title()
        draft["location"] = loc_dict

    # 7. Deadline
    d_days = kwargs.get("deadline_days")
    d_date = kwargs.get("deadline_date")
    if d_days is not None or d_date:
        dl_dict = dict(draft.get("deadline") or {})
        if d_days is not None:
            try:
                dl_dict["in_days"] = int(d_days)
            except Exception:
                pass
        if d_date:
            dl_dict["date"] = str(d_date).strip()
        draft["deadline"] = dl_dict

    # 8. Status is never set here: it comes from the confirmation flow (publish or save as draft), not from a tool call.

    return draft


# =============================================================================
# Status Analysis: Required vs Optional Completion Checker
# =============================================================================
def evaluate_rfq_readiness(
    draft: dict[str, Any],
    config: RFQCollectorConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Analyze which required and optional fields have been collected so far.

    Returns:
        {
            "has_role": bool,
            "has_product": bool,
            "has_category": bool,
            "required_complete": bool,
            "missing_required": list[str],
            "next_required_prompt": Optional[str],
            "collected_optional": list[str],
            "missing_optional": list[str],
        }
    """
    has_role = bool(draft.get("role"))
    has_category = bool(draft.get("category"))
    has_product = bool(draft.get("product_details", {}).get("name"))

    missing_required: list[str] = []
    if not has_role:
        missing_required.append("role")
    if not has_product:
        missing_required.append("product")

    required_complete = len(missing_required) == 0

    # Determine next required question in strict sequence:
    # 1. Role first
    # 2. Product / Category next
    next_required_prompt: Optional[str] = None
    if not has_role:
        next_required_prompt = "role"
    elif not has_product:
        next_required_prompt = "product"

    # Optional fields analysis
    collected_optional: list[str] = []
    missing_optional: list[str] = []

    for field in config.optional_fields:
        if draft.get(field):
            collected_optional.append(field)
        else:
            missing_optional.append(field)

    return {
        "has_role": has_role,
        "has_product": has_product,
        "has_category": has_category,
        "required_complete": required_complete,
        "missing_required": missing_required,
        "next_required_prompt": next_required_prompt,
        "collected_optional": collected_optional,
        "missing_optional": missing_optional,
    }


class RFQDraftInvalid(ValueError):
    """The draft cannot become an RFQ; ``errors`` is a list of (field, message)."""

    def __init__(self, errors: list[tuple[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{f}: {m}" for f, m in errors))


def _short_error(exc: Exception) -> str:
    return str(exc) or "invalid value"


def _validation_errors(exc: Exception, default_field: str = "rfq") -> list[tuple[str, str]]:
    errs = exc.errors() if hasattr(exc, "errors") else None
    if not errs:
        return [(default_field, _short_error(exc))]
    out: list[tuple[str, str]] = []
    for e in errs:
        loc = [str(x) for x in e.get("loc", ())]
        field = loc[0] if loc else default_field
        msg = re.sub(r"^(Value error, |Assertion failed, )", "", str(e.get("msg", "invalid value")))
        msg = re.sub(r"^Input should be ", "must be ", msg)
        if len(loc) > 1 and loc[-1] != loc[0]:
            msg = f"{loc[-1]} {msg}"
        out.append((field, msg))
    return out


def build_rfq_create_payload(
    draft: dict[str, Any],
    title: Optional[str] = None,
    status: str = "active",
    notes: Optional[str] = None,
) -> RFQCreate:
    """Transform an accumulated RFQ draft into a validated RFQCreate, exactly as the web form would.

    Invalid values are never dropped silently: they raise RFQDraftInvalid.
    """
    errors: list[tuple[str, str]] = []

    role_str = (draft.get("role") or "").strip().lower()
    role: Optional[RFQRole] = None
    if role_str == "buyer":
        role = RFQRole.BUYER
    elif role_str == "seller":
        role = RFQRole.SELLER
    else:
        errors.append(("role", "buyer or seller is required"))

    product_name = str((draft.get("product_details") or {}).get("name") or "").strip()
    category = draft.get("category")
    if not category:
        if product_name:
            category = query_extractor._infer_category(product_name)
        if not category:
            category = "General Goods"

    # Like the form: the title is the product name as written.
    if not title:
        title = draft.get("title") or product_name or category
    title = str(title).strip()

    description = notes or draft.get("description") or None

    def build(field: str, factory: Any) -> Any:
        value = draft.get(field)
        if not value:
            return None
        if not isinstance(value, dict):
            errors.append((field, "invalid value"))
            return None
        try:
            return factory(**value)
        except Exception as exc:
            errors.extend((field, m) for _, m in _validation_errors(exc, field))
            return None

    quantity = build("quantity", Quantity)
    minimum_order = build("minimum_order", Quantity) if role == RFQRole.SELLER else None
    price_target = build("price_target", Money)
    location = build("location", Location)
    deadline = build("deadline", Deadline)

    product_details = dict(draft.get("product_details") or {})

    rfq_status = (
        RFQStatus.ACTIVE
        if (status or "").lower() == "active"
        else RFQStatus.DRAFT
    )

    if errors:
        raise RFQDraftInvalid(errors)

    try:
        return RFQCreate(
            role=role,
            category=category[:120],
            title=title[:300],
            description=description,
            quantity=quantity,
            minimum_order=minimum_order,
            price_target=price_target,
            location=location,
            deadline=deadline,
            product_details=product_details,
            status=rfq_status,
        )
    except ValidationError as exc:
        raise RFQDraftInvalid(_validation_errors(exc)) from exc

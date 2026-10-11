"""RFQ extraction tool and preprocessing service for conversational RFQ collection.

Enforces:
1. Role first ('buyer' or 'seller')
2. Product & Category next
3. Follow-up required fields before optional fields
4. Handles single-input multi-field extraction (e.g., 'I want to buy apple in indore')
5. Exposes update_rfq_draft tool with **kwargs callable by LLMs
6. Extensible configuration for future marketplace requirements
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from models.enums import RFQRole, RFQStatus
from schemas.common import Deadline, Location, Money, Quantity
from schemas.rfq import RFQCreate
from schemas.rfq_structure import RFQStructure
from services import chat_draft_assist, query_extractor
from services.chat_permissions import (
    check_close_rfq_intent,
    check_counterparty_message_intent,
    check_create_rfq_intent,
    is_information_request,
    is_question,
)

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


# =============================================================================
# LLM Tool Definition (JSON Schema for Ollama / gpt-oss:20b)
# =============================================================================
UPDATE_RFQ_DRAFT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_rfq_draft",
        "description": (
            "Update or populate the structured RFQ draft with any business details mentioned by the user. "
            "Can accept single or multiple fields at once (e.g. role, product, category, location, quantity, price)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": ["buyer", "seller"],
                    "description": "[REQUIRED] Whether the user is buying or selling.",
                },
                "category": {
                    "type": "string",
                    "description": "[REQUIRED] Industry category (e.g. Agriculture, Packaging, Electronics, Furniture, Textiles).",
                },
                "product_name": {
                    "type": "string",
                    "description": "Specific product name (e.g. Apple, USB-C Cable, Corrugated Box).",
                },
                "description": {
                    "type": "string",
                    "description": "[OPTIONAL] Details, specifications, notes, or terms.",
                },
                "quantity_value": {
                    "type": "number",
                    "description": "[OPTIONAL] Numerical quantity amount (e.g. 500, 10000).",
                },
                "quantity_unit": {
                    "type": "string",
                    "description": "[OPTIONAL] Unit of measurement (e.g. kg, pieces, tonnes, cartons).",
                },
                "min_order_value": {
                    "type": "number",
                    "description": "[OPTIONAL] Minimum order quantity.",
                },
                "min_order_unit": {
                    "type": "string",
                    "description": "[OPTIONAL] Unit for minimum order.",
                },
                "price_amount": {
                    "type": "number",
                    "description": "[OPTIONAL] Target unit price or budget amount.",
                },
                "price_currency": {
                    "type": "string",
                    "description": "[OPTIONAL] Currency code (e.g. INR, USD, EUR).",
                },
                "price_per_unit": {
                    "type": "string",
                    "description": "[OPTIONAL] Unit price is quoted per (e.g. per kg, per piece).",
                },
                "city": {
                    "type": "string",
                    "description": "[OPTIONAL] Delivery or supply city (e.g. Indore, Mumbai, Delhi).",
                },
                "state": {
                    "type": "string",
                    "description": "[OPTIONAL] State or province.",
                },
                "country": {
                    "type": "string",
                    "description": "[OPTIONAL] Country name.",
                },
                "deadline_days": {
                    "type": "integer",
                    "description": "[OPTIONAL] Delivery deadline in number of days from now.",
                },
                "deadline_date": {
                    "type": "string",
                    "description": "[OPTIONAL] Absolute deadline date string (YYYY-MM-DD).",
                },
                "product_details": {
                    "type": "object",
                    "description": "[OPTIONAL] Technical attributes, specs, colors, grades, etc.",
                },
                "hsn_code": {
                    "type": "string",
                    "description": (
                        "[OPTIONAL] HSN/tariff code the USER typed (e.g. 1006, 100630). Only pass a code the user stated; "
                        "it is checked against the HSN master and ignored when unknown."
                    ),
                },
                "must_match": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "[OPTIONAL] Names of attributes in product_details the user said must match strictly "
                        "(e.g. ['Grade']). Unknown attribute names are ignored."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": ["draft", "active"],
                    "description": "[OPTIONAL] RFQ status.",
                },
            },
            "required": [],
        },
    },
}


CREATE_RFQ_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "create_rfq",
        "description": (
            "Create and submit the official RFQ into the marketplace database using the accumulated draft. "
            "Call this tool when the user confirms creation (e.g. 'create rfq', 'yes create it', 'proceed', 'post it', 'submit'), "
            "or when both Role and Product are known and the user wants to publish the listing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Optional custom title for the RFQ. If omitted, will be auto-generated from product and location.",
                },
                "status": {
                    "type": "string",
                    "enum": ["active", "draft"],
                    "description": "RFQ status: 'active' (findable by suppliers) or 'draft'. Defaults to 'active'.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional additional specifications or trade notes.",
                },
            },
            "required": [],
        },
    },
}


DRAFT_COUNTERPARTY_MESSAGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "draft_counterparty_message",
        "description": (
            "Draft a professional, polite trade message or price negotiation for the user to review before sending to a seller/supplier or buyer counterparty. "
            "Use this tool whenever the user instructs to send a message to a seller or negotiate (e.g. 'send negotiation message to the counter user', 'message the supplier politely', 'ask seller for discount', 'tell the seller we need 10% off'). "
            "This drafts the message for the user to review, and the user must click the Send button to actually deliver it to the counterparty."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "[REQUIRED] The complete, polite, and professional message text to draft for the counterparty.",
                },
                "recipient_name": {
                    "type": "string",
                    "description": "Optional name of the target supplier or company (e.g., 'Himalaya Orchards', 'the seller').",
                },
                "connection_id": {
                    "type": "string",
                    "description": "Optional UUID of the existing connection if known.",
                },
                "target_rfq_id": {
                    "type": "string",
                    "description": "Optional target RFQ UUID of the counterparty listing if known.",
                },
                "proposed_price": {
                    "type": "number",
                    "description": "Optional numerical price proposed in the negotiation (e.g., 0.9).",
                },
                "proposed_currency": {
                    "type": "string",
                    "description": "Optional currency for the proposed price (e.g., USD, INR).",
                },
            },
            "required": ["message"],
        },
    },
}

SEND_COUNTERPARTY_MESSAGE_TOOL: dict[str, Any] = DRAFT_COUNTERPARTY_MESSAGE_TOOL


CLOSE_RFQ_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "close_rfq",
        "description": (
            "Close and deactivate an existing RFQ so it stops appearing in the marketplace and matches. "
            "Call this tool when the user instructs to close, cancel, or deactivate their RFQ (e.g. 'close this rfq', 'close rfq', 'cancel my rfq', 'close the apple rfq')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rfq_id": {
                    "type": "string",
                    "description": "Optional UUID of the RFQ to close. If omitted, closes the active RFQ of the current session.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional brief reason for closing the RFQ.",
                },
            },
            "required": [],
        },
    },
}


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
    filler_words = {
        "par", "per", "pr", "rate", "price", "budget", "cost", "buyer", "seller",
        "buy", "sell", "in", "at", "before", "after", "by", "on", "for", "with",
        "dollar", "dollars", "inr", "usd", "kg", "pcs", "create", "rfq", "create rfq",
        "proceed", "post", "submit", "publish", "yes", "ok", "okay", "please", "this", "it",
        "mail", "mail id", "mailid", "email", "email id", "emailid", "contact",
        "supplier", "suppliers", "candidate", "candidates", "top", "three", "two", "five",
        "best", "suits", "suit", "suits best", "compare", "which",
        "crate rfq", "creat rfq", "make rfq", "post rfq", "submit rfq", "publish rfq",
    }
    if product_name and str(product_name).strip():
        norm_p = str(product_name).strip().lower()
        if (
            norm_p not in filler_words
            and not re.search(r"\brfq\b", norm_p)
            and not check_create_rfq_intent(norm_p)
            and not is_information_request(norm_p)
        ):
            details["name"] = str(product_name).strip()

    # Merge any custom product_details passed
    extra_details = kwargs.get("product_details")
    if isinstance(extra_details, dict):
        details.update(extra_details)

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
        chat_draft_assist.set_must_match(details, kwargs["must_match"])
        draft["product_details"] = details

    # An HSN code the user typed: validated like GET /hsn/code/{code}; a known code is a manual pick.
    if kwargs.get("hsn_code"):
        hsn_status, _item = chat_draft_assist.apply_explicit_hsn(draft, kwargs["hsn_code"])
        if hsn_status in ("unknown", "invalid") and isinstance(kwargs.get("_notes"), list):
            kwargs["_notes"].append(chat_draft_assist.unrecognised_hsn_note(kwargs["hsn_code"]))

    # 3. Description
    description = kwargs.get("description")
    if description and str(description).strip():
        draft["description"] = str(description).strip()

    # 4. Quantity
    q_val = kwargs.get("quantity_value")
    q_unit = kwargs.get("quantity_unit")
    if q_val is not None:
        try:
            val = Decimal(str(q_val))
            unit = str(q_unit or "pcs").strip()
            draft["quantity"] = {"value": float(val), "unit": unit}
        except Exception:
            pass

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
            per_u = kwargs.get("price_per_unit")
            draft["price_target"] = {
                "amount": float(amount),
                "currency": cur,
                "per_unit": str(per_u).strip() if per_u else None,
            }
        except Exception:
            pass

    # 6. Location
    city = kwargs.get("city")
    state = kwargs.get("state")
    country = kwargs.get("country")
    if city or state or country:
        loc_dict = dict(draft.get("location") or {})
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

    # 8. Status
    if kwargs.get("status"):
        draft["status"] = str(kwargs["status"]).strip().lower()

    return draft


# =============================================================================
# Deterministic Preprocessing for Single / Multi-Field User Inputs
# =============================================================================
def preprocess_message_to_rfq(
    message: str,
    current_draft: Optional[dict[str, Any]] = None,
    config: RFQCollectorConfig = DEFAULT_CONFIG,
    has_active_connection: bool = False,
    has_previous_counterparty_msg: bool = False,
) -> dict[str, Any]:
    """Parse user text with deterministic regex/NLP to instantly capture fields.

    Extracts role, product, category, location, quantity, price, and deadline
    even from a single compound sentence (e.g. 'I want to buy apple in Indore').
    """
    draft = dict(current_draft or {})
    if (
        not message
        or not config.auto_preprocess
        # "what are common payment terms" asks for advice; it states no requirement.
        or (is_question(message) and query_extractor.detect_role(message) is None)
        or check_close_rfq_intent(message)
        or check_create_rfq_intent(message)
        or is_information_request(message)
        or check_counterparty_message_intent(
            message,
            has_active_connection=has_active_connection,
            has_previous_counterparty_msg=has_previous_counterparty_msg,
        )
    ):
        return draft

    # Leverage the built-in query_extractor parser
    req = query_extractor.extract(message)
    extracted_kwargs: dict[str, Any] = {}

    # Extract role: only one the user stated. req.role defaults to BUYER, which
    # would reset a seller's draft on every message.
    stated_role = query_extractor.detect_role(message)
    if stated_role:
        extracted_kwargs["role"] = stated_role.value

    # Extract product and category (filtering out pure role phrases and prepositions)
    if req.product:
        stopwords = {
            "buyer", "seller", "buy", "sell", "purchaser", "vendor", "supplier",
            "am buyer", "a buyer", "am a buyer", "am seller", "a seller", "am a seller",
            "buying", "selling", "purchasing", "looking to buy", "looking to sell",
            "want to buy", "want to sell",
            "par", "per", "pr", "rate", "price", "budget", "cost", "total", "approx",
            "around", "in", "at", "before", "after", "by", "on", "for", "of", "with",
            "dollar", "dollars", "rs", "inr", "usd", "kg", "pcs", "piece", "pieces",
            "tonne", "tonnes", "tons", "ton", "gm", "gram", "grams",
            "create", "rfq", "create rfq", "proceed", "post", "submit", "publish",
            "yes", "ok", "okay", "please", "this", "it",
            "mail", "mail id", "mail ids", "email", "email id", "email ids",
            "top", "top three", "top 3", "top two", "top 2", "top five", "top 5",
            "suits best", "suit best", "best fit", "which one", "which suits",
            "who is best", "compare", "matching", "counterparties",
            "crate rfq", "creat rfq", "make rfq", "post rfq", "submit rfq", "publish rfq",
            "mention", "mantion", "aslo", "also", "say", "tell", "inform", "clarify",
            "state", "note", "add", "message", "msg", "negotiate", "negotiation",
            "nagitiation", "nagotiation", "deal", "bargain", "seller", "supplier",
            "vendor", "user", "counterparty", "recipient", "away", "instead",
            "analyze", "analyse", "analysis", "listing", "listings", "typical",
            "market", "conditions", "condition", "benchmark", "benchmarks",
        }
        prod_norm = req.product.strip().lower()
        words = [w for w in prod_norm.split() if w not in ("i", "am", "a", "an", "the", "to", "want", "looking")]
        directive_words = {
            "mention", "mantion", "aslo", "also", "say", "tell", "inform", "clarify",
            "state", "note", "add", "message", "msg", "negotiate", "deal", "seller",
            "supplier", "user", "location", "away", "not",
            "analyze", "analyse", "listing", "market",
        }
        has_directive_word = any(w in directive_words for w in words)
        is_invalid = (
            prod_norm in stopwords
            or not words
            or all(w in stopwords for w in words)
            or bool(re.search(r"\brfq\b", prod_norm))
            or check_create_rfq_intent(prod_norm)
            or is_information_request(prod_norm)
            or check_counterparty_message_intent(prod_norm)
            or has_directive_word
        )

        existing_name = (draft.get("product_details") or {}).get("name")
        if not is_invalid:
            extracted_kwargs["product_name"] = req.product
            if req.category:
                extracted_kwargs["category"] = req.category
            elif config.infer_category_from_product:
                inferred = query_extractor._infer_category(req.product)
                if inferred:
                    extracted_kwargs["category"] = inferred
        elif existing_name:
            extracted_kwargs["product_name"] = existing_name

    # Extract location
    if req.city:
        extracted_kwargs["city"] = req.city
    if req.state:
        extracted_kwargs["state"] = req.state
    if req.country:
        extracted_kwargs["country"] = req.country

    # Extract quantity
    if req.quantity_value is not None:
        extracted_kwargs["quantity_value"] = float(req.quantity_value)
        extracted_kwargs["quantity_unit"] = req.quantity_unit or "pcs"

    # Extract price
    if req.price_amount is not None:
        extracted_kwargs["price_amount"] = float(req.price_amount)
        extracted_kwargs["price_currency"] = req.price_currency or config.default_currency
        extracted_kwargs["price_per_unit"] = req.price_per_unit

    # Extract deadline
    if req.deadline_days is not None:
        extracted_kwargs["deadline_days"] = req.deadline_days

    # Extract attributes
    if req.attributes:
        extracted_kwargs["product_details"] = req.attributes

    # Apply through the unified tool function
    return update_rfq_draft(existing_draft=draft, **extracted_kwargs)


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


# =============================================================================
# RFQ Payload Builder (explicit-command intent checks live in services.chat_permissions)
# =============================================================================

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
    prod_name = product_details.get("name")
    if prod_name:
        p_lower = str(prod_name).strip().lower()
        if re.search(r"\brfq\b", p_lower) or check_create_rfq_intent(p_lower):
            # The product name was corrupted with an RFQ creation phrase (e.g. "crate rfq")
            salvaged = None
            if title:
                # E.g. "RFQ: 1,299 kg Organic Green Fuji Apples – Indore (23-day delivery)"
                clean_title = re.sub(r"^RFQ:\s*", "", title, flags=re.IGNORECASE)
                clean_title = re.sub(r"\s*[–-].*$", "", clean_title)
                clean_title = re.sub(
                    r"^[\d,.]+\s*(?:kg|pcs|tonnes?|tons?|boxes|units?)\s*",
                    "",
                    clean_title,
                    flags=re.IGNORECASE,
                ).strip()
                if clean_title and not re.search(r"\brfq\b", clean_title.lower()):
                    salvaged = clean_title
            product_details["name"] = salvaged or category
            if re.search(r"\brfq\b", title.lower()) or check_create_rfq_intent(title.lower()):
                title = product_details["name"]

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


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

from pydantic import BaseModel, ConfigDict, Field

from models.enums import RFQRole, RFQStatus
from schemas.common import Deadline, Location, Money, Quantity
from schemas.rfq import RFQCreate
from schemas.rfq_structure import RFQStructure
from services import query_extractor

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
            and not check_match_query_intent(norm_p)
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
        or check_match_query_intent(message)
        or check_analysis_query_intent(message)
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
            or check_match_query_intent(prod_norm)
            or check_analysis_query_intent(prod_norm)
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
    if not has_product and not has_category:
        missing_required.append("product/category")

    required_complete = len(missing_required) == 0

    # Determine next required question in strict sequence:
    # 1. Role first
    # 2. Product / Category next
    next_required_prompt: Optional[str] = None
    if not has_role:
        next_required_prompt = "role"
    elif not has_product and not has_category:
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
        "has_product": has_product or has_category,
        "has_category": has_category,
        "required_complete": required_complete,
        "missing_required": missing_required,
        "next_required_prompt": next_required_prompt,
        "collected_optional": collected_optional,
        "missing_optional": missing_optional,
    }


# =============================================================================
# RFQ Creation Intent and Payload Builder
# =============================================================================
_CREATE_VERB = r"(?:create|crate|creat|craete|cerate|creste|make|post|submit|publish|generate|raise|rais|raize|riase)"

CREATE_INTENT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        rf"\b{_CREATE_VERB}\s+(the\s+|this\s+|an?\s+|my\s+|that\s+)?(rfq|listing)\b",
        r"\b(create|crate|creat|craete|cerate|creste)\s+(it|this)\b",
        r"\byes\s*,?\s*(please\s*)?(create|crate|creat|craete|cerate|publish|post|submit)\b",
        r"\bproceed\s+(to\s+|and\s+)?(create|crate|creat|craete|cerate|publish|post|submit)\b",
        r"\bproceed\s+with\s+((creating|crating|publishing)\s+)?(the\s+|this\s+|my\s+)?(rfq|listing)\b",
        r"\b(post|submit|publish)\s+(it|this)\b",
        r"\bconfirm\s+and\s+(create|crate|creat|publish|submit)\b",
        r"\bgo\s+ahead\s+and\s+(create|crate|creat|publish|submit|post)\b",
        r"^(yes|create|crate|creat|proceed|publish|submit)(\s+(the\s+)?rfq)?[.!]*$",
        # "raise the rfq", "rais the rfq", "raise it"
        r"\b(raise|rais|raize|riase|rise)\s+(the\s+|this\s+|an?\s+|my\s+)?(rfq|listing|request|it)\b",
        # Hinglish: "rfq bana do", "haan bana do", "publish kar do", "create kar do"
        r"\b(rfq|listing)\s+(bana|banao|banado|bana\s*do|bana\s+dijiye|bana\s+dena|create\s+kar|publish\s+kar|post\s+kar|daal|dal)\b",
        r"\b(bana|banao)\s+(do|de|dijiye|dena)\b",
        r"\bbanado\b",
        r"\b(create|publish|post|submit|raise|live|upload)\s+kar\s*(do|de|dijiye|dena|dain|den)\b",
        r"\b(create|publish|post|submit)\s+kardo\b",
    ]
]

# "don't create", "not ready to proceed", "rfq mat banao", "abhi nahi".
_CREATE_NEGATION_RE = re.compile(
    r"\b(don'?t|dont|do\s+not|does\s+not|didn'?t|not|never|no\s+need|without|hold\s+off|stop|avoid|"
    r"mat|nahi|nahin|nai)\b",
    re.IGNORECASE,
)
# A polite request phrased as a question still asks for the action.
_CREATE_REQUEST_QUESTION_RE = re.compile(
    rf"^\s*(please\s+)?(can|could|would|will)\s+(you|u)\s+(please\s+)?(now\s+)?{_CREATE_VERB}\b"
    r"|^\s*kya\s+(aap|ap|tum)\b.*\b(bana|create|publish|post)\b.*\b(sakte|sakti|sakoge|doge|denge|do)\b",
    re.IGNORECASE,
)
_QUESTION_START_RE = re.compile(
    r"^\s*(how|what|why|when|where|which|who|whom|whose|should|shall|is|are|am|was|were|do|does|did|"
    r"can\s+i|could\s+i|may\s+i|would\s+it|will\s+it|is\s+it|kya|kaise|kyun|kab)\b",
    re.IGNORECASE,
)
_BARE_AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|ok|okay|sure|haan|han|ha|ji|ji\s+haan|confirm|confirmed|go\s+ahead|do\s+it)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_NON_RFQ_OBJECT_RE = re.compile(
    r"\b(message|msg|mail|email|letter|note|reply|description|table|summary|list|chart|report)\b", re.IGNORECASE
)
_ASKED_TO_CREATE_RE = re.compile(r"\b(create|publish|post|submit|raise)\b", re.IGNORECASE)


def is_question(user_message: str) -> bool:
    """True for an interrogative message ("should I publish it?", "how do I ...")."""
    text = (user_message or "").strip()
    return bool(text) and (text.endswith("?") or bool(_QUESTION_START_RE.search(text)))


def is_negated_request(user_message: str) -> bool:
    return bool(_CREATE_NEGATION_RE.search(user_message or ""))


def check_create_rfq_intent(user_message: str, previous_assistant: Optional[str] = None) -> bool:
    """True only for an explicit request to publish the RFQ now.

    Negated ("don't create the rfq yet", "I am not ready to proceed") and
    questioning ("should I publish it or wait?", "how do I create a good
    description?") messages never count, apart from polite requests such as
    "can you create the rfq?". A bare "yes"/"haan" counts only when the previous
    assistant turn offered to create or publish (when that turn is known).
    """
    if not user_message:
        return False
    text = user_message.strip()
    if _CREATE_NEGATION_RE.search(text):
        return False
    if is_question(text) and not _CREATE_REQUEST_QUESTION_RE.search(text):
        return False
    if previous_assistant is not None and _BARE_AFFIRMATIVE_RE.match(text):
        return bool(_ASKED_TO_CREATE_RE.search(previous_assistant))
    # "create a message for the seller", "message bana do": the object is not the RFQ.
    if _NON_RFQ_OBJECT_RE.search(text) and not re.search(r"\b(rfq|listing)\b", text, re.IGNORECASE):
        return False
    return any(rx.search(text) for rx in CREATE_INTENT_REGEXES)


def is_bare_affirmative(user_message: str) -> bool:
    return bool(_BARE_AFFIRMATIVE_RE.match(user_message or ""))


MATCH_QUERY_INTENT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(mail\s*ids?|emails?|email\s*ids?|e-mails?)\b",
        r"\b(give\s+me|show\s+me|get|tell\s+me|what\s+is|what\s+are|list)\b.*\b(mail|email|contact|phone|address)\b",
        r"\b(top\s+(three|3|two|2|four|4|five|5|ten|10|one|1|\d+))\b",
        r"\bwhich\s+(one|supplier|buyer|seller|match|candidate)?\s*(suits?|fits?|is)\s*(best|better)\b",
        r"\b(suits?|fit)\s*best\b",
        r"\bbest\s+(fit|suit|match|choice|option|counterparty)\b",
        r"\bbest\s+from\s+(this|the|these)?\s*(top\s+)?(three|3|five|5|\d+)?\b",
        r"\bcompare\b.*\b(supplier|buyer|seller|candidate|match|price|quote|three|3|five|5|\d+)\b",
        r"\b(who|which)\s+is\s+(the\s+)?(cheapest|best|closest|most\s+reliable)\b",
        r"\bmatching\s+(suppliers?|sellers?|buyers?|candidates?|counterparties?)\b",
        r"\b(view|show|display|list|find|give\s+me|get|browse|search)\s+(the\s+|any\s+|all\s+|some\s+)?(rfqs?|listings?|matches|matching|suppliers?|buyers?|sellers?|counterparties?)\b",
        r"\b(rfqs?|listings?)\s+(link|links|url|urls)\b",
        r"\b(link|links|url|urls)\s+(of|for|to)\s+(the\s+|any\s+)?(rfqs?|listings?|counterparties?|suppliers?|buyers?|candidates?)\b",
        r"\b(show|give|provide|send)\s+.*(rfq|rfqs|listing|listings)\b",
        r"\b(connection\s+requests?|connect\s+to|connect\s+with)\b",
        # "show me sellers of basmati rice", "find us buyers for cotton"
        r"\b(view|show|display|list|find|give|get|browse|search|recommend|suggest)\s+(me\s+|us\s+)?(the\s+|any\s+|all\s+|some\s+|a\s+few\s+|top\s+)?(rfqs?|listings?|matches|suppliers?|buyers?|sellers?|vendors?|manufacturers?|dealers?|counterparties?)\b",
        # "who sells basmati rice?", "who is buying cotton"
        r"\bwho\s+(sells|supplies|stocks|manufactures|makes|exports|deals\s+in|buys|imports|is\s+(selling|supplying|buying)|are\s+(selling|supplying|buying)|can\s+(supply|sell|provide))\b",
        r"\b(any|are\s+there\s+(any)?)\s+(sellers?|suppliers?|buyers?|vendors?|manufacturers?)\s+(of|for|selling|supplying|buying)\b",
        # Hinglish: "koi supplier hai basmati ka?", "supplier chahiye"
        r"\b(koi|kaun|kon|kaunsa|konsa)\s+(sa\s+)?(supplier|seller|vendor|buyer|dealer|manufacturer|wholesaler)s?\b",
        r"\b(supplier|seller|dealer|vendor|buyer)s?\s+(chahiye|batao|bataiye|milega|milenge|dikhao|dikhaiye)\b",
    ]
]


def check_match_query_intent(user_message: str) -> bool:
    """Check if the user message expresses inquiry about matched suppliers/buyers."""
    if not user_message:
        return False
    text = user_message.strip()
    return any(rx.search(text) for rx in MATCH_QUERY_INTENT_REGEXES)


ANALYSIS_QUERY_INTENT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # Listing analysis / evaluation requests
        r"\b(analy[sz]e|evaluat(e|ion)|assess|review)\b.*\b(listing|order|rfq|deal|offer|quote|quotation|buyer|seller|supplier|product|price|market)\b",
        # Market comparisons, conditions, and typical pricing
        r"\b(how\s+does\s+this\s+compare|compare\s+(this\s+)?with|typical\s+market|market\s+conditions?|market\s+rate|market\s+price|market\s+benchmark|price\s+range)\b",
        r"\b(what\s+does\s+the\s+.*market\s+look\s+like|how\s+competitive\s+is\s+the|market\s+gap|market\s+density|competitor\s+density)\b",
        # Trade advisory and Incoterms / KYC / negotiation guidance
        r"\b(what\s+is\s+the\s+difference\s+between|explain)\b.*\b(fob|cif|exw|ddp|cfr|incoterms?)\b",
        r"\b(kyc\s+checklist|supplier\s+kyc|supplier\s+verification|red\s+flags?\s+to\s+watch)\b",
        r"\b(how\s+can\s+i\s+negotiate\s+better|counter-offer\s+strategy|payment\s+terms?\s+advice)\b",
        r"\b(is\s+my\s+asking\s+price\s+competitive|am\s+i\s+priced\s+right|total\s+cost\s+including\s+shipping)\b",
        r"\b(customs\s+documentation|transit\s+time\s+estimate|packaging\s+advice|landed\s+cost\s+calculator)\b",
        r"\b(export\s+opportunities|cross-border\s+demand|trusted\s+supplier\s+criteria)\b",
    ]
]


def check_analysis_query_intent(user_message: str) -> bool:
    """Check if the user message is an analytical, market research, or informational business query."""
    if not user_message:
        return False
    text = user_message.strip()
    return any(rx.search(text) for rx in ANALYSIS_QUERY_INTENT_REGEXES)


_CLOSE_VERB = r"(?:close|closed|closing|cancel|cancell?ing|deactivate|remove|delete|withdraw|take\s+down|shut\s+down|unpublish|band\s+kar)"
_CLOSE_OBJECT = r"(?:rfqs?|listings?|postings?|posts?|requirements?|orders?|requests?)"

CLOSE_RFQ_INTENT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # "close my cotton rfq", "cancel the order", "shut down that listing I posted"
        rf"\b{_CLOSE_VERB}\s+(?:(?:this|the|my|our|that|active|current)\s+)?(?:[\w-]+\s+){{0,3}}?{_CLOSE_OBJECT}\b",
        r"^(close|cancel|deactivate|remove)(\s+this)?(\s+rfq)?[.!]*$",
        # Hinglish: "rfq band kar do", "listing hata do"
        rf"\b{_CLOSE_OBJECT}\s+(band|hata|hatao|cancel|close)\s*(kar|karo|kardo|do|de|dijiye)?\b",
    ]
]

# "close the deal" is negotiation, not closing a listing.
_CLOSE_DEAL_RE = re.compile(r"\bclos(e|ing)\s+(the\s+|this\s+|a\s+)?deal\b", re.IGNORECASE)


def check_close_rfq_intent(user_message: str) -> bool:
    """Check if the user message expresses intent to close or cancel an RFQ."""
    if not user_message:
        return False
    text = user_message.strip()
    if _CLOSE_DEAL_RE.search(text) and not re.search(r"\b(rfq|listing)\b", text, re.IGNORECASE):
        return False
    if _CREATE_NEGATION_RE.search(text):
        return False
    if is_question(text) and not re.match(
        rf"^\s*(please\s+)?(can|could|would|will)\s+(you|u)\s+(please\s+)?{_CLOSE_VERB}\b", text, re.IGNORECASE
    ):
        return False
    return any(rx.search(text) for rx in CLOSE_RFQ_INTENT_REGEXES)


_CP_TARGET = r"(seller|supplier|buyer|counterparty|vendor|them|him|her|user|that\s+user|this\s+user|counter\s+user)s?"

COUNTERPARTY_MESSAGE_INTENT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # Negotiation keywords and typos: negotiate, nagitiation, nagotiation, natotiation, natogiation, draft, negociate, bargain, deal
        r"\b(send|write|drop|deliver|draft|compose|prepare)\s+(a\s+|an\s+|the\s+|this\s+|that\s+|my\s+)?(nagitiation|nagotiation|natotiation|natogiation|negotiation|bargain|counter|offer|proposal|deal|discount)?\s*(message|msg|text|note|email|mail)?\s*(to|for)\s+(the\s+|this\s+|that\s+|our\s+|my\s+)?(seller|supplier|buyer|counterparty|vendor|them|him|her|user|that\s+user|this\s+user|counter\s+user)s?\b",
        # "send the message to this buyer", "write a short note for the supplier"
        r"\b(send|write|draft|compose|prepare|drop)\b[^.?!\n]{0,40}\b(message|msg|mail|email|note|reply)\b[^.?!\n]{0,30}\b(to|for)\s+(the\s+|this\s+|that\s+|our\s+|my\s+)?(seller|supplier|buyer|counterparty|vendor)s?\b",
        r"\b(draft|compose|prepare|write)\s+(a\s+)?(message|msg|proposal|counter-offer|offer|negotiation|natotiation)\s+(for|to)\s+(the\s+)?(seller|supplier|buyer|counterparty|vendor|them|user|counter\s+user)\b",
        r"\b(message|contact|reach\s+out\s+to|notify|ping|chat\s+with|reply\s+to|write\s+to)\s+(the\s+|this\s+|that\s+|my\s+)?(seller|supplier|buyer|counterparty|vendor|them|user|that\s+user|this\s+user|counter\s+user)s?\b",
        r"\b(ask|tell|request|inquire|inform)\s+(the\s+)?(seller|supplier|buyer|counterparty|vendor|them|user|that\s+user|this\s+user|counter\s+user)\b",
        # "negotiate with the vendor", "help me close the deal with the supplier"
        rf"\b(negotiate|nagitiate|nagotiate|natotiate|natogiate|negociate|bargain)\s+(with\s+)?(the\s+|this\s+|that\s+|my\s+|our\s+)?{_CP_TARGET}\b",
        rf"\bdeal\s+with\s+(the\s+|this\s+|that\s+|my\s+|our\s+)?{_CP_TARGET}\b",
        r"\b(can\s+we|can\s+you)\s+deal\s+(it\s+)?(in|at|for)\b",
        r"\b(can\s+you\s+)?(reduce|decrease|lower|discount)\s+(the\s+)?price\b.*\b(to|by|in|for)\b",
        r"\b(send|say|write|draft)\s+to\s+(the\s+)?(seller|supplier|buyer|user|that\s+user|counter\s+user)\b",
        r"\b(behaviour|behavior|tone)\s+should\s+be\s+(like\s+)?polite\b",
        # Follow-up amendments and additions (e.g., "also mention that...", "aslo mantion that...")
        r"\b(also|aslo|and|please)?\s*(mention|mantion|clarify|state|note|highlight|stress)\s+(that|to\s+them)\b",
        r"\b(also|aslo)\s+(mention|mantion|clarify|note|highlight|say|write|include|specify)\b",
        r"\b(also|aslo|and|please)?\s*(tell|ask|inform|let)\s+(them|him|her|the\s+seller|the\s+supplier|the\s+buyer|the\s+user|that\s+user|counter\s+user)\b",
        r"\b(also|aslo|and)?\s*(say|write)\s+that\b",
        r"\b(also|aslo|and)?\s*(add|put)\s+(that|in\s+the\s+message|to\s+the\s+message)\b",
        r"\b(update|amend|revise|edit|change)\s+(the\s+)?(message|msg|note|text|draft\s+message|letter)\b",
        r"\b(send|dispatch)\s+(an?\s+)?(updated|new|another|follow-up|followup)\s+(message|msg)\b",
        # Counterparty location / terms corrections
        r"\b(location|address|delivery\s+place)\s+is\s+not\b",
        r"\bnot\s+[a-zA-Z]+(\s+[a-zA-Z]+)?\s+(its?|it\s+is)?\s*\d+\s*(km|kg|kilometer|kilometers|miles)\s+away\b",
        # Hinglish: "supplier ko 10% discount ke liye message kar do", "seller ko bolo"
        r"\b(seller|supplier|buyer|vendor|dealer|party)\s+ko\b[^.?!\n]{0,60}\b(message|msg|mail|bol|bolo|bolna|likh|likho|likhna|puch|pucho|poochho|batao|bata\s+do|kaho|keh\s+do)\b",
        r"\b(unko|usko|unhe|inko|isko)\b[^.?!\n]{0,60}\b(message|msg|mail|bol|bolo|bolna|likh|likho|puch|pucho|batao|kaho)\b",
        r"\b(message|msg|mail)\s+(kar|karo|kardo|kar\s+do|bhej|bhejo|bhej\s+do|likh\s+do|bana\s+do)\b",
    ]
]

# Inside an active thread, a message counts as an amendment only when it
# addresses the counterparty or the drafted message.
_THREAD_ADDRESS_RE = re.compile(
    r"\b(them|they|him|her|seller|supplier|vendor|buyer|counterparty|message|msg|letter|note|reply|draft\s+message)\b",
    re.IGNORECASE,
)
_THREAD_AMEND_START_RE = re.compile(
    r"^\s*(also|aslo|tell|ask|say|mention|mantion|clarify|inform|let\s+them|and\s+(say|mention|tell|add))\b",
    re.IGNORECASE,
)
_THREAD_CORRECTION_RE = re.compile(r"\b(away\s+from|instead\s+of|rather\s+than)\b", re.IGNORECASE)


def check_counterparty_message_intent(
    user_message: str,
    has_active_connection: bool = False,
    has_previous_counterparty_msg: bool = False,
) -> bool:
    """Check if the user message expresses intent to message/negotiate with a seller or counterparty.

    Supports initial outreach, price negotiation, follow-up additions (e.g. 'also mention that...'),
    and contextual follow-ups when an active counterparty thread exists. Draft edits
    such as "please include delivery to Pune" are not messages unless a thread is
    active and the text addresses the counterparty.
    """
    if not user_message:
        return False
    text = user_message.strip()
    if any(rx.search(text) for rx in COUNTERPARTY_MESSAGE_INTENT_REGEXES):
        return True

    # If in an active counterparty thread or follow-up turn, match conversational amendments.
    # "update the rfq from buyer to seller" edits the user's own listing, not the thread.
    edits_own_listing = bool(re.search(r"\b(rfq|draft|listing|role)\b", text, re.IGNORECASE)) and not re.search(
        r"\b(message|msg|mention|mantion|tell|ask|inform)\b", text, re.IGNORECASE
    )
    if (has_active_connection or has_previous_counterparty_msg) and not edits_own_listing:
        if _THREAD_AMEND_START_RE.search(text) or _THREAD_CORRECTION_RE.search(text):
            return True
        if _THREAD_ADDRESS_RE.search(text) and not is_question(text):
            return True

    return False


def build_rfq_create_payload(
    draft: dict[str, Any],
    title: Optional[str] = None,
    status: str = "active",
    notes: Optional[str] = None,
) -> RFQCreate:
    """Transform an accumulated RFQ draft dictionary into a validated RFQCreate model."""
    role_str = (draft.get("role") or "buyer").lower()
    role = RFQRole.BUYER if role_str == "buyer" else RFQRole.SELLER

    category = draft.get("category")
    if not category:
        prod_name = (draft.get("product_details") or {}).get("name")
        if prod_name:
            category = infer_category(prod_name)
        if not category:
            category = "General Goods"

    # Auto-generate a descriptive title if not explicitly provided
    if not title:
        existing_title = draft.get("title")
        if existing_title:
            title = existing_title
        else:
            role_prefix = "Procurement of" if role == RFQRole.BUYER else "Supply of"
            prod_name = (draft.get("product_details") or {}).get("name") or category
            qty_str = ""
            if draft.get("quantity") and draft["quantity"].get("value"):
                qty_val = draft["quantity"]["value"]
                qty_unit = draft["quantity"].get("unit", "")
                try:
                    qty_str = f"{int(qty_val):,} {qty_unit}".strip()
                except Exception:
                    qty_str = f"{qty_val} {qty_unit}".strip()
            loc_str = ""
            if draft.get("location") and draft["location"].get("city"):
                loc_str = f"in {draft['location']['city']}"

            parts = [p for p in [role_prefix, qty_str, prod_name.title(), loc_str] if p]
            title = " ".join(parts)

    description = notes or draft.get("description")
    if not description:
        prod_name = (draft.get("product_details") or {}).get("name") or category
        description = f"RFQ for {prod_name} created via AI Business Assistant."

    quantity = None
    if draft.get("quantity") and isinstance(draft["quantity"], dict):
        try:
            quantity = Quantity(**draft["quantity"])
        except Exception:
            quantity = None

    minimum_order = None
    if draft.get("minimum_order") and isinstance(draft["minimum_order"], dict):
        try:
            minimum_order = Quantity(**draft["minimum_order"])
        except Exception:
            minimum_order = None

    price_target = None
    if draft.get("price_target") and isinstance(draft["price_target"], dict):
        try:
            price_target = Money(**draft["price_target"])
        except Exception:
            price_target = None

    location = None
    if draft.get("location") and isinstance(draft["location"], dict):
        try:
            location = Location(**draft["location"])
        except Exception:
            location = None

    deadline = None
    if draft.get("deadline") and isinstance(draft["deadline"], dict):
        try:
            deadline = Deadline(**draft["deadline"])
        except Exception:
            deadline = None

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

    rfq_status = (
        RFQStatus.ACTIVE
        if (status or "").lower() == "active"
        else RFQStatus.DRAFT
    )

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


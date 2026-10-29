"""Editing a saved RFQ from the chat: the same rules as the form's /rfqs/:id/edit page.

The chat keeps the RFQ being edited as an ordinary draft (so every drafting helper works on it)
plus a snapshot of what was loaded. Saving sends only the fields that differ from the snapshot,
as an ``RFQPatch`` (no role, no status), exactly like the form's PATCH.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import ValidationError

from schemas.common import normalise_decimal
from schemas.rfq import RFQPatch
from services.chat_agent.rfq_draft import RFQDraftInvalid, _validation_errors

# Draft fields the form can PATCH. Role is locked and status has its own endpoints.
PATCH_FIELDS = (
    "category", "title", "description", "quantity", "minimum_order", "price_target", "location", "deadline",
    "product_details",
)


def rfq_to_draft(rfq: Any) -> dict[str, Any]:
    """An RFQ row as a chat draft: the inverse of ``rfq_draft.build_rfq_create_payload``."""
    draft: dict[str, Any] = {
        "role": rfq.role.value,
        "category": rfq.category,
        "title": rfq.title,
        "product_details": json.loads(json.dumps(rfq.product_details or {}, default=str)),
    }
    if rfq.description:
        draft["description"] = rfq.description
    if rfq.quantity_value is not None:
        draft["quantity"] = {"value": normalise_decimal(rfq.quantity_value), "unit": rfq.quantity_unit or "units"}
    if rfq.min_order_value is not None:
        draft["minimum_order"] = {"value": normalise_decimal(rfq.min_order_value), "unit": rfq.min_order_unit or "units"}
    if rfq.price_amount is not None:
        price: dict[str, Any] = {"amount": normalise_decimal(rfq.price_amount), "currency": rfq.price_currency or "INR"}
        if rfq.price_per_unit:
            price["per_unit"] = rfq.price_per_unit
        draft["price_target"] = price
    # Coordinates are not carried: a changed city is re-geocoded on save, an unchanged one is not sent.
    location = {k: v for k, v in (
        ("city", rfq.location_city), ("state", rfq.location_state), ("country", rfq.location_country)
    ) if v}
    if location:
        draft["location"] = location
    if rfq.deadline_at is not None:
        draft["deadline"] = {"date": rfq.deadline_at.date().isoformat()}
    return draft


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def changed_fields(draft: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Patchable fields whose value differs from the loaded RFQ. A cleared field is not a change (PATCH cannot null)."""
    out: dict[str, Any] = {}
    for field in PATCH_FIELDS:
        new = draft.get(field)
        if new in (None, "", {}):
            continue
        if _canon(new) != _canon(snapshot.get(field)):
            out[field] = new
    return out


def build_rfq_patch(draft: dict[str, Any], snapshot: dict[str, Any]) -> Optional[RFQPatch]:
    """An ``RFQPatch`` of the changes, None when nothing changed. Invalid values raise ``RFQDraftInvalid``."""
    changed = changed_fields(draft, snapshot)
    if not changed:
        return None
    if "category" in changed:
        changed["category"] = str(changed["category"])[:120]
    if "title" in changed:
        changed["title"] = str(changed["title"]).strip()[:300]
    if "minimum_order" in changed and snapshot.get("role") != "seller":
        changed.pop("minimum_order")  # the form shows the minimum order for sell-side RFQs only
        if not changed:
            return None
    try:
        return RFQPatch(**changed)
    except ValidationError as exc:
        raise RFQDraftInvalid(_validation_errors(exc)) from exc


def enforce_locked_role(draft: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """The role cannot change while editing (the form locks it). True when the draft tried to change it."""
    locked = snapshot.get("role")
    if locked and draft.get("role") and draft["role"] != locked:
        draft["role"] = locked
        return True
    if locked and not draft.get("role"):
        draft["role"] = locked
    return False

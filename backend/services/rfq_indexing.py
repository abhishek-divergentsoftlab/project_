"""Turn an RFQ row into the canonical text the embedding is generated from.

Nothing embeds raw JSON. The JSONB payload is rendered into a stable,
human-readable block first, so two RFQs describing the same thing produce
similar text regardless of which keys the extractor happened to emit.

``product_details`` values may be plain scalars or the normalised form
``{"value": ..., "unit": ..., "raw": ...}``; both render sensibly here, and the
normalised form is what later numeric matching will read.
"""

from decimal import Decimal
from typing import Any

from models.rfq import RFQ

# Keys that carry the product identity rather than an attribute of it.
_NAME_KEYS = ("name", "product", "product_name", "title")

# Suffix appended by the UI to flag an attribute for heavier matching weight.
_MUST_MATCH_SUFFIX = "__must_match"

# Rendered as a presence/absence word instead of "Yes"/"No" in tags.
_MAX_TAGS = 40

# Dimension keys in the order a person would say them. JSONB does not preserve
# key insertion order, so a size stored as {length, width, height} can come back
# as {width, height, length}; without a canonical order the same box renders
# "30x20x18" on write and "20x18x30" on re-index, churning its embedding.
_DIMENSION_ORDER = (
    "length", "width", "height", "depth", "diameter", "radius", "thickness",
)


def _humanise_key(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().title()


def _is_redundant(raw: str, rendered: str) -> bool:
    """True when ``raw`` adds no information the rendering does not already have."""
    squash = lambda s: "".join(s.lower().split())
    return squash(raw) in squash(rendered)


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        # Drop trailing zeros without falling into scientific notation:
        # Decimal("20") must render "20", not "2E+1".
        return format(value.normalize(), "f")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render_value(value: Any) -> str:
    """Render any product_details value as display text."""
    if value is None:
        return ""

    if isinstance(value, dict):
        raw = value.get("raw")
        inner = value.get("value")
        unit = value.get("unit")

        if inner is not None:
            if isinstance(inner, dict):
                # Dimensions: {"length": 30, "width": 20, "height": 18}
                ordered = [k for k in _DIMENSION_ORDER if k in inner]
                ordered += sorted(k for k in inner if k not in _DIMENSION_ORDER)
                joined = "x".join(
                    _render_scalar(inner[k]) for k in ordered if inner[k] is not None
                )
                rendered = f"{joined} {unit}".strip() if unit else joined
            else:
                rendered = f"{_render_scalar(inner)} {unit}".strip() if unit else _render_scalar(inner)
            # Prefer the normalised rendering. Keep the user's phrasing only
            # when it adds something -- "30x20x18 cm (30x20x18)" is noise.
            if raw and not _is_redundant(str(raw), rendered):
                return f"{rendered} ({raw})"
            return rendered

        if raw is not None:
            return _render_scalar(raw)

        return ", ".join(
            f"{_humanise_key(k)} {render_value(v)}" for k, v in value.items() if v is not None
        )

    if isinstance(value, (list, tuple)):
        parts = [render_value(v) for v in value]
        return ", ".join(p for p in parts if p)

    return _render_scalar(value)


def _product_name(product_details: dict[str, Any]) -> str | None:
    for key in _NAME_KEYS:
        value = product_details.get(key)
        if value:
            return render_value(value)
    return None


def build_search_text(rfq: RFQ) -> str:
    """The canonical block that gets embedded.

    Stable ordering matters: the same RFQ must always produce byte-identical
    text, or re-indexing churns embeddings for no reason.
    """
    details = rfq.product_details or {}
    lines: list[str] = [
        f"Role: {rfq.role.value.title()}",
        f"Category: {rfq.category}",
    ]

    name = _product_name(details)
    lines.append(f"Product: {name}" if name else f"Product: {rfq.title}")

    if rfq.quantity_value is not None:
        unit = rfq.quantity_unit or "units"
        lines.append(f"Quantity: {_render_scalar(rfq.quantity_value)} {unit}")

    if rfq.price_amount is not None:
        per = f"/{rfq.price_per_unit}" if rfq.price_per_unit else ""
        lines.append(
            f"Target price: {_render_scalar(rfq.price_amount)} {rfq.price_currency}{per}"
        )

    location = ", ".join(
        part
        for part in (rfq.location_city, rfq.location_state, rfq.location_country)
        if part
    )
    if location:
        lines.append(f"Location: {location}")

    if rfq.deadline_at is not None:
        lines.append(f"Deadline: {rfq.deadline_at.date().isoformat()}")

    attributes = [
        (key, value)
        for key, value in sorted(details.items())
        if key not in _NAME_KEYS and not key.endswith(_MUST_MATCH_SUFFIX)
        and value is not None and value != ""
    ]
    if attributes:
        lines.append("")
        lines.append("Attributes:")
        for key, value in attributes:
            rendered = render_value(value)
            if rendered:
                lines.append(f"{_humanise_key(key)}: {rendered}")

    if rfq.description:
        lines.append("")
        lines.append(f"Notes: {rfq.description}")

    return "\n".join(lines)


def build_match_text(rfq: RFQ) -> str:
    """What the product *is*, with none of the terms scored separately.

    ``search_text`` is the full listing, and it is the right thing to embed --
    but it is the wrong thing to measure similarity with. Location, quantity,
    price and deadline each have their own scoring dimension, so leaving them
    in the text counts them twice and, worse, makes two unrelated products in
    the same city look alike.
    """
    details = rfq.product_details or {}
    parts: list[str] = [rfq.category, _product_name(details) or "", rfq.title]

    for key, value in sorted(details.items()):
        if key in _NAME_KEYS or key.endswith(_MUST_MATCH_SUFFIX) or value is None or value == "":
            continue
        if isinstance(value, bool):
            parts.append(_humanise_key(key) if value else f"not {_humanise_key(key)}")
            continue
        parts.append(f"{_humanise_key(key)} {render_value(value)}")

    if rfq.description:
        parts.append(rfq.description)

    return " ".join(part for part in parts if part)


def build_search_tags(rfq: RFQ) -> list[str]:
    """Lowercase keyword aids, deduplicated, order preserved.

    Booleans become presence words -- ``printed: false`` yields "not printed",
    which is what somebody would actually type.
    """
    details = rfq.product_details or {}
    tags: list[str] = []

    def add(tag: str) -> None:
        cleaned = " ".join(tag.lower().split())
        if cleaned and cleaned not in tags:
            tags.append(cleaned)

    name = _product_name(details)
    if name:
        add(name)
    add(rfq.category)

    for key, value in sorted(details.items()):
        if key in _NAME_KEYS or key.endswith(_MUST_MATCH_SUFFIX) or value is None or value == "":
            continue
        if isinstance(value, bool):
            add(_humanise_key(key) if value else f"not {_humanise_key(key)}")
            continue
        rendered = render_value(value)
        if rendered and rendered not in {"Yes", "No"}:
            add(rendered)

    # Location is deliberately NOT tagged. It is already a structured, separately
    # scored dimension, and tagging it made every RFQ in the same city share a
    # tag -- which pulled corrugated boxes into a search for USB cables.
    return tags[:_MAX_TAGS]


def refresh_index_fields(rfq: RFQ) -> RFQ:
    """Recompute the derived search fields and flag the row for re-embedding.

    Called on every create and every edit: the typed columns, the JSONB payload
    and the text that represents them must never drift apart.
    """
    rfq.search_text = build_search_text(rfq)
    rfq.search_tags = build_search_tags(rfq)
    rfq.mark_for_reindex()
    return rfq

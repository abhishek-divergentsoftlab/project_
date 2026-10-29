"""Plain-language explanations of what the chat refused to store, from Requirements.flags, in English or Hinglish."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def _fmt(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    text = f"{number:,.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _crore_lakh(value: Any) -> str:
    """12,345,678 -> "1.23 crore"; Indian traders read big amounts that way."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if number >= Decimal(10_000_000):
        return f"{(number / Decimal(10_000_000)).quantize(Decimal('0.01')).normalize():f} crore"
    if number >= Decimal(100_000):
        return f"{(number / Decimal(100_000)).quantize(Decimal('0.01')).normalize():f} lakh"
    return _fmt(number)


_EN = {
    "quantity_nonpositive": "A quantity of \"{x}\" isn't valid: it has to be more than zero, so I have not recorded it.",
    "quantity_absurd": "\"{x}\" is far beyond any real order, so I have not recorded it.",
    "price_absurd": "\"{x}\" does not look like a real price, so I have not recorded it.",
    "date_invalid": "\"{x}\" is not a valid date, so I have not set a delivery deadline.",
    "date_past": "\"{x}\" is already in the past, so I have not set a delivery deadline.",
    "price_total_derived": "I took {cur} {total} as the total for {qty} {unit}, which is {cur} {per} per {unit}.",
    "price_total_pending": "Is {cur} {total} the total for the whole order? Once I know the quantity I will work out the per-unit price.",
    "quantity_unit_missing": "You gave {x} but no unit (kg, tonnes, pieces ...).",
    "location_unresolved": "I could not find \"{x}\" on my map, so I have not set it as the location.",
    "extra_items": "I also noticed {items}. I will draft {first} first, then we can draft the {second} right after.",
    "role_changed": "Switched this draft to {role}.",
}


_HI = {
    "quantity_nonpositive": "\"{x}\" valid quantity nahi hai (zero se zyada chahiye), isliye maine record nahi kiya.",
    "quantity_absurd": "\"{x}\" kisi real order se bahut bada hai, isliye maine record nahi kiya.",
    "price_absurd": "\"{x}\" real price nahi lagta, isliye maine record nahi kiya.",
    "date_invalid": "\"{x}\" valid date nahi hai, isliye maine delivery deadline set nahi kiya.",
    "date_past": "\"{x}\" beet chuki hai, isliye maine delivery deadline set nahi kiya.",
    "price_total_derived": "Maine {cur} {total} ko {qty} {unit} ka total maana, yaani {cur} {per} per {unit}.",
    "price_total_pending": "Kya {cur} {total} poore order ka total hai? Quantity pata chalte hi per-unit price nikaal dunga.",
    "quantity_unit_missing": "Aapne {x} bataya par unit (kg, tonne, pieces ...) nahi.",
    "location_unresolved": "Mujhe \"{x}\" nahi mila, isliye location set nahi ki.",
    "extra_items": "Maine {items} bhi dekha. Pehle {first} ka draft banaunga, phir {second} ka.",
    "role_changed": "Draft ab {role} ke roop mein set hai.",
}


def _t(lang: str, key: str, **kw: Any) -> str:
    table = _HI if lang == "hinglish" else _EN
    return table[key].format(**kw)


def describe_items(items: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{_fmt(i.get('quantity_value')) if i.get('quantity_value') not in (None, '', 'None') else ''} "
        f"{i.get('quantity_unit') or ''} {i.get('product')}".replace("  ", " ").strip() for i in items
    )


def notes_for_flags(flags: dict[str, Any], lang: str = "en", *, quantity_known: bool = False,
                    first_product: Optional[str] = None) -> list[str]:
    """One sentence per problem flagged on the user's message. Empty when the message was clean."""
    notes: list[str] = []
    if not flags:
        return notes
    for key in ("quantity_nonpositive", "quantity_absurd", "price_absurd", "date_invalid", "date_past"):
        if flags.get(key):
            notes.append(_t(lang, key, x=flags[key]))
    if flags.get("quantity_unit_missing") and not quantity_known:
        notes.append(_t(lang, "quantity_unit_missing", x=_fmt(flags["quantity_unit_missing"])))
    total = flags.get("price_total")
    if isinstance(total, dict):
        cur = total.get("currency") or "INR"
        if total.get("per_unit_amount"):
            notes.append(_t(
                lang, "price_total_derived", cur=cur, total=_crore_lakh(total["total"]), qty=_fmt(total.get("qty")),
                unit=total.get("unit") or "unit", per=_fmt(total["per_unit_amount"]),
            ))
        elif not quantity_known:
            notes.append(_t(lang, "price_total_pending", cur=cur, total=_crore_lakh(total["total"])))
    if flags.get("location_unresolved"):
        notes.append(_t(lang, "location_unresolved", x=str(flags["location_unresolved"]).strip().title()))
    extras = flags.get("extra_items")
    if extras:
        second = str(extras[0].get("product") or "next product")
        notes.append(_t(lang, "extra_items", items=describe_items(extras), first=first_product or "the first product", second=second))
    return notes

"""Localised recap and confirmation texts for binding actions (create, publish, edit, close), built from the draft's own values; and the draft hash that gates a confirmation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional

from services import chat_specs, spec_catalog
from services.chat_agent import draft_assist



_HASH_KEYS = (
    "role", "category", "title", "product_details", "quantity", "minimum_order", "price_target", "location",
    "deadline", "description",
)


_NOT_SPECS = {"name", "hsn_code", "hsn_source"}


def draft_hash(draft: Optional[dict[str, Any]]) -> str:
    """Hash of the parts of a draft the recap shows: any change to them invalidates a shown recap."""
    d = draft or {}
    payload = {k: d.get(k) for k in _HASH_KEYS if d.get(k) not in (None, "", {}, [])}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


REPLY_LANGUAGES = ("english", "hinglish", "hindi_devanagari")
_ALIASES = {"en": "en", "english": "en", "hinglish": "hinglish", "hi": "hindi_devanagari", "hindi": "hindi_devanagari",
            "hindi_devanagari": "hindi_devanagari", "devanagari": "hindi_devanagari"}


def has_devanagari(text: Optional[str]) -> bool:
    return any("\u0900" <= ch <= "\u097f" for ch in text or "")


def latin_lang(lang: Optional[str]) -> str:
    """"en" or "hinglish": for the texts that exist in Latin script only (Devanagari is answered in Hinglish there)."""
    return "hinglish" if lang in ("hinglish", "hindi_devanagari") else "en"


def pick_lang(message: str, sticky: Optional[str] = None, model_lang: Optional[str] = None) -> str:
    """The language of the texts the code writes: "en", "hinglish" (Latin script) or "hindi_devanagari".

    The model's own ``reply_language`` (english | hinglish | hindi_devanagari; legacy en / hi accepted) wins, with one
    mechanical script guard: Devanagari is never written unless the user's latest message contains Devanagari.
    Without a model language only the script is checked, and a short reply ("haan") keeps the conversation's language:
    code never guesses a register from words.
    """
    chosen = _ALIASES.get(str(model_lang or "").strip().lower())
    devanagari = has_devanagari(message)
    if chosen == "hindi_devanagari" and not devanagari:
        return "hinglish"
    if chosen:
        return chosen
    if devanagari:
        return "hindi_devanagari"
    if sticky == "hinglish" and len((message or "").split()) <= 3:
        return "hinglish"
    return "en"


def unfilled_specs(draft: dict[str, Any], suggestions: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The suggested specs the draft has no value for yet, must-match presets first (suggestion order otherwise)."""
    details = draft.get("product_details") or {}
    have = {draft_assist._norm_key(k) for k in details}
    skipped = {draft_assist._norm_key(k) for k in (suggestions or {}).get("skipped") or []}
    out: list[dict[str, Any]] = []
    for spec in (suggestions or {}).get("specs") or []:
        if not isinstance(spec, dict) or draft_assist._norm_key(spec.get("key") or "") in skipped:
            continue
        label = str(spec.get("label") or spec.get("key") or "").strip()
        if label and draft_assist._norm_key(label) not in have and draft_assist._norm_key(spec.get("key") or "") not in have:
            out.append(spec)
    return sorted(out, key=lambda sp: 0 if sp.get("must_match") else 1)


# ---------------------------------------------------------------------------
# Recap blocks
# ---------------------------------------------------------------------------
_L = {
    "en": {
        "intro": "Here is your RFQ draft:", "product": "Product", "type": "Type", "buying": "Buying", "selling": "Selling",
        "quantity": "Quantity", "min_order": "Minimum order", "price_buyer": "Target price", "price_seller": "Offer price",
        "location": "Location", "deadline_buyer": "Deadline", "deadline_seller": "Dispatch within",
        "within": "within {n} days", "days": "{n} days", "specs": "Specs", "must": "must match",
        "description": "Description", "not_set": "Not set", "per": "per", "category": "Category",
        "no_per": "per-unit not set", "yes": "yes", "no": "no",
        "q_publish": "Shall I publish it now, or save it as a draft?",
        "role_note": "your account can only post as a {r}",
        "edit_intro": "Here is what will change:", "q_save": "Save these changes?",
        "q_save_publish": "Save these changes and publish the RFQ?", "title": "Title",
        "nothing": "Nothing has changed yet.",
        "names": {"quantity": "Quantity", "min_order": "Minimum order", "price": "Price", "location": "Location",
                  "dispatch": "Deadline"},
        "role_word": {"buyer": "buyer", "seller": "seller"},
    },
    "hinglish": {
        "intro": "Aapka RFQ draft yeh hai:", "product": "Product", "type": "Type", "buying": "Khareedna", "selling": "Bechna",
        "quantity": "Quantity", "min_order": "Minimum order", "price_buyer": "Target price", "price_seller": "Offer price",
        "location": "Location", "deadline_buyer": "Deadline", "deadline_seller": "Dispatch",
        "within": "{n} din ke andar", "days": "{n} din mein", "specs": "Specs", "must": "match hona zaroori",
        "description": "Description", "not_set": "Set nahi hai", "per": "per", "category": "Category",
        "no_per": "per-unit set nahi", "yes": "haan", "no": "nahi",
        "q_publish": "Ise abhi publish karun ya draft mein save karun?",
        "role_note": "aapka account sirf {r} ke roop mein post kar sakta hai",
        "edit_intro": "Yeh badlav honge:", "q_save": "Ye changes save karun?",
        "q_save_publish": "Ye changes save karke RFQ publish karun?", "title": "Title",
        "nothing": "Abhi koi badlav nahi hai.",
        "names": {"quantity": "Quantity", "min_order": "Minimum order", "price": "Price", "location": "Location",
                  "dispatch": "Deadline"},
        "role_word": {"buyer": "buyer", "seller": "seller"},
    },
}


def _num(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if isinstance(v, int) else str(v)


def _qty(q: Any) -> str:
    if not isinstance(q, dict) or q.get("value") is None:
        return ""
    return f"{_num(q['value'])} {q.get('unit') or ''}".strip()


def _price(p: Any, t: dict[str, Any]) -> str:
    """"USD 20 per kg"; without a per-unit the recap says so instead of guessing one."""
    if not isinstance(p, dict) or p.get("amount") is None:
        return ""
    unit = chat_specs.normalize_per_unit(p.get("per_unit"))
    tail = f" {t['per']} {unit}" if unit else f" ({t['no_per']})"
    return f"{p.get('currency') or 'INR'} {_num(p['amount'])}{tail}"


def _loc(loc: Any) -> str:
    if not isinstance(loc, dict):
        return ""
    return ", ".join(str(loc[k]) for k in ("city", "state", "country") if loc.get(k))


def _deadline(dl: Any, t: dict[str, Any], seller: bool) -> str:
    if not isinstance(dl, dict):
        return ""
    if dl.get("date"):
        return str(dl["date"])
    if dl.get("in_days"):
        return t["days" if seller else "within"].format(n=dl["in_days"])
    return ""


def _spec_value(v: Any, t: dict[str, Any]) -> str:
    if isinstance(v, bool):
        return t["yes"] if v else t["no"]
    if isinstance(v, (list, tuple, set)):
        return ", ".join(_spec_value(i, t) for i in v if i not in (None, ""))
    if isinstance(v, dict):
        return ", ".join(f"{k} {_spec_value(i, t)}" for k, i in v.items())
    return _num(v) if isinstance(v, (int, float)) else str(v)


def spec_label(key: str, labels: Optional[dict[str, str]] = None) -> str:
    """What to call a spec key to the user: the suggestion's label ("Apple Variety"), else the catalogue's, else the key in words."""
    k = str(key)
    if labels and labels.get(k):
        return labels[k]
    common = spec_catalog._COMMON_BY_KEY.get(k)
    if common:
        return spec_catalog.display_label(common["label"])
    return k.replace("_", " ").strip().title() if "_" in k or k.islower() else k


def spec_labels(specs: Optional[Iterable[dict[str, Any]]]) -> dict[str, str]:
    """key -> display label (no unit) for a suggestion list."""
    out: dict[str, str] = {}
    for sp in specs or []:
        if isinstance(sp, dict) and sp.get("key"):
            out[str(sp["key"])] = spec_catalog.display_label(str(sp.get("label") or sp["key"]))
    return out


def _specs(details: Any, t: dict[str, Any], labels: Optional[dict[str, str]] = None) -> list[tuple[str, str, bool]]:
    """The recap's specs as (label, value, must): never the form's own fields, never a raw dict or list repr."""
    out: list[tuple[str, str, bool]] = []
    details = details if isinstance(details, dict) else {}
    for k, v in details.items():
        if (
            str(k) in _NOT_SPECS or str(k).endswith(draft_assist.MUST_MATCH_SUFFIX)
            or chat_specs.is_core_key(k) or v in (None, "", [], {})
        ):
            continue
        out.append((spec_label(str(k), labels), _spec_value(v, t), bool(details.get(f"{k}{draft_assist.MUST_MATCH_SUFFIX}"))))
    return out


def _specs_text(details: Any, t: dict[str, Any], labels: Optional[dict[str, str]] = None) -> str:
    """"Packaging: Carton · Pack size: 20 kg · Certifications: Organic, FSSAI (must match)"."""
    return " · ".join(
        f"{k}: {v}" + (f" ({t['must']})" if must else "") for k, v, must in _specs(details, t, labels)
    )


def build_recap(
    draft: dict[str, Any], lang: str = "en", role_note: Optional[str] = None, labels: Optional[dict[str, str]] = None,
    question: Optional[str] = None,
) -> str:
    """The draft as a short bullet list plus the publish-or-draft question. ``role_note`` names the one role the account can post as."""
    lang = latin_lang(lang)
    t = _L[lang]
    role = str(draft.get("role") or "").lower()
    seller = role == "seller"
    name = draft_assist.product_name(draft)
    lines = [t["intro"]]
    head = name.title() if name else ""
    extras = [x for x in (draft.get("category"), f"HSN {draft_assist.user_hsn(draft)}" if draft_assist.user_hsn(draft) else None) if x]
    lines.append(f"- **{t['product']}:** {head}" + (f" ({', '.join(str(e) for e in extras)})" if extras else ""))
    role_line = t["selling" if seller else "buying"]
    if role_note:
        role_line += f" ({t['role_note'].format(r=t['role_word'][role if role in t['role_word'] else 'buyer'])})"
    lines.append(f"- **{t['type']}:** {role_line}")
    if _qty(draft.get("quantity")):
        lines.append(f"- **{t['quantity']}:** {_qty(draft['quantity'])}")
    if seller and _qty(draft.get("minimum_order")):
        lines.append(f"- **{t['min_order']}:** {_qty(draft['minimum_order'])}")
    if _price(draft.get("price_target"), t):
        lines.append(f"- **{t['price_seller' if seller else 'price_buyer']}:** {_price(draft['price_target'], t)}")
    if _loc(draft.get("location")):
        lines.append(f"- **{t['location']}:** {_loc(draft['location'])}")
    if _deadline(draft.get("deadline"), t, seller):
        lines.append(f"- **{t['deadline_seller' if seller else 'deadline_buyer']}:** {_deadline(draft['deadline'], t, seller)}")
    if _specs(draft.get("product_details"), t):
        lines.append(f"- **{t['specs']}:** {_specs_text(draft.get('product_details'), t, labels)}")
    if draft.get("description"):
        desc = str(draft["description"]).strip()
        lines.append(f"- **{t['description']}:** {desc[:140]}{'...' if len(desc) > 140 else ''}")
    missing = [
        t["names"][f] for f, d in (
            ("quantity", "quantity"), ("price", "price_target"), ("location", "location"), ("dispatch", "deadline")
        ) if not draft.get(d) or (f == "location" and not (draft.get("location") or {}).get("city"))
    ]
    if missing:
        lines.append(f"- **{t['not_set']}:** {', '.join(missing)}")
    lines.append("")
    lines.append(question if question is not None else t["q_publish"])
    return "\n".join(lines)


def _field_value(key: str, draft: dict[str, Any], t: dict[str, Any], seller: bool, labels: Optional[dict[str, str]] = None) -> str:
    if key == "quantity":
        return _qty(draft.get("quantity"))
    if key == "minimum_order":
        return _qty(draft.get("minimum_order"))
    if key == "price_target":
        return _price(draft.get("price_target"), t)
    if key == "location":
        return _loc(draft.get("location"))
    if key == "deadline":
        return _deadline(draft.get("deadline"), t, seller)
    if key == "product_details":
        return _specs_text(draft.get("product_details"), t, labels)
    return str(draft.get(key) or "")


def build_edit_recap(
    draft: dict[str, Any], snapshot: dict[str, Any], changed: Iterable[str], lang: str = "en", publish: bool = False,
    labels: Optional[dict[str, str]] = None,
) -> str:
    """Only the changed fields as ``old -> new`` and the "save these changes?" question."""
    lang = latin_lang(lang)
    t = _L[lang]
    seller = str(snapshot.get("role") or draft.get("role") or "").lower() == "seller"
    label = {
        "quantity": t["quantity"], "minimum_order": t["min_order"], "price_target": t["price_seller" if seller else "price_buyer"],
        "location": t["location"], "deadline": t["deadline_seller" if seller else "deadline_buyer"], "specs": t["specs"],
        "category": t["category"], "title": t["title"], "description": t["description"],
    }
    lines = [t["edit_intro"]]
    for key in changed:
        if key == "product_details":
            old_d, new_d = snapshot.get(key) or {}, draft.get(key) or {}
            names = [k for k in dict.fromkeys([*old_d, *new_d]) if str(k) not in {"name", "hsn_source"}
                     and not str(k).endswith(draft_assist.MUST_MATCH_SUFFIX)]
            for k in names:
                if old_d.get(k) != new_d.get(k) or old_d.get(f"{k}{draft_assist.MUST_MATCH_SUFFIX}") != new_d.get(f"{k}{draft_assist.MUST_MATCH_SUFFIX}"):
                    name = "HSN" if k == "hsn_code" else spec_label(str(k), labels)
                    new_v = _spec_value(new_d.get(k), t) + (f" ({t['must']})" if new_d.get(f"{k}{draft_assist.MUST_MATCH_SUFFIX}") else "") if k in new_d else "-"
                    old_v = _spec_value(old_d.get(k), t) if k in old_d else "-"
                    lines.append(f"- **{name}:** {old_v} -> {new_v}")
            continue
        old = _field_value(key, snapshot, t, seller, labels) or "-"
        new = _field_value(key, draft, t, seller, labels) or "-"
        lines.append(f"- **{label.get(key, key)}:** {old} -> {new}")
    if len(lines) == 1:
        return t["nothing"]
    lines.append("")
    lines.append(t["q_save_publish" if publish else "q_save"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Confirming a stored pending action (publish a saved draft, close a live listing)
# ---------------------------------------------------------------------------
PUBLISH_CONFIRM_REPLIES = ["Yes, publish it", "No, keep it as a draft"]


CLOSE_CONFIRM_REPLIES = ["Yes, close it", "No, keep it open"]


def build_publish_recap(draft: dict[str, Any], title: str, short_id: str, lang: str = "en") -> str:
    """The saved draft RFQ as the recap the user confirms before it goes live."""
    if latin_lang(lang) == "hinglish":
        head = f"Aap '{title}' (ID {short_id}) publish karne wale hain. Yeh abhi saved draft hai.\n\n"
        question = "Kya main ise ab publish kar dun? Publish hote hi suppliers/buyers ise dekh sakenge."
    else:
        head = f"You are about to publish the saved draft '{title}' (ID {short_id}).\n\n"
        question = "Publish it now? Once published, other marketplace users can see it."
    return head + build_recap(draft, lang, question=question)


def build_close_ask(title: str, short_id: str, lang: str = "en") -> str:
    if latin_lang(lang) == "hinglish":
        return (
            f"Kya aap live listing '{title}' (ID {short_id}) band karna chahte hain? "
            "Band karne par uske matches aur quotes bhi band ho jayenge aur ise wapas nahi laya ja sakta."
        )
    return (
        f"Do you want to close the live listing '{title}' (ID {short_id})? "
        "Its matches and quotes will stop too, and closing cannot be undone."
    )


RECAP_REPLIES = ["Publish now", "Save as draft", "Change something"]


def blocked_listing_reply(product: str, category_description: str, lang: str = "en") -> str:
    """Prohibited goods: no draft is made; say why, and offer to start again with another product."""
    if latin_lang(lang) == "hinglish":
        return (
            f"Main {product} ka listing draft nahi kar sakta: yeh \"{category_description}\" mein aata hai, jo is platform par "
            "prohibited hai. Agar aap kuch aur kharidna ya bechna chahte hain to bataiye."
        )
    return (
        f"I can't draft a listing for {product}: it falls under \"{category_description}\", which is prohibited on this "
        "platform. If you meant something else, or want to buy or sell a different product, tell me and we will start there."
    )

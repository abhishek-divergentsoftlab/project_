"""The chat's conversation policy for RFQ drafting: what to ask next, and the recap the user confirms.

Pure functions (no database, no model). ``ai_chat_service`` keeps the per-conversation memory in
``conversation.state``: ``recap_hash`` / ``recap_awaiting`` (the exact draft the user was shown and the
question that is open), ``declined_fields``, ``asked_fields`` and ``last_asked``.

Everything the user reads from here is built from the draft's own values, so it never invents a number.
English and Hinglish templates share one structure; Devanagari input is answered in Hinglish.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Optional

from services import chat_draft_assist, chat_specs, drafting_reply, spec_catalog

_HASH_KEYS = (
    "role", "category", "title", "product_details", "quantity", "minimum_order", "price_target", "location",
    "deadline", "description",
)
_NOT_SPECS = {"name", "hsn_code", "hsn_source"}
MAIN_ASK_LIMIT = 2  # a plain field is asked at most this many times before it is let go
MAX_SPEC_QUESTIONS = 3  # specs listed in the one combined spec question
MAX_SPEC_LIST = 8  # specs listed when the user asks what they can add


def draft_hash(draft: Optional[dict[str, Any]]) -> str:
    """Hash of the parts of a draft the recap shows: any change to them invalidates a shown recap."""
    d = draft or {}
    payload = {k: d.get(k) for k in _HASH_KEYS if d.get(k) not in (None, "", {}, [])}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


def pick_lang(message: str, sticky: Optional[str] = None) -> str:
    """"en" or "hinglish": the message's own register, or the conversation's for a short reply ("haan")."""
    style = drafting_reply.detect_style(message)
    style = "hinglish" if style in ("hi", "hinglish") else "en"
    if style == "en" and sticky == "hinglish" and len((message or "").split()) <= 3:
        return "hinglish"
    return style


# ---------------------------------------------------------------------------
# The next question
# ---------------------------------------------------------------------------
def _filled(draft: dict[str, Any], field: str) -> bool:
    if field == "role":
        return bool(draft.get("role"))
    if field == "product":
        return bool(chat_draft_assist.product_name(draft))
    if field == "quantity":
        return bool(draft.get("quantity"))
    if field == "min_order":
        return bool(draft.get("minimum_order"))
    if field == "price":
        return bool(draft.get("price_target"))
    if field == "location":
        return bool((draft.get("location") or {}).get("city"))
    if field == "dispatch":
        return bool(draft.get("deadline"))
    return False


def unfilled_specs(draft: dict[str, Any], suggestions: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The suggested specs the draft has no value for yet, must-match presets first (suggestion order otherwise)."""
    details = draft.get("product_details") or {}
    have = {chat_draft_assist._norm_key(k) for k in details}
    skipped = {chat_draft_assist._norm_key(k) for k in (suggestions or {}).get("skipped") or []}
    out: list[dict[str, Any]] = []
    for spec in (suggestions or {}).get("specs") or []:
        if not isinstance(spec, dict) or chat_draft_assist._norm_key(spec.get("key") or "") in skipped:
            continue
        label = str(spec.get("label") or spec.get("key") or "").strip()
        if label and chat_draft_assist._norm_key(label) not in have and chat_draft_assist._norm_key(spec.get("key") or "") not in have:
            out.append(spec)
    return sorted(out, key=lambda sp: 0 if sp.get("must_match") else 1)


def unfilled_spec_labels(draft: dict[str, Any], suggestions: Optional[dict[str, Any]]) -> list[str]:
    return [str(sp.get("label") or sp.get("key")).strip() for sp in unfilled_specs(draft, suggestions)]


def next_question(
    draft: dict[str, Any],
    suggestions: Optional[dict[str, Any]] = None,
    role: Optional[str] = None,
    declined: Iterable[str] = (),
    asked: Optional[dict[str, int]] = None,
) -> str:
    """The ONE field to ask about next, most important first, or ``"recap"`` when nothing is left.

    Order: role, product, quantity, minimum order (sellers only), price, location, dispatch days,
    the suggested specs (ONE combined question, asked once), the HSN offer, then the recap.
    Returns ``"role" | "product" | "quantity" | "min_order" | "price" | "location" | "dispatch" |
    "specs" | "hsn" | "recap"``.
    """
    declined = set(declined)
    asked = asked or {}
    suggestions = suggestions or {}
    role = str(role or draft.get("role") or "").lower() or None

    def skip(field: str) -> bool:
        return field in declined or asked.get(field, 0) >= MAIN_ASK_LIMIT

    if not role:
        return "role"
    if not _filled(draft, "product"):
        return "product"
    order = ["quantity"] + (["min_order"] if role == "seller" else []) + ["price", "location", "dispatch"]
    for field in order:
        if not _filled(draft, field) and not skip(field):
            return field
    if "specs" not in declined and asked.get("specs", 0) < 1 and unfilled_specs(draft, suggestions):
        return "specs"
    hsn = suggestions.get("hsn")
    if (
        isinstance(hsn, dict) and hsn.get("code") and not chat_draft_assist.user_hsn(draft)
        and not suggestions.get("hsn_offered") and "hsn" not in declined and asked.get("hsn", 0) < 1
    ):
        return "hsn"
    return "recap"


_Q = {
    "en": {
        "role_p": "Are you buying or selling {p}?",
        "role": "Are you looking to buy or to sell?",
        "product": "Which product do you want to {verb}?",
        "quantity_buyer": "How much {p} do you need (quantity and unit)?",
        "quantity_seller": "How much {p} do you have available (quantity and unit)?",
        "min_order": "What is the minimum order quantity you will accept?",
        "price_buyer": "What target price per unit do you have in mind, and in which currency?",
        "price_seller": "What price per unit are you offering, and in which currency?",
        "location_buyer": "Which city should it be delivered to?",
        "location_seller": "Which city will it be dispatched from?",
        "dispatch_buyer": "Within how many days do you need it?",
        "dispatch_seller": "Within how many days can you dispatch it?",
        "specs_buyer": "To match you with the right sellers, tell me a few specs (or say skip):",
        "specs_seller": "To match you with the right buyers, tell me a few specs (or say skip):",
        "specs_listing": "Here are the specs you can add for {p}:",
        "specs_listing_end": "Tell me any values you want to set (or say skip).",
        "hsn": "I suggest HSN {code} ({title}). Shall I use it?",
        "buy": "buy", "sell": "sell",
    },
    "hinglish": {
        "role_p": "Aap {p} khareed rahe hain ya bech rahe hain?",
        "role": "Aap kharidna chahte hain ya bechna?",
        "product": "Aap kaunsa product {verb} chahte hain?",
        "quantity_buyer": "Aapko kitna {p} chahiye (quantity aur unit)?",
        "quantity_seller": "Aapke paas kitna {p} hai (quantity aur unit)?",
        "min_order": "Aap kam se kam kitni quantity ka order lenge?",
        "price_buyer": "Aapka target price per unit kya hai, aur kis currency mein?",
        "price_seller": "Aap per unit kya price offer kar rahe hain, aur kis currency mein?",
        "location_buyer": "Delivery kis shehar mein chahiye?",
        "location_seller": "Maal kis shehar se dispatch hoga?",
        "dispatch_buyer": "Aapko kitne din ke andar chahiye?",
        "dispatch_seller": "Aap kitne din mein dispatch kar sakte hain?",
        "specs_buyer": "Sahi sellers se match karne ke liye kuch specs bataiye (ya skip likhiye):",
        "specs_seller": "Sahi buyers se match karne ke liye kuch specs bataiye (ya skip likhiye):",
        "specs_listing": "{p} ke liye aap yeh specs add kar sakte hain:",
        "specs_listing_end": "Jo values set karni hain woh bataiye (ya skip likhiye).",
        "hsn": "Main HSN {code} ({title}) suggest karta hoon. Ise use karun?",
        "buy": "kharidna", "sell": "bechna",
    },
}


def question_text(field: str, draft: dict[str, Any], suggestions: Optional[dict[str, Any]], lang: str) -> str:
    """The question for one ``next_question`` field, in the user's register. ``"recap"`` has no question here."""
    t = _Q[lang if lang in _Q else "en"]
    role = str(draft.get("role") or "").lower()
    side = "seller" if role == "seller" else "buyer"
    p = chat_draft_assist.product_name(draft) or "this"
    if field == "role":
        return t["role_p"].format(p=p) if chat_draft_assist.product_name(draft) else t["role"]
    if field == "product":
        return t["product"].format(verb=t["sell" if side == "seller" else "buy"])
    if field == "quantity":
        return t[f"quantity_{side}"].format(p=p)
    if field == "min_order":
        return t["min_order"]
    if field in ("price", "location", "dispatch"):
        return t[f"{field}_{side}"]
    if field == "specs":
        specs = unfilled_specs(draft, suggestions)[:MAX_SPEC_QUESTIONS]
        return "\n".join([t[f"specs_{side}"], *(spec_line(sp) for sp in specs)])
    if field == "hsn":
        hsn = (suggestions or {}).get("hsn") or {}
        return t["hsn"].format(code=hsn.get("code", ""), title=hsn.get("formatted_title") or hsn.get("description") or "")
    return ""


def spec_line(spec: dict[str, Any], max_options: int = 4) -> str:
    """"• Variety — Fuji, Gala, Granny Smith…" (options only when the suggestion has them)."""
    label = str(spec.get("label") or spec.get("key") or "").strip()
    options = [str(o).strip() for o in (spec.get("options") or []) if str(o).strip()]
    if not options:
        return f"• {label}"
    more = "…" if len(options) > max_options else ""
    return f"• {label} — {', '.join(options[:max_options])}{more}"


def specs_listing(draft: dict[str, Any], suggestions: Optional[dict[str, Any]], lang: str = "en") -> str:
    """Every unfilled suggested spec with its options: the answer to "what specs can I add?". "" when none."""
    t = _Q[lang if lang in _Q else "en"]
    specs = unfilled_specs(draft, suggestions)[:MAX_SPEC_LIST]
    if not specs:
        return ""
    name = chat_draft_assist.product_name(draft) or "this product"
    head = t["specs_listing"].format(p=name.title())
    return "\n".join([head, *(spec_line(sp, max_options=6) for sp in specs), "", t["specs_listing_end"]])


_SPEC_WORD = r"(?:attributes?|spec(?:ification)?s?|spesifications?|spesications?|specifcations?|specs?)"
_ASKS_SPECS_RE = re.compile(
    rf"\b(?:what|which|show|list|tell|suggest|kya|kaun\s*(?:se|si)|konse|konsi)\b[^.?!]{{0,60}}\b{_SPEC_WORD}\b"
    rf"|\b{_SPEC_WORD}\b[^.?!]{{0,40}}\b(?:can|could|should|may|available|options?|suggest\w*|add|include|chahiye|jod)\b",
    re.IGNORECASE,
)


def asks_for_specs(message: str) -> bool:
    """"what attributes or specification i can add": a question about which specs exist, not a value."""
    text = (message or "").strip()
    if not text or re.search(r"\d", text):
        return False
    return bool(_ASKS_SPECS_RE.search(text)) and (text.endswith("?") or bool(re.match(
        r"^\s*(?:what|which|show|list|tell|suggest|kya|kaun|konse|konsi|can|could|should)\b", text, re.IGNORECASE)))


# ---------------------------------------------------------------------------
# Declining a question
# ---------------------------------------------------------------------------
_DECLINE_RE = re.compile(
    r"\b(skip|not\s+needed|not\s+required|no\s+need|don'?t\s+need|dont\s+need|not\s+now|maybe\s+later|"
    r"nahi\s+chahiye|nahin\s+chahiye|nahi\s+chahie|baad\s+mein|baad\s+me|pata\s+nahi|don'?t\s+know|dont\s+know|"
    r"no\s+idea|not\s+sure|chhod\s+do|chod\s+do|rehne\s+do|zaroorat\s+nahi|zarurat\s+nahi)\b",
    re.IGNORECASE,
)
_DECLINE_ALL_RE = re.compile(
    r"\b(skip\s+(?:all|the\s+rest|everything|rest)|that'?s\s+all|nothing\s+else|no\s+more|bas\s+itna|bas\s+yahi|"
    r"baaki\s+skip|sab\s+skip|baaki\s+sab\s+skip|rest\s+skip|bas\s+itna\s+hi|done|ho\s*gaya|hogaya)\b",
    re.IGNORECASE,
)
_BARE_NO_RE = re.compile(r"^\s*(no|nope|nah|nahi|nahin|na)\s*[.!]*\s*$", re.IGNORECASE)


def detect_decline(message: str) -> Optional[str]:
    """``"all"`` (nothing more to add), ``"one"`` (skip the question just asked) or None."""
    text = (message or "").strip()
    if not text:
        return None
    short = len(text.split()) <= 6 or not re.search(r"\d", text)
    if _DECLINE_ALL_RE.search(text) and short:
        return "all"
    if _BARE_NO_RE.match(text) or (_DECLINE_RE.search(text) and short):
        return "one"
    return None


# ---------------------------------------------------------------------------
# Answers to the recap question
# ---------------------------------------------------------------------------
_YES_RE = re.compile(
    r"^\s*(?:(?:yes|yeah|yep|yup|haan|han|ha|ji|okay|ok|sure)\s*[,!.]?\s*)*"
    r"(?:(?:please\s+)?(?:go\s+ahead|do\s+it|proceed|submit(?:\s+it)?|finali[sz]e(?:\s+it)?|looks\s+good|all\s+good|"
    r"publish(?:\s+it)?|post(?:\s+it)?|create(?:\s+it)?(?:\s+rfq)?|confirm(?:ed)?|"
    r"theek\s+hai|thik\s+hai|sahi\s+hai|publish\s+kar\s*(?:do|dijiye|o)|post\s+kar\s*(?:do|dijiye|o)|"
    r"kar\s*(?:do|dijiye|o)|kardo|please)\s*)?"
    r"(?:(?:now|please|ji|abhi)\s*)*[.!]*\s*$",
    re.IGNORECASE,
)
_DRAFT_RE = re.compile(
    r"^\s*(?:(?:yes|haan|ok|okay|please)\s*[,.]?\s*)*"
    r"(?:(?:save\s+(?:it\s+|this\s+)?(?:as\s+)?(?:a\s+)?draft)|(?:as\s+(?:a\s+)?draft)|draft(?:\s+only)?|"
    r"draft\s+(?:mein|me|main)\s+(?:save|rakh|rakho|daal|dal)(?:\s+(?:karo|kar\s+do|do|dijiye))?|"
    r"draft\s+(?:mein|me|main)|(?:save|save\s+it|save\s+kar\s*(?:do|o))|draft\s+bana\s*(?:do|o)?)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"^\s*(?:no|nope|nah|wait|hold\s+on|hold\s+it|not\s+yet|stop|cancel|ruko|ruk\s+jao|ruk|nahi|nahin|abhi\s+nahi|"
    r"ek\s+minute|one\s+sec(?:ond)?|mat\s+karo|don'?t|dont|change\s+(?:something|a\s+few\s+things|some\s+things)|"
    r"make\s+(?:some\s+)?changes|kuch\s+badalna\s+hai|kuch\s+change\s+karna\s+hai)(?:\s+(?:yet|abhi|please|publish|post|karo|kijiye))*\s*[.!]*\s*$",
    re.IGNORECASE,
)


def classify_recap_reply(message: str) -> Optional[str]:
    """``"yes"`` (publish / save), ``"draft"``, ``"no"``, or None when the message is something else.

    Only a bare answer counts; anything that carries more content ("yes, but make it 600 kg") is None,
    so the ordinary draft update handles it and the shown recap is invalidated by the change.
    """
    text = (message or "").strip()
    if not text or len(text.split()) > 7:
        return None
    if _NO_RE.match(text):
        return "no"
    if _DRAFT_RE.match(text):
        return "draft"
    if _YES_RE.match(text):
        return "yes"
    return None


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
            str(k) in _NOT_SPECS or str(k).endswith(chat_draft_assist.MUST_MATCH_SUFFIX)
            or chat_specs.is_core_key(k) or v in (None, "", [], {})
        ):
            continue
        out.append((spec_label(str(k), labels), _spec_value(v, t), bool(details.get(f"{k}{chat_draft_assist.MUST_MATCH_SUFFIX}"))))
    return out


def _specs_text(details: Any, t: dict[str, Any], labels: Optional[dict[str, str]] = None) -> str:
    """"Packaging: Carton · Pack size: 20 kg · Certifications: Organic, FSSAI (must match)"."""
    return " · ".join(
        f"{k}: {v}" + (f" ({t['must']})" if must else "") for k, v, must in _specs(details, t, labels)
    )


def build_recap(
    draft: dict[str, Any], lang: str = "en", role_note: Optional[str] = None, labels: Optional[dict[str, str]] = None
) -> str:
    """The draft as a short bullet list plus the publish-or-draft question. ``role_note`` names the one role the account can post as."""
    t = _L[lang if lang in _L else "en"]
    role = str(draft.get("role") or "").lower()
    seller = role == "seller"
    name = chat_draft_assist.product_name(draft)
    lines = [t["intro"]]
    head = name.title() if name else ""
    extras = [x for x in (draft.get("category"), f"HSN {chat_draft_assist.user_hsn(draft)}" if chat_draft_assist.user_hsn(draft) else None) if x]
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
    lines.append(t["q_publish"])
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
    t = _L[lang if lang in _L else "en"]
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
                     and not str(k).endswith(chat_draft_assist.MUST_MATCH_SUFFIX)]
            for k in names:
                if old_d.get(k) != new_d.get(k) or old_d.get(f"{k}{chat_draft_assist.MUST_MATCH_SUFFIX}") != new_d.get(f"{k}{chat_draft_assist.MUST_MATCH_SUFFIX}"):
                    name = "HSN" if k == "hsn_code" else spec_label(str(k), labels)
                    new_v = _spec_value(new_d.get(k), t) + (f" ({t['must']})" if new_d.get(f"{k}{chat_draft_assist.MUST_MATCH_SUFFIX}") else "") if k in new_d else "-"
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


def recap_question(lang: str = "en") -> str:
    return _L[lang if lang in _L else "en"]["q_publish"]


def ready_to_confirm(draft: dict[str, Any]) -> bool:
    return _filled(draft, "role") and _filled(draft, "product")


# ---------------------------------------------------------------------------
# Quick replies: the chips shown under a question. Each one, sent back as a message, is handled by the flow above
# ("Publish now" confirms, "Change something" is a no, "Buying" answers the role, an option answers its spec).
# ---------------------------------------------------------------------------
SKIP_REPLY = "Skip"
DONE_REPLY = "Done"
OTHER_REPLY = "Other"
MAX_FOCUS_CHIPS = 8
RECAP_REPLIES = ["Publish now", "Save as draft", "Change something"]
ROLE_REPLIES = ["Buying", "Selling"]
MAX_CHIPS = 6


def quick_replies(
    field: Optional[str], draft: dict[str, Any], suggestions: Optional[dict[str, Any]] = None,
    value_ask: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Chips for the question ``field`` just asked (a ``next_question`` field, or ``"spec_value"``); [] when none fit."""
    suggestions = suggestions or {}
    if field == "recap":
        return list(RECAP_REPLIES)
    if field == "role":
        return list(ROLE_REPLIES)
    if field == "spec_value":
        examples = [str(e).strip() for e in ((value_ask or {}).get("examples") or []) if str(e).strip()]
        return [*examples[:MAX_CHIPS], SKIP_REPLY]
    if field == "hsn":
        quals = [q.capitalize() for q in chat_draft_assist.hsn_qualifiers(suggestions.get("hsn"))][:MAX_CHIPS]
        return [*quals, "No"] if quals else ["Yes", "No"]
    if field == "specs":
        return [*spec_label_chips(draft, suggestions), SKIP_REPLY]
    if field == "spec_pick":
        return [*spec_label_chips(draft, suggestions), DONE_REPLY]
    return []


def spec_label_chips(draft: dict[str, Any], suggestions: Optional[dict[str, Any]]) -> list[str]:
    """Labels of the specs still to fill, must-match presets first (the catalogue's order otherwise), at most 6."""
    out = [spec_catalog.display_label(str(sp.get("label") or sp.get("key"))) for sp in unfilled_specs(draft, suggestions)]
    return list(dict.fromkeys(out))[:MAX_CHIPS]


# ---------------------------------------------------------------------------
# Tapping specs: a label chip asks only that spec; its options are the next chips
# ---------------------------------------------------------------------------
_DONE_RE = re.compile(r"^\s*(?:done|ho\s*gaya|hogaya|that'?s\s+it|finished)\s*[.!]*\s*$", re.IGNORECASE)
_OTHER_RE = re.compile(r"^\s*other\s*[.!]*\s*$", re.IGNORECASE)


def is_done(message: str) -> bool:
    return bool(_DONE_RE.match(message or ""))


def is_other(message: str) -> bool:
    return bool(_OTHER_RE.match(message or ""))


def match_spec_label(message: str, specs: Optional[Iterable[dict[str, Any]]]) -> Optional[dict[str, Any]]:
    """The suggested spec a short message names by its label ("Variety", "pack size"); None for anything longer or unlike one."""
    text = spec_catalog._norm(message or "")
    if not text or len(text.split()) > 4 or re.search(r"\d", text):
        return None
    best, score = None, 0.0
    for sp in specs or []:
        if not isinstance(sp, dict):
            continue
        names = [spec_catalog.display_label(str(sp.get("label") or sp.get("key") or "")), str(sp.get("key") or "").replace("_", " ")]
        for n in names:
            n = spec_catalog._norm(n)
            if not n:
                continue
            r = 1.0 if n == text or n == f"the {text}" else (spec_catalog._ratio(n, text) if len(text) >= 4 else 0.0)
            if r > score:
                best, score = sp, r
    return best if score >= 0.85 else None


def spec_ask_text(spec: dict[str, Any], lang: str = "en") -> str:
    label = spec_catalog.display_label(str(spec.get("label") or spec.get("key"))).lower()
    if lang == "hinglish":
        return f"Kaun sa {label}?" if spec.get("options") else f"{label.capitalize()} kitna?"
    if str(spec.get("input_type")) == "numeric":
        return f"What {label}?"
    return f"Which {label}?"


def spec_type_text(spec: dict[str, Any], lang: str = "en") -> str:
    label = spec_catalog.display_label(str(spec.get("label") or spec.get("key"))).lower()
    return f"{label.capitalize()} type kijiye." if lang == "hinglish" else f"Please type the {label}."


def spec_pick_text(lang: str = "en") -> str:
    return "Aur kuch add karna hai? Neeche se chuniye ya 'Ho gaya' likhiye." if lang == "hinglish" else "Anything else? Pick a spec below, or say Done."


def spec_more_text(spec: dict[str, Any], lang: str = "en") -> str:
    label = spec_catalog.display_label(str(spec.get("label") or spec.get("key"))).lower()
    return f"Aur {label} chuniye, ya 'Ho gaya' likhiye." if lang == "hinglish" else f"Choose more {label}, or tap Done."


def focus_chips(spec: dict[str, Any], details: Optional[dict[str, Any]] = None) -> list[str]:
    """Chips for the spec being asked: its options (choice), the options still unchosen + Done (multi), or example values
    with the unit (number); always an Other."""
    options = [str(o).strip() for o in (spec.get("options") or []) if str(o).strip()]
    unit = str(spec.get("unit") or "").strip()
    if spec.get("multi"):
        have = {spec_catalog._norm(x) for x in ((details or {}).get(str(spec.get("key"))) or [])}
        left = [o for o in options if spec_catalog._norm(o) not in have]
        return [*left[:MAX_FOCUS_CHIPS], OTHER_REPLY, DONE_REPLY]
    if str(spec.get("input_type")) == "numeric":
        return [*[f"{o} {unit}".strip() for o in options[:MAX_FOCUS_CHIPS]], OTHER_REPLY]
    return [*options[:MAX_FOCUS_CHIPS], OTHER_REPLY, SKIP_REPLY]

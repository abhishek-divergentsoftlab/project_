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

from services import chat_draft_assist, drafting_reply

_HASH_KEYS = (
    "role", "category", "title", "product_details", "quantity", "minimum_order", "price_target", "location",
    "deadline", "description",
)
_NOT_SPECS = {"name", "hsn_code", "hsn_source"}
MAIN_ASK_LIMIT = 2  # a plain field is asked at most this many times before it is let go
MAX_SPEC_QUESTIONS = 3


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


def unfilled_spec_labels(draft: dict[str, Any], suggestions: Optional[dict[str, Any]]) -> list[str]:
    details = draft.get("product_details") or {}
    have = {chat_draft_assist._norm_key(k) for k in details}
    out: list[str] = []
    for spec in (suggestions or {}).get("specs") or []:
        label = str(spec.get("label") or spec.get("key") or "").strip()
        if label and chat_draft_assist._norm_key(label) not in have and chat_draft_assist._norm_key(spec.get("key") or "") not in have:
            out.append(label)
    return out


def next_question(
    draft: dict[str, Any],
    suggestions: Optional[dict[str, Any]] = None,
    role: Optional[str] = None,
    declined: Iterable[str] = (),
    asked: Optional[dict[str, int]] = None,
) -> str:
    """The ONE field to ask about next, most important first, or ``"recap"`` when nothing is left.

    Order: role, product, quantity, minimum order (sellers only), price, location, dispatch days,
    suggested specs (one per turn, at most three overall), the HSN offer, then the recap.
    Returns ``"role" | "product" | "quantity" | "min_order" | "price" | "location" | "dispatch" |
    "spec:<label>" | "hsn" | "recap"``.
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
    spec_asked = sum(1 for k in asked if k.startswith("spec:"))
    if spec_asked < MAX_SPEC_QUESTIONS:
        for label in unfilled_spec_labels(draft, suggestions):
            key = f"spec:{label}"
            if key not in declined and asked.get(key, 0) < 1:
                return key
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
        "spec": "Do you have a preference for {label}? Say \"skip\" if it does not matter.",
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
        "spec": "Kya {label} ke liye koi preference hai? Zaroori na ho to \"skip\" likhiye.",
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
    if field.startswith("spec:"):
        return t["spec"].format(label=field.split(":", 1)[1])
    if field == "hsn":
        hsn = (suggestions or {}).get("hsn") or {}
        return t["hsn"].format(code=hsn.get("code", ""), title=hsn.get("formatted_title") or hsn.get("description") or "")
    return ""


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
    r"baaki\s+skip|sab\s+skip|baaki\s+sab\s+skip|rest\s+skip|bas\s+itna\s+hi)\b",
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
    r"(?:(?:please\s+)?(?:go\s+ahead|do\s+it|publish(?:\s+it)?|post(?:\s+it)?|create(?:\s+it)?(?:\s+rfq)?|confirm(?:ed)?|"
    r"theek\s+hai|thik\s+hai|sahi\s+hai|publish\s+kar\s*(?:do|dijiye|o)|post\s+kar\s*(?:do|dijiye|o)|"
    r"kar\s*(?:do|dijiye|o)|kardo|please)\s*)?"
    r"(?:ji\s*)?[.!]*\s*$",
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
    r"ek\s+minute|one\s+sec(?:ond)?|mat\s+karo|don'?t|dont)(?:\s+(?:yet|abhi|please|publish|post|karo|kijiye))*\s*[.!]*\s*$",
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


def _price(p: Any, per: str) -> str:
    if not isinstance(p, dict) or p.get("amount") is None:
        return ""
    unit = f" {per} {p['per_unit']}" if p.get("per_unit") else ""
    return f"{p.get('currency') or 'INR'} {_num(p['amount'])}{unit}"


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


def _specs(details: Any, t: dict[str, Any]) -> list[tuple[str, str, bool]]:
    out: list[tuple[str, str, bool]] = []
    details = details if isinstance(details, dict) else {}
    for k, v in details.items():
        if str(k) in _NOT_SPECS or str(k).endswith(chat_draft_assist.MUST_MATCH_SUFFIX):
            continue
        out.append((str(k), str(v), bool(details.get(f"{k}{chat_draft_assist.MUST_MATCH_SUFFIX}"))))
    return out


def _specs_text(details: Any, t: dict[str, Any]) -> str:
    return ", ".join(f"{k} {v}" + (f" ({t['must']})" if must else "") for k, v, must in _specs(details, t))


def build_recap(draft: dict[str, Any], lang: str = "en", role_note: Optional[str] = None) -> str:
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
    if _price(draft.get("price_target"), t["per"]):
        lines.append(f"- **{t['price_seller' if seller else 'price_buyer']}:** {_price(draft['price_target'], t['per'])}")
    if _loc(draft.get("location")):
        lines.append(f"- **{t['location']}:** {_loc(draft['location'])}")
    if _deadline(draft.get("deadline"), t, seller):
        lines.append(f"- **{t['deadline_seller' if seller else 'deadline_buyer']}:** {_deadline(draft['deadline'], t, seller)}")
    if _specs(draft.get("product_details"), t):
        lines.append(f"- **{t['specs']}:** {_specs_text(draft.get('product_details'), t)}")
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


def _field_value(key: str, draft: dict[str, Any], t: dict[str, Any], seller: bool) -> str:
    if key == "quantity":
        return _qty(draft.get("quantity"))
    if key == "minimum_order":
        return _qty(draft.get("minimum_order"))
    if key == "price_target":
        return _price(draft.get("price_target"), t["per"])
    if key == "location":
        return _loc(draft.get("location"))
    if key == "deadline":
        return _deadline(draft.get("deadline"), t, seller)
    if key == "product_details":
        return _specs_text(draft.get("product_details"), t)
    return str(draft.get(key) or "")


def build_edit_recap(
    draft: dict[str, Any], snapshot: dict[str, Any], changed: Iterable[str], lang: str = "en", publish: bool = False
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
                    name = "HSN" if k == "hsn_code" else k
                    new_v = str(new_d.get(k)) + (f" ({t['must']})" if new_d.get(f"{k}{chat_draft_assist.MUST_MATCH_SUFFIX}") else "") if k in new_d else "-"
                    lines.append(f"- **{name}:** {old_d.get(k, '-')} -> {new_v}")
            continue
        old = _field_value(key, snapshot, t, seller) or "-"
        new = _field_value(key, draft, t, seller) or "-"
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

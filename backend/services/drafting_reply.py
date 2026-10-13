"""Deterministic RFQ-drafting reply: acknowledge what was captured, ask the next question.

Used when the model's drafting answer cannot be shown (it failed verification
twice, or the model was unreachable). Built only from the draft and the user's
own values, with no example numbers, so it can never fail the number check.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
_HINGLISH_RE = re.compile(
    r"\b(mujhe|mereko|me|mein|hu|hoon|hun|chahta|chahti|chahiye|chahie|kharid\w*|bech\w*|becn\w*|hai|hain|"
    r"hamare|humare|paas|kitna|kya|kaise|ka|ki|ke|ko|se|sabut|bori|padi|nahi|aur|wala|wali)\b",
    re.IGNORECASE,
)

_FIELD_ORDER = ("quantity", "location", "price_target", "deadline")

_QUESTIONS = {
    "en": {
        "role": "Are you looking to buy or to sell?",
        "product": "Which product are you looking to {verb}?",
        "quantity": "How much quantity do you need?",
        "quantity_sell": "How much quantity do you have?",
        "location": "Which city or region is this for?",
        "price_target": "Do you have a target price in mind?",
        "deadline": "By when do you need it?",
        "deadline_sell": "By when can you supply it?",
        "done": "Your RFQ draft looks complete. Shall I publish it now, or save it as a draft?",
        "ack_buy": "Got it: you want to buy {p}.",
        "ack_sell": "Got it: you want to sell {p}.",
        "ack_loc": " Location: {c}.",
        "buy": "buy", "sell": "sell",
    },
    "hinglish": {
        "role": "Aap kharidna chahte hain ya bechna?",
        "product": "Aap kaunsa product {verb} chahte hain?",
        "quantity": "Aapko kitni quantity chahiye?",
        "quantity_sell": "Aapke paas kitni quantity hai?",
        "location": "Yeh kis shehar ya region ke liye hai?",
        "price_target": "Kya aapke mind me koi target price hai?",
        "deadline": "Aapko yeh kab tak chahiye?",
        "deadline_sell": "Aap yeh kab tak supply kar sakte hain?",
        "done": "Aapka RFQ draft taiyaar hai. Ise abhi publish karun ya draft mein save karun?",
        "ack_buy": "Samajh gaya: aap {p} kharidna chahte hain.",
        "ack_sell": "Samajh gaya: aap {p} bechna chahte hain.",
        "ack_loc": " Location: {c}.",
        "buy": "kharidna", "sell": "bechna",
    },
    "hi": {
        "role": "आप खरीदना चाहते हैं या बेचना?",
        "product": "आप कौन सा प्रोडक्ट {verb} चाहते हैं?",
        "quantity": "आपको कितनी मात्रा चाहिए?",
        "quantity_sell": "आपके पास कितनी मात्रा है?",
        "location": "यह किस शहर या क्षेत्र के लिए है?",
        "price_target": "क्या आपके मन में कोई टारगेट प्राइस है?",
        "deadline": "आपको यह कब तक चाहिए?",
        "deadline_sell": "आप यह कब तक सप्लाई कर सकते हैं?",
        "done": "आपका RFQ ड्राफ्ट तैयार है। क्या मैं इसे अभी पब्लिश करूं या ड्राफ्ट में सेव करूं?",
        "ack_buy": "ठीक है: आप {p} खरीदना चाहते हैं।",
        "ack_sell": "ठीक है: आप {p} बेचना चाहते हैं।",
        "ack_loc": " स्थान: {c}।",
        "buy": "खरीदना", "sell": "बेचना",
    },
}


def detect_style(text: str) -> str:
    """'hi' for Devanagari, 'hinglish' for romanised Hindi, else 'en'."""
    if _DEVANAGARI_RE.search(text or ""):
        return "hi"
    if len(_HINGLISH_RE.findall(text or "")) >= 2:
        return "hinglish"
    return "en"


def _missing_field(draft: dict[str, Any]) -> Optional[str]:
    if not draft.get("quantity"):
        return "quantity"
    if not (draft.get("location") or {}).get("city"):
        return "location"
    if not draft.get("price_target"):
        return "price_target"
    if not draft.get("deadline"):
        return "deadline"
    return None


def build_drafting_reply(
    draft: dict[str, Any], readiness: dict[str, Any], user_message: str = "", closing: Optional[str] = None
) -> str:
    """One line on what was captured, then the next question. No example numbers.

    ``closing`` is the conversation's own next step (the next question, or "" when the caller appends the
    recap); None falls back to this module's field order.
    """
    t = _QUESTIONS[detect_style(user_message)]
    role = str(draft.get("role") or "").lower()
    product = (draft.get("product_details") or {}).get("name") or draft.get("category")
    sell = role == "seller"

    parts: list[str] = []
    if role in ("buyer", "seller") and product:
        ack = t["ack_sell" if sell else "ack_buy"].format(p=product)
        city = (draft.get("location") or {}).get("city")
        if city:
            ack += t["ack_loc"].format(c=city)
        parts.append(ack)

    nxt = readiness.get("next_required_prompt")
    if closing is not None:
        question = closing
    elif nxt == "role":
        question = t["role"]
    elif nxt == "product":
        question = t["product"].format(verb=t["sell" if sell else "buy"])
    else:
        field = _missing_field(draft)
        if field is None:
            question = t["done"]
        elif sell and field in ("quantity", "deadline"):
            question = t[field + "_sell"]
        else:
            question = t[field]
    if question:
        parts.append(question)
    return " ".join(parts)

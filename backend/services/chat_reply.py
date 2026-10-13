"""Deterministic wording for the chat's drafting replies (pure functions, English and Hinglish).

* ``build_ack`` says what a turn really changed in the draft, computed from the draft before and after it.
* ``strip_draft_claims`` removes the model's "RFQ draft saved/updated..." opening, which can be false: a chat draft is
  never saved or posted before the user confirms the recap.
* ``strip_spec_echo`` removes the model's own spec list and "no specs found in listings" remarks when the flow appends
  the spec question itself, so the user never sees two near-identical lists.
* ``hsn_line``, ``spec_value_question`` and ``already_published_reply`` are the flow's own sentences.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from services import chat_conversation as cc
from services import chat_draft_assist, chat_specs

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_BULLET_RE = re.compile(r"^\s*(?:[-*•●▪]|\d+[.)])\s+")

# "RFQ draft saved: ...", "Your RFQ draft has been updated with ...", "I've updated the draft ...", "Draft finalized:".
_CLAIM_RE = re.compile(
    r"^\W*(?:"
    r"(?:added|updated|add\s+kiya|update\s+kiya)\s*:"  # the model copying this module's own acknowledgement from the history
    r"|(?:your\s+|the\s+)?(?:rfq\s+)?(?:draft|rfq|listing)(?:\s+(?:has\s+been|is\s+now|was|is))?\s*[:\-]?\s*(?:been\s+)?"
    r"(?:saved|created|updated|captured|finali[sz]ed|recorded|ready|posted|published|submitted|stored|noted)\b"
    r"|(?:saved|updated|created|captured|recorded|stored|finali[sz]ed)\s+(?:your\s+|the\s+|this\s+)?(?:rfq\s+)?(?:draft|rfq|listing)\b"
    r"|(?:i(?:'ve|\s+have)?|we(?:'ve|\s+have)?)\s+(?:now\s+)?(?:saved|updated|created|captured|recorded|stored|finali[sz]ed)\s+(?:your\s+|the\s+|this\s+)?(?:rfq\s+)?(?:draft|rfq|listing|details|specs?|specifications?)\b"
    r"|(?:rfq\s+)?draft\s+(?:save|update|create)\s+(?:ho\s+gaya|ho\s+gayi|kar\s+diya|kar\s+di)\b"
    r")",
    re.IGNORECASE,
)
# "(No size or firmness specs found in current listings...)": a remark about listings, not about the draft.
_NO_SPECS_REMARK_RE = re.compile(
    r"\bno\b[^.\n]{0,80}\b(?:specs?|specifications?|attributes?|details)\b[^.\n]{0,60}\b(?:found|available|listed|listings?|data)\b"
    r"|\b(?:specs?|specifications?|attributes?)\b[^.\n]{0,40}\bnot\s+(?:found|available|listed)\b"
    r"|\bnot\s+(?:found|available)\s+in\s+(?:the\s+)?(?:current\s+)?listings?\b",
    re.IGNORECASE,
)
_ASKS_SPECS_RE = re.compile(
    r"\bto\s+match\s+you\s+with\b|\btell\s+me\s+(?:a\s+few\s+|any\s+|some\s+)?(?:specs?|specifications?|attributes?)\b"
    r"|\b(?:which|what)\s+(?:specs?|specifications?|attributes?)\b|\b(?:specs?|specifications?|attributes?)\s+(?:you\s+can|to\s+add|you\s+want)\b"
    r"|\b(?:any|optional|more)\s+(?:specs?|specifications?|attributes?)\b|\bwould\s+you\s+like\s+to\s+(?:add|specify)\b"
    r"|\bsahi\s+(?:sellers|buyers)\b",
    re.IGNORECASE,
)


def label_core(label: Any) -> str:
    return re.sub(r"\([^)]*\)", " ", str(label or "")).strip()


# ---------------------------------------------------------------------------
# The model's opening claim and spec echo
# ---------------------------------------------------------------------------
def _is_claim(sentence: str) -> bool:
    return bool(_CLAIM_RE.search(sentence.strip()))


def strip_draft_claims(text: str) -> tuple[str, bool]:
    """Remove "RFQ draft saved/created/updated/captured..." sentences (and the bullet block that follows one that ends
    with a colon). Returns (text, whether anything was removed)."""
    if not text:
        return text, False
    out: list[str] = []
    removed = False
    skipping = False
    for line in text.split("\n"):
        stripped = line.strip()
        if skipping and _BULLET_RE.match(line):
            continue  # the block of values the claim announced
        skipping = False
        sentences = _SENT_SPLIT_RE.split(stripped) if stripped else []
        if sentences and any(_is_claim(s) for s in sentences):
            removed = True
            kept = [s for s in sentences if not _is_claim(s)]
            skipping = stripped.endswith(":")
            if kept:
                out.append(" ".join(kept))
            continue
        out.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return cleaned, removed


def strip_spec_echo(text: str, specs: Optional[Iterable[dict[str, Any]]], *, drop_lists: bool) -> str:
    """Remove the model's "no specs found in listings" remarks; with ``drop_lists`` (the flow appends the spec question)
    also its own spec asks and the lines that name a suggested spec label."""
    if not text:
        return text
    labels = [label_core(s.get("label") or s.get("key")).lower() for s in (specs or []) if isinstance(s, dict)]
    labels = [l for l in labels if len(l) >= 3]
    kept_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            kept_lines.append(line)
            continue
        sentences = _SENT_SPLIT_RE.split(stripped)
        kept: list[str] = []
        for s in sentences:
            low = s.lower()
            if _NO_SPECS_REMARK_RE.search(s):
                continue
            if drop_lists:
                listy = bool(_BULLET_RE.match(s)) or "?" in s or "—" in s or ":" in s or " - " in s
                if _ASKS_SPECS_RE.search(s) and ("?" in s or s.rstrip().endswith(":") or len(s) < 140):
                    continue
                if any(re.search(rf"(?<![\w]){re.escape(l)}(?![\w])", low) for l in labels) and (listy or _BULLET_RE.match(line)):
                    continue
            kept.append(s)
        if kept:
            kept_lines.append(" ".join(kept) if len(kept) != len(sentences) else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()


_INVITE_RE = re.compile(
    r"\b(?:let\s+me\s+know|would\s+you\s+like|do\s+you\s+want|feel\s+free|shall\s+i|want\s+me\s+to|if\s+you(?:'d|\s+would)?\s+like|"
    r"ready\s+to\s+publish|when\s+you\s+are\s+ready|anything\s+else|any\s+other|adjust\s+any|to\s+(?:confirm|proceed|lock)|"
    r"(?:reply|say|type)\s+['\"\u201c]?(?:yes|ok|okay|haan|confirm))\b",
    re.IGNORECASE,
)
MAX_MODEL_SENTENCES = 2
_HSN_APPLIED_RE = re.compile(
    r"\bhsn\b[^.?!\n]{0,60}\b(?:appl(?:y|ies|ied)|confirm\w*|(?:has\s+been|is|was)\s+(?:set|added|selected|used|applied)|selected|used)\b"
    r"|\b(?:appl(?:y|ies|ied)|confirm\w*|set|added|using|used)\b[^.?!\n]{0,40}\bhsn\b",
    re.IGNORECASE,
)


def drop_unapplied_hsn_claims(text: str) -> str:
    """Remove "HSN 080810 applies / confirmed" sentences: the suggestion is only offered, the user has not accepted it."""
    lines = []
    for line in (text or "").split("\n"):
        kept = [s for s in _SENT_SPLIT_RE.split(line) if not (_HSN_APPLIED_RE.search(s) and not s.rstrip().endswith("?"))]
        if kept or not line.strip():
            lines.append(" ".join(kept) if kept else line)
    return "\n".join(lines).strip()


def limit_model_text(text: str) -> str:
    """The model's part of a drafting reply: no value lists (the acknowledgement carries the facts), no invitations
    (the flow asks the one question), at most two sentences."""
    kept: list[str] = []
    for line in (text or "").split("\n"):
        if not line.strip() or _BULLET_RE.match(line):
            continue
        kept.extend(s for s in _SENT_SPLIT_RE.split(line.strip()) if s and not _INVITE_RE.search(s))
    return " ".join(kept[:MAX_MODEL_SENTENCES]).strip()


# ---------------------------------------------------------------------------
# What changed
# ---------------------------------------------------------------------------
def _sig(*parts: Any) -> Optional[tuple[Any, ...]]:
    """A comparable form of a field: numbers as floats, text case-folded; None when the field has no value."""
    if parts[0] in (None, ""):
        return None
    out: list[Any] = []
    for p in parts:
        try:
            out.append(float(p))
        except (TypeError, ValueError):
            out.append(str(p or "").casefold())
    return tuple(out)


def _spec_map(draft: dict[str, Any], t: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    return {k: (v, must) for k, v, must in cc._specs((draft or {}).get("product_details"), t)}


def build_ack(before: Optional[dict[str, Any]], after: Optional[dict[str, Any]], lang: str = "en") -> str:
    """"Added: Apple Variety Fuji, Pack Size 20 kg, Certifications Organic, FSSAI (must match)." from the draft diff.

    Empty when nothing the user can see changed: the assistant then makes no "updated" claim at all.
    """
    t = cc._L[lang if lang in cc._L else "en"]
    before, after = before or {}, after or {}
    seller = str(after.get("role") or before.get("role") or "").lower() == "seller"
    added: list[str] = []
    updated: list[str] = []

    def put(old: Any, new: Any, label: str, shown: str) -> None:
        """Report a field that gained a value or really changed (``old``/``new`` are comparable signatures)."""
        if new and new != old:
            (updated if old else added).append(f"{label} {shown}")

    name_before, name_after = chat_draft_assist.product_name(before), chat_draft_assist.product_name(after)
    put(name_before.casefold(), name_after.casefold(), t["product"], name_after.title())
    qb, qa = before.get("quantity") or {}, after.get("quantity") or {}
    put(_sig(qb.get("value"), qb.get("unit")), _sig(qa.get("value"), qa.get("unit")), t["quantity"], cc._qty(qa))
    if seller:
        mb, ma = before.get("minimum_order") or {}, after.get("minimum_order") or {}
        put(_sig(mb.get("value"), mb.get("unit")), _sig(ma.get("value"), ma.get("unit")), t["min_order"], cc._qty(ma))
    pb, pa = before.get("price_target") or {}, after.get("price_target") or {}
    put(
        _sig(pb.get("amount"), str(pb.get("currency") or "").upper(), chat_specs.normalize_per_unit(pb.get("per_unit"))),
        _sig(pa.get("amount"), str(pa.get("currency") or "").upper(), chat_specs.normalize_per_unit(pa.get("per_unit"))),
        t["price_seller" if seller else "price_buyer"], cc._price(pa, t),
    )
    cb, ca = str((before.get("location") or {}).get("city") or "").casefold(), str((after.get("location") or {}).get("city") or "").casefold()
    put(cb, ca, t["location"], cc._loc(after.get("location")))
    db_, da = before.get("deadline") or {}, after.get("deadline") or {}
    # in_days -> date is the same deadline written another way: only a new deadline or a different date is news
    deadline_changed = bool(da) and (not db_ or (bool(db_.get("date")) and bool(da.get("date")) and db_["date"] != da["date"])
                                      or (not db_.get("date") and not da.get("date") and db_.get("in_days") != da.get("in_days")))
    if deadline_changed:
        (updated if db_ else added).append(f"{t['deadline_seller' if seller else 'deadline_buyer']} {cc._deadline(da, t, seller)}")
    hsn_before, hsn_after = chat_draft_assist.user_hsn(before), chat_draft_assist.user_hsn(after)
    if hsn_after and hsn_after != hsn_before:
        (updated if hsn_before else added).append(f"HSN {hsn_after}")
    old_specs, new_specs = _spec_map(before, t), _spec_map(after, t)
    for label, (value, must) in new_specs.items():
        if label in old_specs and old_specs[label] == (value, must):
            continue
        item = f"{label} {value}" + (f" ({t['must']})" if must else "")
        (updated if label in old_specs else added).append(item)
    if not added and not updated:
        return ""
    add_word, upd_word = ("Add kiya", "Update kiya") if lang == "hinglish" else ("Added", "Updated")
    parts = []
    if added:
        parts.append(f"{add_word}: {', '.join(added)}")
    if updated:
        parts.append(f"{upd_word}: {', '.join(updated)}")
    return "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# The flow's own sentences
# ---------------------------------------------------------------------------
def hsn_line(hsn: dict[str, Any], lang: str = "en") -> str:
    """"Suggested HSN 0803 - Bananas, fresh or dried. Reply 'fresh' or 'dried' (or 'yes') to use it."""
    title = str(hsn.get("formatted_title") or hsn.get("description") or "").strip().rstrip(".")
    head = f"Suggested HSN {hsn.get('code')}" + (f" – {title}" if title else "") + "."
    quals = [q for q in chat_draft_assist.hsn_qualifiers(hsn)][:3]
    if lang == "hinglish":
        if quals:
            opts = " ya ".join(f"'{q}'" for q in quals)
            return f"{head} Use karne ke liye {opts} (ya 'haan') likhiye."
        return f"{head} Use karne ke liye 'haan' likhiye."
    if quals:
        opts = " or ".join(f"'{q}'" for q in quals)
        return f"{head} Reply {opts} (or 'yes') to use it."
    return f"{head} Reply 'yes' to use it."


def spec_value_question(ask: dict[str, Any], lang: str = "en") -> str:
    """"What minimum Brix Sugar Content (°Bx) do you need? e.g. 12, 14" for a numeric spec named without a value."""
    label = str(ask.get("label") or "").strip()
    qualifier = str(ask.get("qualifier") or "").strip()
    examples = [str(e) for e in (ask.get("examples") or []) if str(e).strip()][:2]
    if lang == "hinglish":
        q = f"Aapko {qualifier + ' ' if qualifier else ''}{label} kitna chahiye?"
        return q + (f" jaise {', '.join(examples)}" if examples else "")
    q = f"What {qualifier + ' ' if qualifier else ''}{label} do you need?"
    return q + (f" e.g. {', '.join(examples)}" if examples else "")


def already_published_reply(title: str, rfq_id: str, lang: str = "en") -> str:
    shown = title or "your RFQ"
    if lang == "hinglish":
        return f"Aapka RFQ '{shown}' pehle hi publish ho chuka hai (id {rfq_id}). Kya aap naya RFQ shuru karna chahte hain?"
    return f"Your RFQ '{shown}' is already published (id {rfq_id}). Want to start a new one?"


def join_blocks(*blocks: str) -> str:
    return "\n\n".join(b.strip() for b in blocks if b and b.strip())

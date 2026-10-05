"""Check an AI answer's numbers against the data it was given.

Product rule: every number, count, price and factual claim about the
marketplace must come from a tool result or from the user's own message. This
module is the guard that enforces it after the model has written its answer:

* ``extract_numeric_claims`` finds the numbers in the text (with currency,
  unit, %, and k / lakh / crore scaling), skipping list markers, heading
  numbers, ids, URLs and dates.
* ``verify`` accepts a claim only if the same value occurs in a tool result,
  the user's message or the RFQ draft (equal within 0.5 percent or after
  rounding to the digits shown). With zero read tools called the allowed set is
  just the user's message and draft, so any price or count the model volunteers
  is flagged.
* ``BANNED_PHRASES`` is a small backstop for outside-knowledge wording that can
  carry no number ("typical market rate"). The number check is the main guard.
* ``render_facts_answer`` is the deterministic fallback, built only from tool
  facts and notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Backstop only. Whole-phrase, case-insensitive. Each phrase presents outside
# knowledge or an invented benchmark as if it were platform data.
BANNED_PHRASES: tuple[str, ...] = (
    "typical market",
    "industry average",
    "market rate is",
    "market rates are",
    "generally costs",
    "usually priced",
    "market equilibrium",
    "typically costs",
    "average market price",
    "standard market price",
)
_BANNED_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in BANNED_PHRASES) + r")\b", re.IGNORECASE)

# A statement about what exists on the platform ("there are no seller listings ... on the platform") is a
# marketplace claim even when it has no digits; with no read tool called it has no source.
_EXISTENCE_CLAIM_RE = re.compile(
    r"\b(?:there\s+(?:are|is|were)|no|zero|only|\d+)\b[^.\n]{0,50}\b(?:listings?|sellers?|buyers?|suppliers?|rfqs?)\b"
    r"[^.\n]{0,60}\b(?:on|in|across|within)\b[^.\n]{0,25}\b(?:platform|marketplace)\b",
    re.IGNORECASE,
)

_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_URL_RE = re.compile(r"(?:https?://|/)[^\s)\]>]*(?:rfq|marketplace|api|chat)[^\s)\]>]*", re.IGNORECASE)
_RAW_NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")

_MONTHS = {
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august", "september", "october",
    "november", "december",
}

# A small bare integer is ordinary prose ("2 options") unless a unit or a
# marketplace noun follows it ("5 buyer RFQs", "3 days", "0 sellers").
_COUNTED_WORDS = {
    "listing", "listings", "rfq", "rfqs", "seller", "sellers", "buyer", "buyers", "supplier", "suppliers",
    "user", "users", "company", "companies", "match", "matches", "order", "orders", "offer", "offers",
    "quote", "quotes", "quotation", "quotations", "day", "days", "week", "weeks", "month", "months",
    "hour", "hours", "unit", "units", "pcs", "pc", "piece", "pieces", "kg", "kgs", "g", "tonne", "tonnes",
    "ton", "tons", "mt", "quintal", "quintals", "qtl", "litre", "litres", "liter", "liters", "l", "ml",
    "bottle", "bottles", "case", "cases", "box", "boxes", "carton", "cartons", "bag", "bags", "tin", "tins",
    "pack", "packs", "packet", "packets", "km", "cbm", "dozen", "per", "inr", "usd", "eur", "gbp", "rs",
    "rupees", "dollars", "lakh", "lakhs", "crore", "crores",
}

_SCALES = {
    "k": 1_000, "thousand": 1_000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
    "crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000,
    "mn": 1_000_000, "million": 1_000_000, "bn": 1_000_000_000, "billion": 1_000_000_000,
}

_CURRENCY_SYMBOLS = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP"}
_CURRENCY_CODE = r"(?:INR|USD|EUR|GBP|AED|Rs\.?|rupees?)"

_CLAIM_RE = re.compile(
    r"(?P<cur>[₹$€£]|\b" + _CURRENCY_CODE + r")?\s?"
    r"(?<![\w.])(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<scale>k|thousand|lakhs?|lacs?|crores?|cr|mn|million|bn|billion)(?![A-Za-z]))?"
    r"(?P<pct>\s?%)?"
    r"(?:\s?(?P<cur2>" + _CURRENCY_CODE + r")(?![A-Za-z]))?"
    r"(?:\s{0,2}(?P<unit>[A-Za-z][A-Za-z/]*))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Claim:
    """One numeric claim in an answer."""

    text: str
    value: float  # after k/lakh/crore scaling
    raw: float  # as written
    currency: Optional[str] = None
    unit: Optional[str] = None
    percent: bool = False
    decimals: int = 0
    scaled: bool = False

    def __str__(self) -> str:
        return self.text.strip()


@dataclass
class Verification:
    ok: bool
    unverified: list[Claim] = field(default_factory=list)
    checked: int = 0
    banned: list[str] = field(default_factory=list)
    read_tools_called: int = 0
    ungrounded: bool = False

    def problems(self) -> list[str]:
        out = [f"number not found in any tool result: {c}" for c in self.unverified]
        out += [f"banned outside-knowledge phrase: '{p}'" for p in self.banned]
        if self.ungrounded:
            out.append("you stated what exists on the platform without calling a data tool; call search_listings first")
        return out


def _to_float(text: str) -> float:
    return float(text.replace(",", ""))


def _decimals(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


def _mask(text: str) -> str:
    """Blank out ids and links so digits inside them are never read as claims."""
    text = _URL_RE.sub(" ", text)
    return _UUID_RE.sub(" ", text)


def _is_list_marker(text: str, start: int, end: int) -> bool:
    """'1. item', '2) item' at the start of a line, or a number right after '#'."""
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    if re.fullmatch(r"\s*(?:[-*>•]\s*)*(?:#+\s*)?(?:\*\*)?", prefix) is None:
        return False
    after = text[end:end + 3]
    if prefix.strip().startswith("#") and re.match(r"\s*[.):]?\s", after + " "):
        return True
    return bool(re.match(r"(?:\*\*)?[.)](?:\*\*)?\s", after))


def extract_numeric_claims(text: str) -> list[Claim]:
    """Numbers in ``text`` that assert a quantity (price, count, size, share, year)."""
    if not text:
        return []
    masked = _mask(text)
    claims: list[Claim] = []
    for m in _CLAIM_RE.finditer(masked):
        num_start, num_end = m.start("num"), m.end("num")
        # "1st", "2nd": ordinal suffix glued to the number.
        if re.match(r"(?:st|nd|rd|th)\b", masked[num_end:num_end + 3], re.IGNORECASE) and not m.group("scale"):
            continue
        if _is_list_marker(masked, num_start, num_end):
            continue
        # clock times and ratios like 10:30
        if masked[num_end:num_end + 1] == ":" and re.match(r":\d", masked[num_end:num_end + 2]):
            continue
        if num_start > 0 and masked[num_start - 1] == ":" and num_start > 1 and masked[num_start - 2].isdigit():
            continue

        raw_text = m.group("num")
        raw = _to_float(raw_text)
        scale = (m.group("scale") or "").lower()
        mult = _SCALES.get(scale, 1)
        cur = (m.group("cur") or m.group("cur2") or "").strip()
        currency = _CURRENCY_SYMBOLS.get(cur) or (
            "INR" if cur.lower().startswith(("rs", "rupee")) else (cur.upper() or None)
        )
        unit_word = (m.group("unit") or "").lower().strip("/")
        percent = bool(m.group("pct"))

        prev_word = re.search(r"([A-Za-z]+)\W*$", masked[:num_start])
        if unit_word in _MONTHS or (prev_word and prev_word.group(1).lower() in _MONTHS and not currency):
            continue

        has_unit = unit_word in _COUNTED_WORDS or (scale != "" and unit_word != "")
        plain = not currency and not percent and not scale
        if plain and raw == int(raw) and 0 <= raw <= 10 and "," not in raw_text and not has_unit and "." not in raw_text:
            continue
        # A glued-on word that is not a unit ("2nd", "3D") is not a claim.
        if plain and m.group("unit") and masked[num_end:num_end + 1].isalpha() and not has_unit:
            continue

        end = m.end()
        if m.group("unit") and unit_word not in _COUNTED_WORDS:
            end = m.end("pct") if m.group("pct") else (m.end("scale") if m.group("scale") else num_end)
            end = max(end, m.end("cur2")) if m.group("cur2") else end
        claims.append(
            Claim(
                text=masked[m.start():end].strip(),
                value=raw * mult,
                raw=raw,
                currency=currency,
                unit=unit_word if unit_word in _COUNTED_WORDS else None,
                percent=percent,
                decimals=_decimals(raw_text),
                scaled=mult != 1,
            )
        )
    return claims


# --------------------------------------------------------------------------- allowed values


def _numbers_in_string(text: str) -> set[float]:
    out: set[float] = set()
    masked = _mask(text)
    for m in _RAW_NUMBER_RE.finditer(masked):
        try:
            out.add(_to_float(m.group(0).rstrip(",")))
        except ValueError:
            continue
    # "5k", "2.5 lakh" in a user message also allow the scaled value.
    for c in extract_numeric_claims(text):
        out.add(c.value)
        out.add(c.raw)
    return out


def _walk(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        out |= _numbers_in_string(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _walk(v, out)


def allowed_values(tool_results: Iterable[Any], user_message: str = "", draft: Optional[dict[str, Any]] = None,
                   extra_text: str = "") -> set[float]:
    """Every numeric leaf of the tool results plus numbers in the user's message, draft and extra context."""
    out: set[float] = set()
    for res in tool_results or []:
        _walk(res, out)
    if user_message:
        out |= _numbers_in_string(user_message)
    if extra_text:
        out |= _numbers_in_string(extra_text)
    if draft:
        _walk(draft, out)
    return out


def _matches(claim_value: float, decimals: int, allowed: set[float]) -> bool:
    for a in allowed:
        if a == claim_value:
            return True
        scale = max(abs(a), abs(claim_value))
        if scale and abs(a - claim_value) / scale <= 0.005:
            return True
        if round(a, decimals) == round(claim_value, decimals):
            return True
    return False


def _claim_allowed(claim: Claim, allowed: set[float]) -> bool:
    if _matches(claim.value, claim.decimals if not claim.scaled else 0, allowed):
        return True
    if claim.scaled and _matches(claim.raw, claim.decimals, allowed):
        return True
    if claim.percent and _matches(claim.raw / 100.0, claim.decimals + 2, allowed):
        return True
    return False


# --------------------------------------------------------------------------- verify


def _read_tool_names() -> frozenset[str]:
    from services.market_data_tools import READ_TOOL_NAMES

    return READ_TOOL_NAMES


def verify(
    answer: str,
    tool_results: list[dict[str, Any]],
    user_message: str,
    draft: Optional[dict[str, Any]] = None,
    extra_text: str = "",
) -> Verification:
    """Accept an answer only if every number in it is traceable and no banned phrase appears."""
    read_called = sum(1 for r in tool_results or [] if isinstance(r, dict) and r.get("tool") in _read_tool_names())
    allowed = allowed_values(tool_results, user_message, draft, extra_text)
    claims = extract_numeric_claims(answer)
    unverified = [c for c in claims if not _claim_allowed(c, allowed)]
    banned = sorted({m.group(0).lower() for m in _BANNED_RE.finditer(answer or "")})
    ungrounded = read_called == 0 and bool(_EXISTENCE_CLAIM_RE.search(answer or ""))
    return Verification(
        ok=not unverified and not banned and not ungrounded,
        ungrounded=ungrounded,
        unverified=unverified,
        checked=len(claims),
        banned=banned,
        read_tools_called=read_called,
    )


# --------------------------------------------------------------------------- fallback

_NEXT_STEP = (
    "To get a platform comparison you can post an RFQ for this product, widen the city or region, "
    "or share the listing you want analysed."
)

_NO_DATA_MESSAGE = "I don't have verified platform data to answer that yet."


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".") if value != int(value) else f"{int(value):,}"
    return str(value)


def _describe_args(args: dict[str, Any]) -> str:
    parts = [f"{k}={v}" for k, v in (args or {}).items() if k not in ("limit", "include_mine") and v]
    return ", ".join(parts)


def _is_empty(result: dict[str, Any]) -> bool:
    data = result.get("data") or {}
    return result.get("ok") and (
        data.get("distinct_listings") == 0 or data.get("n_listings") == 0 or data.get("total_matches") == 0
    )


def render_facts_answer(tool_results: list[dict[str, Any]], user_message: str = "") -> str:
    """Deterministic answer from tool facts and notes only (no model involved)."""
    results = [r for r in (tool_results or []) if isinstance(r, dict)]
    if not results:
        return (
            f"{_NO_DATA_MESSAGE}\n\n"
            "It would help to know the product, the city or region, whether you are buying or selling, "
            "and the listing you want analysed."
        )

    blocks: list[str] = []
    any_empty = False
    for res in results:
        name = (res.get("tool") or "lookup").replace("_", " ")
        scope = _describe_args(res.get("args") or {})
        title = f"**{name.capitalize()}**" + (f" ({scope})" if scope else "")
        lines = [title]
        if not res.get("ok"):
            lines.append("Could not look this up: " + "; ".join(res.get("notes") or ["no details"]))
            blocks.append("\n".join(lines))
            continue
        if _is_empty(res):
            any_empty = True
            lines.append("There is no matching data on the platform for this yet, so nothing can be compared.")
        facts = res.get("facts") or []
        if facts:
            lines += ["", "| Fact | Value |", "|---|---|"]
            for f in facts:
                unit = f" {f.get('unit')}" if f.get("unit") else ""
                lines.append(f"| {f.get('label')} | {_fmt(f.get('value'))}{unit} |")
        for note in res.get("notes") or []:
            lines.append(f"- {note}")
        blocks.append("\n".join(lines))

    text = "\n\n".join(blocks)
    if any_empty:
        text += "\n\n" + _NEXT_STEP
    return text

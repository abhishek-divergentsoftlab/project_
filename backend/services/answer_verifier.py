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
* Numeric claims are typed. A price (currency, "per kg") must match a price
  fact of a tool result, a percentage (GST, tax rate) a percent/rate fact, and a
  count of listings or suppliers a count fact. The user's own numbers are typed
  the same way and support a claim only when the types match (the user's "₹50,000"
  backs "your budget of ₹50,000"); an untyped user number supports none of the
  three, and a count or a GST/tax-rate percentage is never supported by user text.
* Trust claims without a number ("GST verified", "KYC verified", "verified
  supplier") are blocked unless a tool result says so.
* ``unsafe_payment_issues`` blocks UPI ids, bank account / IFSC details and
  advice to pay outside escrow, whatever the source (a listing title written
  by a seller included); ``scrub_unsafe`` replaces them with a safe statement.
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

# --------------------------------------------------------------------------- unsafe payment content

# Known UPI handle suffixes. Plain e-mail domains are not listed, so ordinary e-mail addresses are not blocked.
_UPI_SUFFIXES = (
    "okaxis|oksbi|okhdfcbank|okicici|okbizaxis|ybl|ibl|axl|apl|fbl|paytm|upi|sbi|axisbank|icici|hdfcbank|"
    "ptyes|pthdfc|ptsbi|ptaxis|waaxis|wasbi|waicici|wahdfcbank|aubank|yesbank|idfcbank|kotak|rbl|pnb|"
    "barodampay|jupiteraxis|freecharge|ikwik|abfspay|yapl|postbank|cnrb|unionbank|slice|fam"
)
UPI_ID_RE = re.compile(rf"(?<![\w.\-])[\w.\-]{{2,}}@(?:{_UPI_SUFFIXES})\b", re.IGNORECASE)
_UPI_LABEL_RE = re.compile(r"\bupi(?:\s*id)?\s*[:=\-]\s*[\w.\-]{2,}@\w+", re.IGNORECASE)
_BANK_ACCOUNT_RE = re.compile(
    r"\b(?:a/?c|acct|account)\.?\s*(?:no\.?|number|num|#)?\s*[:=\-]?\s*\d[\d\s-]{7,20}\d\b", re.IGNORECASE
)
_IFSC_RE = re.compile(r"(?-i:\b[A-Z]{4}0[A-Z0-9]{6}\b)|\bifsc(?:\s*code)?\s*[:=\-]?\s*[A-Za-z]{4}0\w{6}\b", re.IGNORECASE)
_ESCROW_OFF_RE = re.compile(
    r"\bescrow\b[^.\n]{0,25}\b(?:offline|down|disabled|unavailable|suspended|paused|not\s+(?:working|available|active|needed|required|used))\b",
    re.IGNORECASE,
)
_ADVANCE_OFFPLATFORM_RE = re.compile(
    r"\b\d{1,3}\s?%\s+advance\b[^.\n]{0,40}\b(?:upi|neft|imps|rtgs|bank|wire|cash)\b"
    r"|\b(?:upi|neft|imps|rtgs|bank\s+transfer|wire\s+transfer)\b[^.\n]{0,40}\badvance\b"
    r"|\badvance\b[^.\n]{0,40}\b(?:by|via|through|using|on)\s+(?:upi|neft|imps|rtgs|bank\s+transfer|wire\s+transfer|cash)\b",
    re.IGNORECASE,
)
_PAY_OUTSIDE_RE = re.compile(
    r"\bpay(?:ing|ment)?\b[^.\n]{0,30}\b(?:directly|outside|off[\s-]?platform|off\s+the\s+platform|bypass)\b"
    r"|\bpay\b[^.\n]{0,40}\bto\s+(?:the\s+)?(?:seller'?s?|their|my)\s+(?:bank|account|upi)\b",
    re.IGNORECASE,
)
# A warning ("never pay directly") is not advice to do it. Only the soft "pay directly / outside" patterns are exempt.
_NEGATION_RE = re.compile(r"\b(?:never|don'?t|do\s+not|avoid|shouldn'?t|should\s+not|not|without)\b", re.IGNORECASE)

SAFE_PAYMENT_STATEMENT = (
    "Payments on this platform go through the platform's escrow. Do not pay anyone outside it, and do not share "
    "bank or UPI details in chat."
)

_HARD_UNSAFE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("UPI id", UPI_ID_RE),
    ("UPI id", _UPI_LABEL_RE),
    ("bank account number", _BANK_ACCOUNT_RE),
    ("IFSC code", _IFSC_RE),
    ("escrow described as unavailable", _ESCROW_OFF_RE),
    ("advance payment outside escrow", _ADVANCE_OFFPLATFORM_RE),
)


def _unsafe_in(segment: str, include_advance: bool = True) -> list[str]:
    hits = [
        label for label, rx in _HARD_UNSAFE
        if (include_advance or rx is not _ADVANCE_OFFPLATFORM_RE) and rx.search(segment)
    ]
    soft = _PAY_OUTSIDE_RE.search(segment)
    if soft and not _NEGATION_RE.search(segment[:soft.end()]):  # only a warning BEFORE the phrase ("never pay ...")
        hits.append("advice to pay outside escrow")
    return hits


def unsafe_payment_issues(text: str, include_advance: bool = True) -> list[str]:
    """Kinds of payment content in ``text`` that an assistant reply must never carry (empty when clean).

    ``include_advance=False`` leaves out "advance payment by bank transfer", which is an ordinary payment term in a
    seller's own listing even though the assistant must not recommend it."""
    out: list[str] = []
    for line in (text or "").splitlines():
        for hit in _unsafe_in(line, include_advance):
            if hit not in out:
                out.append(hit)
    return out


def scrub_unsafe(text: str) -> str:
    """Replace every sentence that carries unsafe payment content with one safe escrow statement."""
    if not text or not unsafe_payment_issues(text):
        return text
    out_lines: list[str] = []
    said = False
    for line in text.splitlines():
        if not _unsafe_in(line):
            out_lines.append(line)
            continue
        kept: list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if _unsafe_in(sentence):
                if not said:
                    kept.append(SAFE_PAYMENT_STATEMENT)
                    said = True
            else:
                kept.append(sentence)
        out_lines.append(" ".join(kept))
    return "\n".join(out_lines)


# --------------------------------------------------------------------------- trust claims

_TRUST_CLAIM_RE = re.compile(
    r"\b(?P<what>gst|kyc|pan|aadhaar|aadhar|iec)\s*(?:status\s*)?(?:is\s+|are\s+|has\s+been\s+|:\s*|-\s*)?"
    r"(?:verified|validated|authenticated|approved|completed|confirmed)\b"
    r"|\b(?:seller|supplier|buyer|company|business|manufacturer|exporter|account|profile|identity)\s+"
    r"(?:is\s+|has\s+been\s+|are\s+)?(?:fully\s+)?(?:verified|kyc[\s-]verified|gst[\s-]verified)\b"
    r"|\bverified\s+(?:seller|supplier|buyer|company|business|manufacturer|exporter)\b",
    re.IGNORECASE,
)
_NEGATED_BEFORE_RE = re.compile(
    r"(?:\bnot|n't|\bnever|\bno|\bun|\bwithout|\byet\s+to\s+be|\bif|\bwhether|\bonce|\bbecome|\bget|\bgetting|\bunless)\W*(?:\w+\W+){0,2}$",
    re.IGNORECASE,
)


_CONTEXT_GST_RE = re.compile(r"gst[_ ]verified\"?\s*[:=]\s*(?:true|yes)\b", re.IGNORECASE)
_CONTEXT_KYC_RE = re.compile(r"kyc[_ ](?:status|verified)\"?\s*[:=]\s*\"?(?:verified|true|yes)\b", re.IGNORECASE)


def _trust_facts(tool_results: Iterable[Any], extra_text: str = "") -> tuple[bool, bool]:
    """(some profile is KYC verified, some profile has GST verified/on file).

    Sources: ``company_profile`` tool results, and the platform's own structured context (candidate cards carry
    ``"gst_verified": true``)."""
    kyc = gst = False
    for res in tool_results or []:
        if not isinstance(res, dict) or res.get("tool") != "company_profile" or not res.get("ok"):
            continue
        data = res.get("data") or {}
        if str(data.get("kyc_status") or "").lower() == "verified":
            kyc = True
            gst = gst or bool(data.get("gst_on_file"))
    if extra_text:
        gst = gst or bool(_CONTEXT_GST_RE.search(extra_text))
        kyc = kyc or bool(_CONTEXT_KYC_RE.search(extra_text))
    return kyc, gst


def unsupported_trust_claims(answer: str, tool_results: Iterable[Any], extra_text: str = "") -> list[str]:
    """Trust claims ("GST verified", "verified supplier") that no tool result or platform context backs."""
    kyc_ok, gst_ok = _trust_facts(tool_results, extra_text)
    out: list[str] = []
    for m in _TRUST_CLAIM_RE.finditer(answer or ""):
        before = (answer or "")[max(0, m.start() - 24):m.start()].split("\n")[-1]
        if _NEGATED_BEFORE_RE.search(before) or (answer or "")[m.end():m.end() + 1] == "?":
            continue
        text = m.group(0).strip().lower()
        what = (m.group("what") or "").lower()
        if what == "gst" or "gst" in text:
            ok = gst_ok
        elif what in ("kyc", "pan", "aadhaar", "aadhar", "iec") or "kyc" in text:
            ok = kyc_ok
        else:  # "verified supplier": either kind of verification backs it
            ok = kyc_ok or gst_ok
        if not ok and text not in out:
            out.append(text)
    return out


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
# The newer codes are upper-case only: "try 3 samples" is not a price in TRY.
_CURRENCY_CODE = r"(?:INR|USD|EUR|GBP|AED|(?-i:TRY|JPY|CNY|AUD|CAD|SGD|CHF|SAR)|Rs\.?|rupees?)"

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
    pos: int = -1  # offset of the number in the masked text (for the words around it)

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
    # Payment content that must never be shown (UPI id, bank details, pay outside escrow) and trust claims no tool backs.
    unsafe: list[str] = field(default_factory=list)
    unsupported_trust: list[str] = field(default_factory=list)
    # Statements about platform fees, commission, tiers or other policy that no platform fact backs.
    unsupported_policy: list[str] = field(default_factory=list)

    def problems(self) -> list[str]:
        out = [f"number not found in a tool result of the right type (price, percent or count): {c}" for c in self.unverified]
        out += [f"banned outside-knowledge phrase: '{p}'" for p in self.banned]
        out += [f"never include {u}; say payments go through the platform's escrow" for u in self.unsafe]
        out += [f"'{t}' is not stated by any tool result; remove it or call company_profile" for t in self.unsupported_trust]
        out += [
            f"'{p}' is a claim about platform fees or policy that no platform fact states; say you do not have that "
            "information instead" for p in self.unsupported_policy
        ]
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
                pos=num_start,
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


# --------------------------------------------------------------------------- policy claims

# Words that make a sentence a statement about what the PLATFORM charges or offers as a plan. The platform defines none
# of these (see ``platform_facts``), so a positive sentence about them is invented unless it says there is no information.
_POLICY_WORD_RE = re.compile(
    r"\b(?:fees?|commissions?|charges|charged|surcharges?|brokerage|memberships?|tiers?|subscriptions?|premium\s+members?|"
    r"pricing\s+plans?|rate\s+card|waived|waive|reduced\s+rates?|loyalty|cashback|rebates?)\b",
    re.IGNORECASE,
)
_NO_INFO_RE = re.compile(
    r"\b(?:(?:do(?:es)?\s*n[o']?t|do\s+not|don'?t|cannot|can'?t|unable\s+to|no|not|nahi|nahin)\s+(?:\w+\s+){0,3}"
    r"(?:information|info|jaankari|data|details|know|aware|sure|define[ds]?|state[ds]?|list(?:ed)?|quote|confirm|have|available)"
    r"|(?:information|info|details|jaankari)\s+(?:is\s+|are\s+|hai\s+)?(?:not|unavailable|nahi|nahin)"
    r"|\b(?:koi|any)\s+(?:\w+\s+){0,2}(?:jaankari|information)\s+(?:\w+\s+){0,2}(?:nahi|nahin|not)"
    r"|no\s+(?:such\s+)?(?:fee|fees|commission)s?\s+(?:is|are)\s+(?:defined|listed|stated))\b",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"(?<=[.!?\n])\s+|\n+")


def unsupported_policy_claims(answer: str, user_message: str = "") -> list[str]:
    """Sentences that state a platform fee, commission, tier, membership or similar, which the platform does not define.

    A sentence that says the assistant has no such information passes; so does one that only repeats a word the user
    wrote in a question ("what is the shipping charge?") without asserting anything about the platform."""
    found: list[str] = []
    for sent in _SENTENCE_RE.split(answer or ""):
        text = sent.strip(" *-•#")
        if not text or not _POLICY_WORD_RE.search(text):
            continue
        if _NO_INFO_RE.search(text) or text.rstrip().endswith("?"):
            continue
        # a fee quoted from a tool fact (freight, customs) is a market fact, not platform policy
        if re.search(r"\b(?:freight|shipping|customs|duty|tax|gst|logistics|courier|transport)\b", text, re.IGNORECASE):
            continue
        found.append(text[:120])
    return found


# --------------------------------------------------------------------------- typed claims

_PRICE_UNIT_WORDS = {"per", "inr", "usd", "eur", "gbp", "rs", "rupees", "dollars"}
_COUNT_NOUNS = {
    "listing", "listings", "rfq", "rfqs", "seller", "sellers", "buyer", "buyers", "supplier", "suppliers",
    "user", "users", "company", "companies", "match", "matches",
}
_CURRENCY_UNIT_RE = re.compile(r"inr|usd|eur|gbp|aed|₹|\$|€|£|/")
_PERCENT_LABEL_RE = re.compile(r"percent(?!ile)|pct|%|\bgst\b|\btax\b|\brate\b|\bshare\b", re.IGNORECASE)
_COUNT_LABEL_RE = re.compile(
    r"listings?|users?|counterpart|matches|\bcount\b|returned|removed|reviews?|suppliers?|sellers?|buyers?|compan",
    re.IGNORECASE,
)
_KEY_OTHER_RE = re.compile(r"rating|quantity|distance|latitude|longitude", re.IGNORECASE)
_KEY_PERCENT_RE = re.compile(r"pct|percent|score", re.IGNORECASE)
_KEY_PRICE_RE = re.compile(r"price|^(?:median|mean|average|avg|p25|p75|min|max|amount|cost)$", re.IGNORECASE)
_KEY_COUNT_RE = re.compile(r"^n$|^n_|count|distinct|^total_|duplicates|removed|by_role", re.IGNORECASE)


def claim_type(claim: Claim) -> Optional[str]:
    """'percent', 'price' or 'count' when the claim has that shape, else None (a plain quantity, year, size)."""
    if claim.percent:
        return "percent"
    if claim.currency or (claim.unit or "") in _PRICE_UNIT_WORDS:
        return "price"
    if (claim.unit or "") in _COUNT_NOUNS:
        return "count"
    return None


_TAX_WORDS_RE = re.compile(r"\b(?:gst|igst|cgst|sgst|vat|tax|taxes|duty|duties|tds|tariff|rate)\b", re.IGNORECASE)


def _is_tax_claim(masked_answer: str, claim: Claim) -> bool:
    """A percentage stated as a tax / GST rate (words like GST, tax, duty, rate around the number)."""
    if claim.pos < 0:
        return False
    around = masked_answer[max(0, claim.pos - 40):claim.pos + len(claim.text) + 40]
    return bool(_TAX_WORDS_RE.search(around))


def _user_typed_values(user_text: str) -> dict[str, set[float]]:
    """The user's numbers by the type they were written with: next to a currency or "per" a price, next to % a percent.
    Bare numbers and quantities ("12 bags", "18") are untyped and appear in neither set."""
    out: dict[str, set[float]] = {"price": set(), "percent": set()}
    for c in extract_numeric_claims(user_text or ""):
        kind = claim_type(c)
        if kind in out:
            out[kind].update({c.value, c.raw})
    return out


def _fact_type(label: str, unit: str) -> Optional[str]:
    unit = unit or ""
    if _CURRENCY_UNIT_RE.search(unit.lower()):
        return "price"
    if "%" in unit or _PERCENT_LABEL_RE.search(label or ""):
        return "percent"
    if "price" in (label or "").lower():
        return "price"
    if not unit and _COUNT_LABEL_RE.search(label or ""):
        return "count"
    return None


def _key_type(path: tuple[str, ...]) -> Optional[str]:
    for key in reversed(path):
        if _KEY_OTHER_RE.search(key):
            return None
        for name, rx in (("percent", _KEY_PERCENT_RE), ("price", _KEY_PRICE_RE), ("count", _KEY_COUNT_RE)):
            if rx.search(key):
                return name
    return None


def _walk_typed(obj: Any, path: tuple[str, ...], out: dict[str, set[float]]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        kind = _key_type(path)
        if kind:
            out[kind].add(float(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk_typed(v, path + (str(k),), out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_typed(v, path, out)


def typed_values(tool_results: Iterable[Any]) -> dict[str, set[float]]:
    """Numbers the tool results state as a price, a percent/rate or a count (facts by label+unit, data by key)."""
    out: dict[str, set[float]] = {"price": set(), "percent": set(), "count": set()}
    for res in tool_results or []:
        if not isinstance(res, dict):
            continue
        for fact in res.get("facts") or []:
            if not isinstance(fact, dict) or isinstance(fact.get("value"), bool):
                continue
            value = fact.get("value")
            if isinstance(value, (int, float)):
                kind = _fact_type(str(fact.get("label") or ""), str(fact.get("unit") or ""))
                if kind:
                    out[kind].add(float(value))
        _walk_typed(res.get("data"), (), out)
    return out


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
    # Platform-supplied context (the draft, candidate cards, suggestions) is evidence for any type; the user's own
    # words are not evidence for a price, a percentage or a count.
    context_allowed = allowed_values([], "", draft, extra_text)
    typed = typed_values(tool_results)
    user_typed = _user_typed_values(user_message)
    claims = extract_numeric_claims(answer)
    masked_answer = _mask(answer or "")
    unverified: list[Claim] = []
    for c in claims:
        kind = claim_type(c)
        if kind is None:
            pool = allowed
        else:
            pool = typed[kind] | context_allowed
            # The user's own number supports an echo of the same type: their price, their percentage.
            # Never a count, and never a GST / tax rate (only a tool fact says what a tax rate is).
            if kind == "price" or (kind == "percent" and not _is_tax_claim(masked_answer, c)):
                pool = pool | user_typed[kind]
        if not _claim_allowed(c, pool):
            unverified.append(c)
    banned = sorted({m.group(0).lower() for m in _BANNED_RE.finditer(answer or "")})
    ungrounded = read_called == 0 and bool(_EXISTENCE_CLAIM_RE.search(answer or ""))
    unsafe = unsafe_payment_issues(answer)
    trust = unsupported_trust_claims(answer, tool_results, extra_text)
    policy = unsupported_policy_claims(answer, user_message)
    return Verification(
        ok=not unverified and not banned and not ungrounded and not unsafe and not trust and not policy,
        unsupported_policy=policy,
        ungrounded=ungrounded,
        unverified=unverified,
        checked=len(claims),
        banned=banned,
        read_tools_called=read_called,
        unsafe=unsafe,
        unsupported_trust=trust,
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
    # Facts can carry text a seller wrote (a listing title): never print payment details from it.
    return scrub_unsafe(text)

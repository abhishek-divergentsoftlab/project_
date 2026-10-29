"""One normaliser for the ways trade people write grade codes and sizes.

"SS 304", "ss-304", "SS304", "AISI 304", "304 stainless" are one grade (``ss304``); "MS" and "mild steel" are one
material; "EN 8" is ``en8``; "Fe 500D" is ``fe500``. Sizes: "1/2 inch", '1/2"', "0.5in" and "15 NB" are all half an inch.
Used on BOTH sides of every comparison (token overlap, critical spec tokens, attribute values, marketplace search), so
a spelling difference can never be scored as a different product.

Pure functions with no imports from the rest of the services (``unit_converter`` and ``spec_catalog`` import this).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Optional

_I = re.IGNORECASE

# ---------------------------------------------------------------------------
# Grade / material codes
# ---------------------------------------------------------------------------
_STAINLESS_SERIES = r"[234]\d{2}"
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # SS 304 / ss-304 / SUS304 / AISI 304 / AISI-316L / ASI 304 / SS 304 L
    (re.compile(rf"\b(?:ss|sus|aisi|asi)[\s\-_/.]*({_STAINLESS_SERIES})[\s\-_]*([lh]?)\b", _I), r"ss\1\2"),
    # "stainless steel 304" / "stainless 316L" -> keep the words, add the code
    (re.compile(rf"\bstainless(?:\s+steel)?[\s\-_]+(?:grade\s+|type\s+)?({_STAINLESS_SERIES})[\s\-_]*([lh]?)\b", _I), r"stainless steel ss\1\2"),
    # "304 stainless steel" / "316L stainless"
    (re.compile(rf"\b({_STAINLESS_SERIES})[\s\-_]*([lh]?)[\s\-_]*(?:grade\s+)?stainless(?:\s+steel)?\b", _I), r"ss\1\2 stainless steel"),
    # 304L / 316L / 304H / 304 L are unambiguous stainless grades even without the SS prefix
    (re.compile(rf"(?<![\w.])({_STAINLESS_SERIES})[\s\-_]+([lh])\b", _I), r"ss\1\2"),
    (re.compile(r"(?<![\w.])([234]\d{2}[lh])\b", _I), r"ss\1"),
    # MS / mild steel
    (re.compile(r"\b(?:mild[\s\-]+steel|ms)\b", _I), "mild steel"),
    # EN8, EN-24, EN 31
    (re.compile(r"\ben[\s\-_]*(\d{1,2})\b", _I), r"en\1"),
    # Fe500, Fe 500D, Fe-550 (the D / S suffix is a ductility class of the same strength grade)
    (re.compile(r"\bfe[\s\-_]*(\d{3})[\s\-_]*[a-ds]?\b", _I), r"fe\1"),
    # Cat 6 / cat-6a
    (re.compile(r"\bcat[\s\-_]*(\d{1,2})[\s\-_]*([ae]?)\b", _I), r"cat\1\2"),
    # USB Type-C / Type C / USB-C
    (re.compile(r"\b(?:usb[\s\-_]*)?type[\s\-_]*c\b", _I), "type-c"),
    (re.compile(r"\busb[\s\-_]*c\b", _I), "type-c"),
    # Plywood / cardboard ply (e.g. 5 ply / 5-ply / 7ply)
    (re.compile(r"\b(\d+)[\s\-_]*ply\b", _I), r"\1-ply"),
)


def canonical_codes(text: Optional[str]) -> str:
    """``text`` lowercased with every known grade code collapsed to its canonical spelling."""
    if not text:
        return ""
    out = str(text).lower()
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    return out


_BARE_STAINLESS_RE = re.compile(rf"^{_STAINLESS_SERIES}[lh]?$")
_VALUE_PREFIX_RE = re.compile(r"^(?:grade|type|gr\.?|aisi)\s+", _I)


def canonical_value(text: Optional[str]) -> str:
    """A whole attribute value in canonical form: "Grade 304" and "SS304" are both ``ss304``; "MS" is "mild steel"."""
    value = canonical_codes(re.sub(r"\s+", " ", str(text or "")).strip())
    value = _VALUE_PREFIX_RE.sub("", value).strip()
    value = re.sub(r"^(?:stainless steel )?(ss\d{3}[lh]?)(?: stainless steel)?$", r"\1", value)  # "304 stainless"
    if _BARE_STAINLESS_RE.match(value):
        value = "ss" + value
    return value


def codes_equal(a: object, b: object) -> bool:
    """True when two attribute strings are the same grade / material code once normalised."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    ca, cb = canonical_value(a), canonical_value(b)
    return bool(ca) and ca == cb


def code_tokens(text: Optional[str]) -> set[str]:
    """Canonical letter-digit codes found in ``text`` (``ss304``, ``en8``, ``fe500``, ``316l``)."""
    return set(re.findall(r"\b(?:ss|en|fe|cat)\d+[a-z]?\b", canonical_codes(text)))


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------
# Nominal bore (NB / DN, mm) -> nominal pipe size in inches.
NB_TO_INCH: dict[int, str] = {
    6: "0.125", 8: "0.25", 10: "0.375", 15: "0.5", 20: "0.75", 25: "1", 32: "1.25", 40: "1.5", 50: "2",
    65: "2.5", 80: "3", 100: "4", 125: "5", 150: "6", 200: "8", 250: "10", 300: "12",
}

_QUOTE = r"[\"”″]"
_FRAC_INCH_RE = re.compile(
    rf"(?<![\d/.])(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)\s*(?:{_QUOTE}|''|inch(?:es)?\b|in\b)", _I
)
_FRAC_WHOLE_RE = re.compile(r"^\s*(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)\s*$")
_DEC_INCH_QUOTE_RE = re.compile(rf"(?<![\d/.])(\d+(?:\.\d+)?)\s*(?:{_QUOTE}|'')")
_NB_AFTER_RE = re.compile(r"\b(\d{1,3})\s*(?:nb|dn|n\.b\.?|mm\s*nb)\b", _I)
_NB_BEFORE_RE = re.compile(r"\b(?:dn|nb)\s*[-:]?\s*(\d{1,3})\b", _I)


def _fmt(d: Decimal) -> str:
    s = format(d.quantize(Decimal("0.0001")), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _fraction(whole: Optional[str], num: str, den: str) -> Optional[str]:
    n, d = int(num), int(den)
    if d == 0 or d > 64:
        return None
    return _fmt(Decimal(int(whole or 0)) + Decimal(n) / Decimal(d))


def normalize_size_text(text: Any) -> str:
    """Rewrite fractions, inch marks and NB/DN sizes as "<decimal> in": '1 1/2"' -> "1.5 in", "15 NB" -> "0.5 in"."""
    s = str(text)

    def frac_inch(m: re.Match[str]) -> str:
        dec = _fraction(m.group(1), m.group(2), m.group(3))
        return f"{dec} in" if dec else m.group(0)

    s = _FRAC_INCH_RE.sub(frac_inch, s)
    whole = _FRAC_WHOLE_RE.match(s)
    if whole and int(whole.group(2)) < int(whole.group(3) or 1):
        dec = _fraction(whole.group(1), whole.group(2), whole.group(3))
        if dec:
            s = dec
    s = _DEC_INCH_QUOTE_RE.sub(r"\1 in", s)

    def nb(m: re.Match[str]) -> str:
        inch = NB_TO_INCH.get(int(m.group(1)))
        return f"{inch} in" if inch else m.group(0)

    s = _NB_AFTER_RE.sub(nb, s)
    return _NB_BEFORE_RE.sub(nb, s)


# ---------------------------------------------------------------------------
# Search: sizes as POSIX-regex alternatives (marketplace search over listing text)
# ---------------------------------------------------------------------------
_INCH_WORD_RE = re.compile(r"(?<![\d/.])(\d+(?:\.\d+)?)\s*inch(?:es)?\b", _I)
_INCH_MARK = r"""\s*(?:["”″]|''|inch(?:es)?\M|in\M)"""


def _fmt1(d: Decimal) -> str:
    s = format(d.quantize(Decimal("0.1")), "f")
    return s[:-2] if s.endswith(".0") else s


def size_alternatives(inches: Decimal) -> list[str]:
    """Regexes for every common spelling of one pipe/fitting size ("1/2", "0.5 in", "12.7mm", "15 NB")."""
    from fractions import Fraction

    alts: list[str] = []
    frac = Fraction(inches).limit_denominator(64)
    whole, rem = divmod(frac.numerator, frac.denominator)
    if rem:
        part = f"{rem}/{frac.denominator}"
        alts.append(rf"\m{whole}\s+{part}{_INCH_MARK}" if whole else rf"\m{part}{_INCH_MARK}")
    dec = _fmt(inches)
    if "." in dec:
        lead, tail = dec.split(".")
        alts.append(rf"\m{lead}?\.{tail}{_INCH_MARK}" if lead == "0" else rf"\m{lead}\.{tail}{_INCH_MARK}")
    else:
        alts.append(rf"\m{dec}{_INCH_MARK}")
    alts.append(rf"\m{re.escape(_fmt1(inches * Decimal('25.4')))}\s*mm\M")
    for nb, inch in NB_TO_INCH.items():
        if Decimal(inch) == inches:
            alts += [rf"\m{nb}\s*(?:nb|dn)\M", rf"\m(?:nb|dn)[\s\-:]*{nb}\M", rf"\m{nb}\s*mm\s*nb"]
    return alts


def extract_sizes(text: str) -> tuple[str, list[list[str]]]:
    """(``text`` without its pipe sizes, one list of regex alternatives per size found)."""
    found: list[Decimal] = []

    def grab(m: re.Match[str]) -> str:
        try:
            found.append(Decimal(m.group(1)))
        except Exception:  # noqa: BLE001
            return m.group(0)
        return " "

    normalised = normalize_size_text(text)
    if normalised == text and not _INCH_WORD_RE.search(text):
        return text, []
    # normalize_size_text writes "<decimal> in"; take those and any "<n> inch" back out as sizes.
    rest = re.sub(r"(?<![\d/.])(\d+(?:\.\d+)?)\s*in\b", grab, normalised, flags=_I)
    rest = _INCH_WORD_RE.sub(grab, rest)
    return re.sub(r"\s+", " ", rest).strip(), [size_alternatives(d) for d in dict.fromkeys(found)]

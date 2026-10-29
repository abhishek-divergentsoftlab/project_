"""Grounding and argument cleaning for the model's draft updates. Pure functions, no I/O.

The model decides what the user means; this module only checks that what it writes into the draft is something the
user actually said. Every provided value carries ``said``: the user's own words it came from. A value whose ``said``
is not in the conversation is rejected (per field, the grounded ones still apply) and the model is told to ask.
``None`` / ``""`` / missing always mean "unchanged"; only ``remove`` clears.

Text comparison is mechanical (case, whitespace, punctuation, digit script and thousands separators are ignored); there
is no regex that reads the user's intent.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from services import spec_catalog, unit_converter

MUST_SUFFIX = "__must_match"
CORE_DETAIL_KEYS = {"name", "hsn_code", "hsn_source"}
RESERVED_SPEC_KEYS = CORE_DETAIL_KEYS | {"_negated", "negated_attributes", "min_quantity", "max_quantity"}
_ROLE_WORDS = {"buyer", "buy", "buying", "seller", "sell", "selling"}
_CURRENCY_SYMBOLS = {"$": "usd", "₹": "inr", "€": "eur", "£": "gbp"}

_GROUPED_RE = re.compile(r"(?<![\d,])(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?:,\d{2})+,\d{3})(?!\d)")  # 5,000 / 1,00,000
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_BOUNDARY_RE = re.compile(r"(?<=\d)(?=[^\W\d_])|(?<=[^\W\d_])(?=\d)")  # 500kg -> 500 kg


# ---------------------------------------------------------------------------
# Text comparison
# ---------------------------------------------------------------------------
def _fmt_number(raw: str) -> str:
    try:
        return format(Decimal(raw).normalize(), "f")
    except InvalidOperation:
        return raw


def canon(text: Any) -> str:
    """Comparable form of a text: casefolded, ASCII digits, no thousands commas, no punctuation, numbers canonical."""
    s = unicodedata.normalize("NFKC", str(text if text is not None else "")).casefold()
    s = "".join(str(unicodedata.decimal(ch)) if unicodedata.category(ch) == "Nd" else ch for ch in s)
    s = _GROUPED_RE.sub(lambda m: m.group().replace(",", ""), s)
    s = re.sub(r"(?<=\d)\.(?=\d)", "\x00", s)  # a decimal point is part of the number
    s = "".join(ch if (unicodedata.category(ch)[0] in "LMN" or ch == "\x00") else " " for ch in s)
    s = s.replace("\x00", ".")
    s = _NUM_RE.sub(lambda m: _fmt_number(m.group()), s)
    s = _BOUNDARY_RE.sub(" ", s)
    return " ".join(s.split())


def numbers_in(text: Any) -> set[Decimal]:
    """The numbers written in ``text`` ("5,000 kg" -> {5000}), whatever the digit script."""
    out: set[Decimal] = set()
    for raw in _NUM_RE.findall(canon(text)):
        try:
            out.add(Decimal(raw))
        except InvalidOperation:
            continue
    return out


def has_digit(text: Any) -> bool:
    return bool(_NUM_RE.search(canon(text)))


def canon_sources(sources: Iterable[Any]) -> list[str]:
    """Sources in comparable form, padded so a match is on whole words."""
    return [f" {c} " for c in (canon(s) for s in sources) if c]


_ELLIPSIS_RE = re.compile(r"\.{2,}|…")


def _weak(c: str) -> bool:
    return not c or (len(c) < 2 and not has_digit(c))


def _parts_in_order(parts: list[str], src: str) -> bool:
    """Every part occurs in ``src`` (whole words), each after the one before."""
    pos = 0
    for part in parts:
        needle = f" {part} "
        at = src.find(needle, pos)
        if at < 0:
            return False
        pos = at + len(needle) - 1  # the space after a part may start the next one
    return True


def is_grounded(said: Any, sources: Iterable[Any], *, prepared: bool = False) -> bool:
    """True when ``said`` occurs (as whole words, ignoring case/punctuation/number format) in one of the sources.

    A quote with an ellipsis ("mujhe ... chahiye") is grounded when each part occurs in a source, in order."""
    prepared_sources = list(sources) if prepared else canon_sources(sources)
    pieces = [canon(p) for p in _ELLIPSIS_RE.split(str(said if said is not None else ""))]
    pieces = [p for p in pieces if p]
    if not pieces or any(_weak(p) for p in pieces):
        return False
    return any(_parts_in_order(pieces, src) for src in prepared_sources)


def _number_of(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", canon(value))


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, dict)) and not value)


def _unit_in_text(unit: Any, said: str) -> bool:
    """The unit (or a known spelling of it) appears among the words of ``said``."""
    target = spec_catalog.canonical_unit(str(unit))
    if not target:
        return False
    if canon(unit) and f" {canon(unit)} " in f" {canon(said)} ":
        return True
    for token in canon(said).split():
        if any(ch.isdigit() for ch in token):
            continue
        got = spec_catalog.canonical_unit(token)
        if got and got.lower() == target.lower():
            return True
    return False


def _recognised_unit(token: str) -> bool:
    """A unit word the catalogue or the converter knows (kg, tonnes, bags ...); a stray word or a foreign-script unit is not."""
    if not token or not token.isascii() or any(ch.isdigit() for ch in token):
        return False  # a unit in another script is the model's reading to trust, not one to compare
    if spec_catalog._norm(token) in spec_catalog._UNIT_ALIAS:
        return True
    return any(f(token) for f in (
        unit_converter.is_mass_unit, unit_converter.is_volume_unit, unit_converter.is_length_unit,
        unit_converter.is_count_unit, unit_converter.is_packaging_unit,
    ))


def _unit_keys(unit: Any) -> set[str]:
    base = str(spec_catalog.canonical_unit(str(unit)) or unit).strip().casefold()
    keys = {base}
    if base.endswith("es"):
        keys.add(base[:-2])
    if base.endswith("s"):
        keys.add(base[:-1])
    return {k for k in keys if k}


def units_said(said: str) -> list[str]:
    """The distinct recognised units among the words of ``said`` (canonical spelling)."""
    found: list[str] = []
    for token in canon(said).split():
        if _recognised_unit(token):
            canonical = str(spec_catalog.canonical_unit(token) or token)
            if canonical not in found:
                found.append(canonical)
    return found


def unit_matches_quote(unit: Any, said: str) -> bool:
    """False only when the quote names recognised units and the model's unit is none of them (an invented unit).

    A quote with no recognised unit (a foreign-script unit, "2 container") leaves the model's reading trusted.
    """
    said_units = units_said(said)
    if not said_units:
        return True
    wanted = _unit_keys(unit)
    return any(wanted & _unit_keys(u) for u in said_units)


# ---------------------------------------------------------------------------
# Cleaning one update_rfq_draft call
# ---------------------------------------------------------------------------
@dataclass
class Cleaned:
    """The grounded part of an update. ``kwargs`` are the flat arguments of ``rfq_draft.update_rfq_draft``."""

    kwargs: dict[str, Any] = field(default_factory=dict)
    specs: dict[str, Any] = field(default_factory=dict)  # raw spec key -> value
    evidence: dict[str, str] = field(default_factory=dict)  # draft field path -> quoted words
    rejected: list[dict[str, str]] = field(default_factory=list)  # {"field", "said", "reason"}
    remove: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.kwargs or self.specs or self.remove)


NOT_STATED = "not stated by the user: ask them (quote their exact words in `said` once they answer)"
NO_QUOTE = "missing `said`: give the user's exact words this value came from, or ask them"


class _Cleaner:
    def __init__(self, sources: Iterable[Any], draft: Optional[dict[str, Any]]) -> None:
        self.sources = canon_sources(sources)
        self.draft = draft or {}
        self.out = Cleaned()

    # -- helpers ---------------------------------------------------------
    def reject(self, path: str, reason: str, said: str = "") -> None:
        self.out.rejected.append({"field": path, "said": said, "reason": reason})

    def quote(self, path: str, entry: dict[str, Any]) -> Optional[str]:
        """The grounded ``said`` of an entry, or None after recording why it was rejected."""
        said = str(entry.get("said") or "").strip()
        if not said:
            self.reject(path, NO_QUOTE)
            return None
        if not is_grounded(said, self.sources, prepared=True):
            self.reject(path, NOT_STATED, said)
            return None
        return said

    def number(self, path: str, value: Any, said: str, *, positive: bool = True) -> Optional[Decimal]:
        """``value`` as a number the user's words support (a said without digits, e.g. "five tonnes", is trusted)."""
        number = _number_of(value)
        if number is None:
            self.reject(path, "must be a number")
            return None
        if has_digit(said) and number not in numbers_in(said):
            self.reject(path, f"the number {value} is not in the quoted words '{said}': use the user's own figure", said)
            return None
        return number

    def known_unit(self, section: str) -> str:
        return str((self.draft.get(section) or {}).get("unit") or "").strip()

    # -- fields ----------------------------------------------------------
    def role(self, entry: Any) -> None:
        """Buyer or seller: a classification the recap confirms, so a bare string or a missing ``said`` is fine."""
        raw = entry.get("value") if isinstance(entry, dict) else entry
        value = str(raw or "").strip().lower()
        if not value:
            return
        if value not in _ROLE_WORDS:
            return self.reject("role", "must be buyer or seller")
        self.out.kwargs["role"] = "seller" if value.startswith("sell") else "buyer"
        said = str(entry.get("said") or "").strip() if isinstance(entry, dict) else ""
        self.out.evidence["role"] = said or "classified"

    def text(self, path: str, key: str, entry: dict[str, Any], *, limit: int = 300) -> None:
        value = entry.get("value")
        if _blank(value):
            return
        said = self.quote(path, entry)
        if said:
            self.out.kwargs[key] = " ".join(str(value).split())[:limit]
            self.out.evidence[path] = said

    def hsn(self, entry: dict[str, Any]) -> None:
        value = entry.get("value")
        if _blank(value):
            return
        said = self.quote("hsn_code", entry)
        if not said:
            return
        if not _digits(value) or _digits(value) not in _digits(said):
            return self.reject("hsn_code", f"the code {value} is not in the quoted words '{said}'", said)
        self.out.kwargs["hsn_code"] = str(value).strip()
        self.out.evidence["hsn_code"] = said

    def amount_with_unit(self, path: str, entry: dict[str, Any], section: str, value_key: str, unit_key: str) -> None:
        """quantity / minimum_order: {value, unit, said}. A unit the user never said is asked, not guessed."""
        value, unit = entry.get("value"), entry.get("unit")
        if _blank(value) and _blank(unit):
            return
        said = self.quote(path, entry)
        if not said:
            return
        if _blank(value):  # a unit alone changes the unit of the value already in the draft
            value = (self.draft.get(section) or {}).get("value")
            if value is None:
                return self.reject(path, "no quantity yet: ask the user for the amount")
        number = self.number(path, value, said)
        if number is None:
            return
        from_model = not _blank(unit)
        unit = str(unit).strip() if from_model else self.known_unit(section)
        if from_model and not unit_matches_quote(unit, said):
            return self.reject(
                path, f"the unit '{unit}' is not in the quoted words '{said}' (units there: {', '.join(units_said(said))}): "
                "use the unit the user said", said,
            )
        if not unit:  # the unit is the model's reading of the user's words (any script): only a missing one is asked
            return self.reject(path, "no unit: ask which unit (kg, tonnes, pcs ...)", said)
        self.out.kwargs[value_key] = float(number)
        self.out.kwargs[unit_key] = unit
        self.out.evidence[path] = said

    def price(self, entry: dict[str, Any]) -> None:
        amount = entry.get("amount", entry.get("value"))
        currency = entry.get("currency")
        per = entry.get("per_unit", entry.get("per"))
        if _blank(amount) and _blank(currency) and _blank(per):
            return
        said = self.quote("price_target", entry)
        if not said:
            return
        if _blank(amount):  # only the currency or the per-unit basis of the price already in the draft
            amount = (self.draft.get("price_target") or {}).get("amount")
            if amount is None:
                return self.reject("price_target", "no price yet: ask the user for the amount")
        number = self.number("price_target", amount, said)
        if number is None:
            return
        self.out.kwargs["price_amount"] = float(number)
        if not _blank(currency):
            if self._currency_stated(str(currency), said):
                self.out.kwargs["price_currency"] = str(currency).strip().upper()
            else:
                self.reject("price_target.currency", "currency not stated by the user (INR is assumed): ask if it is not INR", said)
        if not _blank(per):
            if _unit_in_text(per, said) or canon(per) in canon(said):
                self.out.kwargs["price_per_unit"] = str(per).strip()
            else:
                self.reject("price_target.per_unit", "price basis (per kg / per piece ...) not stated by the user: ask", said)
        self.out.evidence["price_target"] = said

    @staticmethod
    def _currency_stated(currency: str, said: str) -> bool:
        cur = currency.strip().lower()
        if cur in ("inr", "rs", "rupee", "rupees", "₹"):
            return True  # the default
        text = canon(said)
        if cur in text.split():
            return True
        return any(sym in said and code == cur for sym, code in _CURRENCY_SYMBOLS.items())

    def location(self, entry: dict[str, Any]) -> None:
        parts = {k: entry.get(k) for k in ("city", "state", "country") if not _blank(entry.get(k))}
        if not parts:
            return
        said = self.quote("location", entry)
        if said:
            self.out.kwargs.update({k: " ".join(str(v).split())[:80] for k, v in parts.items()})
            self.out.evidence["location"] = said

    def deadline(self, entry: dict[str, Any]) -> None:
        days, date = entry.get("in_days", entry.get("days")), entry.get("date")
        if _blank(days) and _blank(date):
            return
        said = self.quote("deadline", entry)
        if not said:
            return
        if not _blank(days):
            number = _number_of(days)
            if number is None or number != number.to_integral_value():
                return self.reject("deadline", "days must be a whole number")
            self.out.kwargs["deadline_days"] = int(number)
        if not _blank(date):
            self.out.kwargs["deadline_date"] = str(date).strip()[:10]
        self.out.evidence["deadline"] = said

    def must_match(self, entry: dict[str, Any]) -> None:
        names = entry.get("value")
        names = [names] if isinstance(names, str) else names
        names = [str(n).strip() for n in names or [] if str(n).strip()]
        if not names:
            return
        said = self.quote("must_match", entry)
        if said:
            self.out.kwargs["must_match"] = names
            self.out.evidence["must_match"] = said

    def specifications(self, specs: Any) -> None:
        if not isinstance(specs, dict):
            return
        for raw_key, entry in specs.items():
            key = " ".join(str(raw_key).split())[:60]
            path = f"specifications.{key}"
            if not isinstance(entry, dict):
                if not _blank(entry):
                    self.reject(path, NO_QUOTE)
                continue
            value = entry.get("value")
            if _blank(value) or not key:
                continue
            if key.casefold() in RESERVED_SPEC_KEYS or key.endswith(MUST_SUFFIX):
                self.reject(path, "not a specification (use the product, quantity, price or HSN fields)")
                continue
            said = self.quote(path, entry)
            if not said:
                continue
            items = value if isinstance(value, list) else [value]
            if has_digit(said) and any(
                has_digit(item) and not numbers_in(item) <= numbers_in(said) for item in items
            ):
                self.reject(path, f"the figure in {value} is not in the quoted words '{said}': use the user's own figure", said)
                continue
            self.out.specs[key] = [" ".join(str(i).split()) for i in items] if isinstance(value, list) else " ".join(str(value).split())
            self.out.evidence[path] = said


def clean_update(
    fields: Any,
    remove: Any,
    sources: Iterable[Any],
    draft: Optional[dict[str, Any]] = None,
) -> Cleaned:
    """Validate the arguments of one ``update_rfq_draft`` call against what the user said.

    ``fields`` maps a field name to ``{"value" ..., "said": "..."}`` (see the tool schema); ``sources`` are the texts a
    ``said`` may come from (the user's messages, options and lookup results shown earlier); ``draft`` is the current
    draft (for a unit given without a value, a unit already in force).
    """
    cleaner = _Cleaner(sources, draft)
    handlers = {
        "role": cleaner.role,
        "product_name": lambda e: cleaner.text("product_name", "product_name", e, limit=200),
        "category": lambda e: cleaner.text("category", "category", e, limit=120),
        "description": lambda e: cleaner.text("description", "description", e, limit=2000),
        "hsn_code": cleaner.hsn,
        "quantity": lambda e: cleaner.amount_with_unit("quantity", e, "quantity", "quantity_value", "quantity_unit"),
        "minimum_order": lambda e: cleaner.amount_with_unit("minimum_order", e, "minimum_order", "min_order_value", "min_order_unit"),
        "price_target": cleaner.price,
        "price": cleaner.price,
        "location": cleaner.location,
        "deadline": cleaner.deadline,
        "must_match": cleaner.must_match,
    }
    for name, entry in (fields.items() if isinstance(fields, dict) else []):
        if name == "specifications":
            cleaner.specifications(entry)
        elif name in handlers:
            if isinstance(entry, dict) or (name == "role" and not _blank(entry)):
                handlers[name](entry)
            elif not _blank(entry):
                cleaner.reject(name, NO_QUOTE)
        elif not _blank(entry):
            cleaner.reject(str(name), "unknown field")
    cleaner.out.remove, bad = valid_removals(remove)
    for path in bad:
        cleaner.reject(path, "cannot be removed")
    return cleaner.out


# ---------------------------------------------------------------------------
# Explicit removal
# ---------------------------------------------------------------------------
_REMOVABLE = {
    "category", "description", "quantity", "minimum_order", "price_target", "location", "deadline", "hsn_code",
    "specifications", "must_match", "location.city", "location.state", "location.country", "price_target.per_unit",
}


def valid_removals(remove: Any) -> tuple[list[str], list[str]]:
    """(paths that may be cleared, paths that may not). ``specifications.<key>`` and ``must_match.<key>`` are free keys."""
    ok: list[str] = []
    bad: list[str] = []
    for raw in (remove if isinstance(remove, list) else []):
        path = " ".join(str(raw or "").split())
        if not path:
            continue
        head, _, tail = path.partition(".")
        if path in _REMOVABLE or (head in ("specifications", "must_match") and tail):
            ok.append(path)
        else:
            bad.append(path)
    return ok, bad


def _spec_key_in(details: dict[str, Any], wanted: str) -> Optional[str]:
    norm = lambda k: re.sub(r"[^a-z0-9]+", "_", str(k).casefold()).strip("_")  # noqa: E731
    for key in details:
        if norm(key) == norm(wanted):
            return key
    return None


def apply_removals(draft: dict[str, Any], paths: Iterable[str]) -> list[str]:
    """Clear the named fields in ``draft`` (in place). Returns the paths that actually removed something."""
    done: list[str] = []
    for path in paths:
        head, _, tail = path.partition(".")
        details = draft.get("product_details") if isinstance(draft.get("product_details"), dict) else {}
        hit = False
        if head == "specifications" and not tail:
            for key in [k for k in details if k not in CORE_DETAIL_KEYS]:
                details.pop(key)
                hit = True
        elif head == "specifications":
            key = _spec_key_in(details, tail)
            if key is not None and key not in CORE_DETAIL_KEYS:
                details.pop(key)
                details.pop(f"{key}{MUST_SUFFIX}", None)
                hit = True
        elif head == "must_match":
            for key in [k for k in details if str(k).endswith(MUST_SUFFIX) and (not tail or _spec_key_in({str(k)[: -len(MUST_SUFFIX)]: 1}, tail))]:
                details.pop(key)
                hit = True
        elif head == "hsn_code":
            hit = any([details.pop(k, None) is not None for k in ("hsn_code", "hsn_source")])
        elif path in ("location.city", "location.state", "location.country", "price_target.per_unit"):
            section = draft.get(head)
            if isinstance(section, dict) and section.pop(tail, None) is not None:
                hit = True
                if not any(section.get(k) for k in section if k != "per_unit") and head == "location":
                    draft.pop(head)
        elif path in _REMOVABLE:
            hit = draft.pop(path, None) is not None
        if hit:
            done.append(path)
    return done


def evidence_after_removal(evidence: dict[str, str], paths: Iterable[str]) -> dict[str, str]:
    """``evidence`` without the entries of the removed fields."""
    out = dict(evidence)
    for path in paths:
        for key in list(out):
            if key == path or key.startswith(f"{path}."):
                out.pop(key)
    return out


# ---------------------------------------------------------------------------
# Product change
# ---------------------------------------------------------------------------
def same_product(old: Any, new: Any) -> bool:
    """True when ``new`` only refines ``old`` ("rice" -> "basmati rice") or equals it, so its specs still apply."""
    a, b = set(canon(old).split()), set(canon(new).split())
    return bool(a and b) and (a <= b or b <= a)


# ---------------------------------------------------------------------------
# Recap facts: values a reply must carry (the model may phrase it in any language)
# ---------------------------------------------------------------------------
def missing_facts(reply: Any, facts: Iterable[Any]) -> list[str]:
    """The facts (numbers, titles, ids) whose canonical form does not occur, as whole words, in ``reply``."""
    text = f" {canon(reply)} "
    out: list[str] = []
    for fact in facts:
        c = canon(fact)
        if c and f" {c} " not in text:
            out.append(str(fact))
    return out

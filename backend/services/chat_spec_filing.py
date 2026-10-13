"""Filing the user's answer to the spec question into the draft, without the model.

The chat asks one combined spec question ("Apple Variety - Fuji, Honeycrisp...; Size Diameter (mm); ..."). The user
answers it in free text: "product type is fuji, quality is minimum Brix lael and packaging in carton size box of 20kg
each, certification must of organic and fssai". A model asked to turn that into tool arguments loses, garbles or
invents values, so the answer is parsed here, before the model runs:

* every spec LABEL mentioned in the answer (a suggestion's label, key or a synonym such as "product type" for a variety,
  or a common spec word such as "certification") anchors a segment of the text; the segment up to the next anchor is
  that spec's value;
* a select spec takes the option(s) found in its segment ("fuji" -> "Fuji", "carton" -> "Carton Box"), also from the
  FULL suggestion list, not only the three that were asked;
* a numeric spec needs a number (and a compatible unit) next to its label; "minimum Brix lael" has none, so nothing is
  stored and ``Filing.ask_value`` names the spec the next question must ask for;
* "must", "mandatory", "zaroori" inside a spec's segment make it a must-match spec;
* a pack size ("20kg each", "box of 20 kg") and any other "<key> is <value>" pair are stored too.

Values are filed under the suggestion's label, like the form's chip does. Pure functions, no database, no model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from services import chat_draft_assist, chat_specs

MUST_SUFFIX = chat_draft_assist.MUST_MATCH_SUFFIX

_MUST_RE = re.compile(
    r"\b(?:must|mandatory|compulsory|non[\s-]?negotiable|strictly|zaroori|zaruri|jaruri|zarori|jaroori|has\s+to|have\s+to|need\s+to|needs\s+to)\b",
    re.IGNORECASE,
)
_QUAL_WORDS = frozenset({
    "minimum", "min", "maximum", "max", "atleast", "least", "at", "than", "less", "more", "not", "upto", "up", "to", "above",
    "below", "under", "over", "around", "about", "approx", "approximately", "required", "need", "needed", "should", "be",
    "is", "are", "of", "the", "a", "an", "in", "and", "or", "also", "with", "good", "fine", "ok", "okay", "great", "enough",
    "necessary", "important", "kuch", "bhi", "hai", "h", "chahiye", "ho", "per", "each", "etc", "please", "pls",
})
_TRIVIAL_VALUES = frozenset({"fine", "ok", "okay", "good", "great", "enough", "required", "needed", "necessary", "important", "yes", "no"})

_WEAK_TOKENS = frozenset({"size", "type", "grade", "kind", "rate", "time", "level", "state", "form", "use", "mode"})
_GENERIC_TOKENS = frozenset({
    "content", "level", "applied", "treatment", "percentage", "percent", "rating", "score", "value", "range", "number",
    "index", "status", "kind", "amount", "name", "required", "preferred", "specification", "spec", "details", "detail",
})
# A synonym phrase for a spec, keyed by a word of its label.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "variety": ("product type", "cultivar", "variant"),
    "grade": ("quality",),
    "packaging": ("packing", "package"),
    "certification": ("certificate", "certified"),
    "origin": ("grown in",),
}
_NUMERIC_GENERIC = ("brix", "moisture", "purity", "firmness", "diameter", "thickness", "protein")
# Generic spec words: (pattern, label, kind). A suggestion with the same span wins over these.
_GENERIC: list[tuple[str, str, str]] = [
    (r"certifications?|certificates?|certified", "Certifications", "list"),
    (r"packaging|packing|package", "Packaging", "text"),
    (r"quality", "Quality", "text"),
    (r"grade", "Grade", "text"),
    (r"variety|varieties|cultivars?|variants?|product\s+type", "Variety", "text"),
    (r"origin", "Origin", "text"),
    (r"colou?r", "Color", "text"),
    (r"brand", "Brand", "text"),
    (r"shelf\s+life", "Shelf Life", "text"),
    (r"texture", "Texture", "text"),
    *[(w, w.title(), "numeric") for w in _NUMERIC_GENERIC],
]
# "<key> is <value>" keys that are not specs (the form's own fields and conversation words).
_PAIR_SKIP = frozenset({
    "it", "this", "that", "there", "here", "what", "who", "which", "price", "budget", "quantity", "qty", "moq", "delivery",
    "location", "city", "state", "country", "deadline", "date", "days", "role", "name", "title", "product", "rfq", "hsn",
    "hsn code", "code", "order", "category", "description", "amount", "rate", "cost", "total", "all", "everything", "answer",
    "reply", "message", "question", "one", "two", "ok", "okay", "yes", "no", "i", "we", "you", "they", "he", "she",
})
_KEY_STOP = frozenset({
    "and", "or", "with", "also", "but", "that", "which", "then", "if", "so", "it", "this", "there", "what", "who", "the",
    "my", "our", "your", "a", "an", "in", "of", "for", "to", "should", "must", "be", "am", "i", "we", "they", "he", "she",
    "you", "please", "ok", "okay", "yes", "no", "aur", "ka", "ki", "ke", "hai", "bhi", "when", "where", "how", "why",
})

_UNIT_WORDS = r"(?:kgs?|kilos?|kilograms?|g|gm|gms|grams?|mg|lbs?|tonnes?|tons?|mt|quintals?|l|ltr|litres?|liters?|ml|pcs|pieces?)"
_PACK_CONTAINER = r"(?:box(?:es)?|cartons?|bags?|packs?|packets?|sacks?|crates?|cases?|bundles?|tins?|cans?|bottles?|units?|pieces?|pcs?)"
_PACK_RES = [
    re.compile(
        rf"(?P<n>\d+(?:\.\d+)?)\s*(?P<u>{_UNIT_WORDS})\s*(?:each\b|per\s+{_PACK_CONTAINER}\b|/\s*{_PACK_CONTAINER}\b)", re.IGNORECASE
    ),
    re.compile(
        rf"\b(?:pack(?:et|ing|age)?|{_PACK_CONTAINER})\s*(?:size|weight|wt)?\s*(?:of|:|=|is)?\s*(?P<n>\d+(?:\.\d+)?)\s*(?P<u>{_UNIT_WORDS})\b",
        re.IGNORECASE,
    ),
]
_UNIT_CANON = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg",
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g", "mg": "mg", "lb": "lb", "lbs": "lb",
    "tonne": "tonne", "tonnes": "tonne", "ton": "tonne", "tons": "tonne", "mt": "tonne", "quintal": "quintal", "quintals": "quintal",
    "l": "l", "ltr": "l", "litre": "l", "litres": "l", "liter": "l", "liters": "l", "ml": "ml",
    "pcs": "pcs", "piece": "pcs", "pieces": "pcs",
}
_MEASURE_UNITS = frozenset({
    "mm", "cm", "m", "kg", "kgs", "g", "gm", "mg", "kpa", "pa", "%", "bx", "brix", "day", "days", "pcs", "l", "ml", "lb", "lbs",
    "mt", "ton", "tons", "tonne", "tonnes", "percent", "pct", "hrs", "hours", "c", "f", "ppm", "gsm", "mpa", "psi", "bar",
})
_UNIT_GROUPS = [
    {"%", "percent", "pct"}, {"bx", "brix"}, {"day", "days"}, {"kg", "kgs"}, {"g", "gm", "gms"}, {"ton", "tons", "tonne", "tonnes", "mt"},
    {"lb", "lbs"}, {"hrs", "hours"},
]
_NUM_RE = re.compile(
    r"(?P<q>>=|<=|>|<)?\s*(?P<n>\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(?P<n2>\d+(?:\.\d+)?))?\s*(?P<u>%|°\s*[a-z]{1,4}|[a-z]{1,9})?",
    re.IGNORECASE,
)
_QUAL_RE = re.compile(
    r"\b(?:min(?:imum)?|max(?:imum)?|at\s*least|not\s+(?:less|more)\s+than|up\s*to|upto|above|below|under|over)\b|>=|<=|>|<", re.IGNORECASE
)
_LEAD_RE = re.compile(
    r"^[\s,:;=\-–.]*(?:(?:is|are|was|be|being|should|shall|will|of|in|to|as|the|a|an|hai|hain|h|ho|ke|ki|ka|me|mein|chahiye|with|"
    r"equal|around|about|approx|approximately|like|should\s+be)\b[\s,:;=\-–.]*)+",
    re.IGNORECASE,
)
_TRAIL_RE = re.compile(
    r"(?:[\s,:;.\-–!]+|\b(?:and|or|also|with|aur|bhi|plus|each|please|pls|ok|okay|thanks|etc|per|in|of|is|are|be)\b)+$", re.IGNORECASE
)
_ACRONYMS = {
    "fssai": "FSSAI", "haccp": "HACCP", "iso": "ISO", "gmp": "GMP", "brc": "BRC", "usda": "USDA", "apeda": "APEDA", "fda": "FDA",
    "eu": "EU", "npop": "NPOP", "bis": "BIS", "isi": "ISI", "fsc": "FSC", "sqf": "SQF", "ifs": "IFS", "bsci": "BSCI",
    "sedex": "SEDEX", "ce": "CE", "ukca": "UKCA", "globalgap": "GlobalGAP", "gap": "GAP", "agmark": "AGMARK", "kosher": "Kosher",
    "halal": "Halal", "organic": "Organic",
}
_LIST_SPLIT_RE = re.compile(r"\s*(?:,|;|/|&|\+|\band\b|\bor\b|\bplus\b|\baur\b)\s*", re.IGNORECASE)


def _norm(text: Any) -> str:
    return re.sub(r"[\s_\-]+", " ", str(text)).strip().lower()


def _tokens(text: Any) -> set[str]:
    return {chat_specs._stem(t) for t in re.findall(r"[a-z]+", str(text).lower())}


def _unit_norm(u: str) -> str:
    return re.sub(r"[°\s]", "", (u or "").lower())


def _same_unit(a: str, b: str) -> bool:
    a, b = _unit_norm(a), _unit_norm(b)
    if a == b:
        return True
    return any(a in g and b in g for g in _UNIT_GROUPS)


# ---------------------------------------------------------------------------
# Specs and anchors
# ---------------------------------------------------------------------------
@dataclass
class _Spec:
    label: str
    kind: str  # numeric | choice | text | list
    key: str = ""
    options: list[str] = field(default_factory=list)
    unit: str = ""
    placeholder: str = ""
    suggested: bool = False
    must_preset: bool = False  # the suggestion comes with must-match switched on


@dataclass
class _Anchor:
    start: int
    end: int
    spec: _Spec
    strong: bool
    priority: int


@dataclass
class Filing:
    """What an answer to the spec question filed."""

    stored: dict[str, Any] = field(default_factory=dict)  # label -> value, new or changed
    must: list[str] = field(default_factory=list)  # labels flagged must-match by this answer
    ask_value: Optional[dict[str, Any]] = None  # a numeric spec named without a usable number
    matched: set[str] = field(default_factory=set)  # every label this answer spoke about

    @property
    def touched(self) -> bool:
        return bool(self.stored or self.must or self.ask_value)


def _label_core(label: str) -> str:
    return re.sub(r"\([^)]*\)", " ", label).strip()


def _label_unit(label: str) -> str:
    m = re.search(r"\(([^)]+)\)", label or "")
    if m and re.fullmatch(r"[°%]?\s*[A-Za-z%]{1,6}", m.group(1).strip()):
        return m.group(1).strip()
    return ""


def _kind_of(spec: dict[str, Any], unit: str) -> str:
    options = [str(o).strip() for o in (spec.get("options") or []) if str(o).strip()]
    numeric_options = bool(options) and all(re.fullmatch(r"\d+(?:\.\d+)?", o) for o in options)
    if str(spec.get("input_type") or "").lower() == "numeric" or unit or numeric_options:
        return "numeric"
    if options:
        return "choice"
    tokens = _tokens(f"{spec.get('label') or ''} {spec.get('key') or ''}")
    if "certification" in tokens:
        return "list"
    return "text"


def _suggestion_specs(suggestions: Optional[list[dict[str, Any]]]) -> list[_Spec]:
    out: list[_Spec] = []
    for s in suggestions or []:
        if not isinstance(s, dict):
            continue
        label = str(s.get("label") or s.get("key") or "").strip()
        if not label:
            continue
        unit = _label_unit(label)
        out.append(_Spec(
            label=label, kind=_kind_of(s, unit), key=str(s.get("key") or ""),
            options=[str(o).strip() for o in (s.get("options") or []) if str(o).strip()],
            unit=unit, placeholder=str(s.get("placeholder") or ""), suggested=True, must_preset=bool(s.get("must_match")),
        ))
    return out


def _phrase_re(phrase: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in re.split(r"[\s_\-]+", phrase.strip()) if w]
    return re.compile(r"(?<![\w])" + r"[\s_\-]+".join(words) + r"(?:s|es)?(?![\w])", re.IGNORECASE)


_PACK_BEFORE_RE = re.compile(r"(?:carton|cartons|pack|packs|packet|box|boxes|bag|bags|packing|case|crate|sack)\s*$", re.IGNORECASE)


def _anchors_for_suggestion(spec: _Spec, text: str, product_tokens: set[str]) -> list[_Anchor]:
    core = _label_core(spec.label)
    key_phrase = re.sub(r"_(?:mm|cm|kg|g|pct|percent|days?|kpa|pcs)$", "", spec.key.lower()).replace("_", " ").strip()
    found: list[_Anchor] = []

    def add(rx: re.Pattern[str], strong: bool) -> None:
        for m in rx.finditer(text):
            if not strong and _PACK_BEFORE_RE.search(text[max(0, m.start() - 14): m.start()]):
                continue  # "carton size" names a pack, not the size spec
            found.append(_Anchor(m.start(), m.end(), spec, strong, 0))

    if core:
        add(_phrase_re(core), True)
    if key_phrase and _norm(key_phrase) != _norm(core):
        add(_phrase_re(key_phrase), True)
    label_tokens = re.findall(r"[a-z]+", core.lower())
    for tok in label_tokens:
        if tok in _GENERIC_TOKENS or len(tok) < 3 or chat_specs._stem(tok) in product_tokens or tok == "type":
            continue
        add(_phrase_re(tok), tok not in _WEAK_TOKENS)
        for syn in _SYNONYMS.get(chat_specs._stem(tok), ()) + _SYNONYMS.get(tok, ()):
            add(_phrase_re(syn), True)
    if "variety" in {chat_specs._stem(t) for t in label_tokens}:
        add(re.compile(r"(?<![\w])type(?=\s*(?:is|are|:|=))", re.IGNORECASE), True)
    return found


def _generic_anchors(text: str) -> list[_Anchor]:
    out: list[_Anchor] = []
    for pattern, label, kind in _GENERIC:
        spec = _Spec(label=label, kind=kind)
        for m in re.finditer(rf"(?<![\w])(?:{pattern})(?![\w])", text, re.IGNORECASE):
            out.append(_Anchor(m.start(), m.end(), spec, True, 1))
    return out


def _pair_anchors(text: str) -> list[_Anchor]:
    """"<key> is <value>" / "<key>: <value>" for keys that are not otherwise known (lowest priority)."""
    out: list[_Anchor] = []
    for m in re.finditer(r"\b(?:is|are)\b|[:=]", text, re.IGNORECASE):
        before = text[:m.start()]
        words = list(re.finditer(r"[A-Za-z][A-Za-z\-]*", before))
        if not words or before[words[-1].end():].strip():
            continue
        take: list[re.Match[str]] = []
        idx = len(words) - 1
        while idx >= 0 and len(take) < 3:
            w = words[idx]
            if take and before[w.end(): take[0].start()].strip():
                break
            if w.group().lower() in _KEY_STOP:
                break
            take.insert(0, w)
            idx -= 1
        if not take or len(take) > 2:
            continue
        key = " ".join(t.group().lower() for t in take)
        if key in _PAIR_SKIP or any(w in _PAIR_SKIP for w in key.split()) or chat_specs.is_core_key(key):
            continue
        out.append(_Anchor(take[0].start(), m.end(), _Spec(label=" ".join(w.capitalize() for w in key.split()), kind="text"), False, 2))
    return out


def _resolve(anchors: list[_Anchor]) -> list[_Anchor]:
    """Overlaps go to the higher priority, then the longer span; one anchor per spec label (the first by position)."""
    accepted: list[_Anchor] = []
    for a in sorted(anchors, key=lambda x: (x.priority, -(x.end - x.start), x.start)):
        if any(a.start < b.end and b.start < a.end for b in accepted):
            continue
        accepted.append(a)
    accepted.sort(key=lambda x: x.start)
    seen: set[str] = set()
    out: list[_Anchor] = []
    for a in accepted:
        k = _norm(a.spec.label)
        if k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------
def pack_size(text: str) -> Optional[tuple[str, tuple[int, int]]]:
    """"20kg each" / "box of 20 kg" / "20 kg per bag" -> ("20 kg", span)."""
    for rx in _PACK_RES:
        m = rx.search(text)
        if m:
            n = float(m.group("n"))
            unit = _UNIT_CANON.get(m.group("u").lower(), m.group("u").lower())
            return f"{n:g} {unit}", (m.start(), m.end())
    return None


def _qualifier(text: str) -> Optional[str]:
    """"min" or "max" from the last qualifier word in ``text``, or None."""
    found = None
    for m in _QUAL_RE.finditer(text or ""):
        q = m.group(0).lower()
        found = "min" if q.startswith(("min", "at", "not less")) or q in (">", ">=", "above", "over") else "max"
    return found


def _find_number(seg: str, spec: _Spec) -> Optional[tuple[str, str, int]]:
    """The first number in ``seg`` with a unit compatible with the spec: (number text, unit the user wrote or "", start)."""
    for m in _NUM_RE.finditer(seg):
        unit = _unit_norm(m.group("u") or "")
        if unit and unit not in _MEASURE_UNITS:
            unit = ""  # "14 each": a word, not a unit
        if unit and spec.unit and not _same_unit(unit, spec.unit):
            continue  # "20kg" is not a size in mm
        number = m.group("n") + (f"-{m.group('n2')}" if m.group("n2") else "")
        return number, ((m.group("u") or "").strip() if unit else ""), m.start()
    return None


def _numeric_value(number: str, unit: str, qualifier: Optional[str]) -> str:
    text = f"{number} {unit}".strip() if unit else number
    return f"{qualifier} {text}" if qualifier else text


def _clean(seg: str) -> str:
    v = _MUST_RE.sub(" ", seg)
    v = re.sub(r"\s+", " ", v)
    v = _LEAD_RE.sub("", v)
    cut = re.search(r"[.?!]\s|\s(?:but|so|then|please|pls)\b", v, re.IGNORECASE)
    if cut:
        v = v[: cut.start()]
    prev = None
    while prev != v:
        prev = v
        v = _TRAIL_RE.sub("", v).strip()
    words = re.findall(r"[^\W_]+", v.lower())
    if not words or all(w in _QUAL_WORDS for w in words):
        return ""
    return v.strip(" ,;:.-–")


def _title_if_plain(value: str) -> str:
    return " ".join(w.capitalize() if w.islower() else w for w in value.split())


def _canon_item(item: str) -> str:
    words = item.strip().split()
    return " ".join(_ACRONYMS.get(w.lower(), w.capitalize() if w.islower() else w) for w in words)


def _list_value(raw: str) -> str:
    items = []
    for part in _LIST_SPLIT_RE.split(raw):
        part = _clean(part)
        if part and part.lower() not in {"certified", "certificate", "certification", "certifications", "all", "the"}:
            items.append(_canon_item(part))
    return ", ".join(dict.fromkeys(items))


def _option_tokens(option: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", option.lower()) if t not in ("of", "and", "the", "or")]


def match_options(spec: _Spec, text: str, *, loose: bool = True) -> list[str]:
    """The spec's options the text speaks of: the whole phrase, one distinctive word (>= 5 letters, in no other option)
    and, with ``loose`` (the text is the spec's own segment), all the option's words in any order or a bare number."""
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9]+", lowered))
    stems = {chat_specs._stem(w) for w in words}
    found: list[str] = []
    for option in spec.options:
        toks = _option_tokens(option)
        if not toks:
            continue
        if all(re.fullmatch(r"\d+(?:\.\d+)?", t) for t in toks):
            if loose and re.search(rf"(?<![\d.]){re.escape(option.strip())}(?![\d])", text):
                found.append(option)
            continue
        if len(option.strip()) >= 4 and _phrase_re(option).search(text):
            found.append(option)
            continue
        if loose and all(t in words or chat_specs._stem(t) in stems for t in toks):
            found.append(option)
            continue
        others = {t for o in spec.options if o != option for t in _option_tokens(o)}
        if any(len(t) >= 5 and t not in others and (t in words or chat_specs._stem(t) in stems) for t in toks):
            found.append(option)
    return found


# ---------------------------------------------------------------------------
# The filing itself
# ---------------------------------------------------------------------------
def _product_tokens(draft: dict[str, Any]) -> set[str]:
    return _tokens(chat_draft_assist.product_name(draft))


def _pack_label(specs: list[_Spec]) -> str:
    for s in specs:
        t = _tokens(s.label)
        if t & {"pack", "packing", "packaging"} and t & {"size", "weight"}:
            return s.label
    return "Pack Size"


def _examples(spec: _Spec, limit: int = 2) -> list[str]:
    nums = [o for o in spec.options if re.fullmatch(r"\d+(?:\.\d+)?", o.strip())]
    if nums:
        return nums[:limit]
    if spec.placeholder and re.fullmatch(r"\d+(?:\.\d+)?", spec.placeholder.strip()):
        return [spec.placeholder.strip()]
    return []


def file_answer(
    draft: dict[str, Any], message: str, suggestions: Optional[list[dict[str, Any]]] = None,
    pending_value: Optional[dict[str, Any]] = None,
) -> Filing:
    """Parse ``message`` as an answer to the spec question and file it into ``draft["product_details"]`` (in place)."""
    filing = Filing()
    text = message or ""
    if not text.strip():
        return filing
    specs = _suggestion_specs(suggestions)
    product_tokens = _product_tokens(draft)
    details = draft.get("product_details")
    if not isinstance(details, dict):
        details = {}

    if pending_value:
        _file_pending(details, text, pending_value, filing)

    pack = pack_size(text)
    pack_span = pack[1] if pack else None

    anchors: list[_Anchor] = []
    for spec in specs:
        anchors.extend(_anchors_for_suggestion(spec, text, product_tokens))
    anchors.extend(_generic_anchors(text))
    anchors.extend(_pair_anchors(text))
    anchors = [a for a in _resolve(anchors) if not (pack_span and a.start < pack_span[1] and pack_span[0] < a.end)]

    # Segment boundaries: each anchor's value runs to the next anchor. A number written BEFORE a numeric label
    # ("min 14 brix") belongs to that label and is cut off the previous segment.
    seg_start = [a.end for a in anchors]
    seg_end = [anchors[i + 1].start if i + 1 < len(anchors) else len(text) for i in range(len(anchors))]
    pre_numbers: dict[int, tuple[str, str, Optional[str]]] = {}
    for i, a in enumerate(anchors):
        if a.spec.kind != "numeric" or _find_number(text[seg_start[i]: seg_end[i]], a.spec):
            continue
        window_start = anchors[i - 1].end if i else 0
        m = re.search(
            r"(?P<q>(?:min(?:imum)?|max(?:imum)?|at\s*least|up\s*to|upto|above|below|under|over|>=|<=|>|<)\s*)?(?P<n>\d+(?:\.\d+)?)\s*"
            r"(?P<u>%|°\s*[a-z]{1,4}|[a-z]{1,9})?\s*(?:of\s+)?$",
            text[window_start: a.start], re.IGNORECASE,
        )
        if not m:
            continue
        unit = _unit_norm(m.group("u") or "")
        if unit and unit not in _MEASURE_UNITS:
            unit = ""
        if unit and a.spec.unit and not _same_unit(unit, a.spec.unit):
            continue
        pre_numbers[i] = (m.group("n"), (m.group("u") or "").strip() if unit else "", _qualifier(m.group("q") or ""))
        if i:
            seg_end[i - 1] = min(seg_end[i - 1], window_start + m.start())

    def put(label: str, value: Any, must: bool) -> None:
        if not label or chat_specs.is_core_key(label) or label.lower() in ("name", "hsn code", "hsn"):
            return
        filing.matched.add(label)
        existing = next((k for k in chat_draft_assist.spec_keys(details) if _norm(k) == _norm(label)), None)
        if existing is None or _norm(details.get(existing)) != _norm(value):
            filing.stored[label] = value
        if must:
            filing.must.append(label)

    for i, a in enumerate(anchors):
        raw = text[seg_start[i]: seg_end[i]]
        if pack_span:  # the pack size is filed on its own
            lo, hi = max(pack_span[0], seg_start[i]) - seg_start[i], min(pack_span[1], seg_end[i]) - seg_start[i]
            if lo < hi:
                raw = raw[:lo] + " " + raw[hi:]
        spec = a.spec
        must = bool(_MUST_RE.search(raw))
        if spec.kind == "numeric":
            hit = _find_number(raw, spec)
            window_start = anchors[i - 1].end if i else 0
            qual = _qualifier(text[max(window_start, a.start - 30): a.start])
            if not qual and pending_value and _norm(pending_value.get("label")) == _norm(spec.label):
                qual = {"minimum": "min", "maximum": "max"}.get(str(pending_value.get("qualifier") or ""))  # "14 brix" after "what minimum?"
            if hit:
                put(spec.label, _numeric_value(hit[0], hit[1], _qualifier(raw[: hit[2]]) or qual), must)
            elif i in pre_numbers:
                n, u, q = pre_numbers[i]
                put(spec.label, _numeric_value(n, u, q or qual), must)
            elif a.strong and filing.ask_value is None:
                word = {"min": "minimum", "max": "maximum"}.get(qual or _qualifier(raw) or "", "")
                filing.ask_value = {
                    "label": spec.label, "key": spec.key, "qualifier": word, "examples": _examples(spec), "unit": spec.unit,
                }
                filing.matched.add(spec.label)
            continue
        if spec.kind == "choice":
            chosen = match_options(spec, raw)
            if chosen:
                put(spec.label, ", ".join(chosen), must)
                continue
        elif spec.kind == "list":
            value = _list_value(_MUST_RE.sub(" ", raw))
            if value:
                put(spec.label, value, must)
            continue
        value = _clean(raw)
        if spec.label == "Packaging":
            value = _clean(re.sub(r"\bsize\b", " ", value, flags=re.IGNORECASE))
        if value and value.lower() not in _TRIVIAL_VALUES and len(value.split()) <= 6:
            put(spec.label, _title_if_plain(value) if spec.kind == "text" else value, must)

    # Choice specs whose label was not mentioned: an option said on its own ("fuji") still answers them.
    for spec in specs:
        if spec.kind != "choice" or _norm(spec.label) in {_norm(x) for x in filing.matched}:
            continue
        probe = text
        if pack_span:
            probe = text[: pack_span[0]] + " " + text[pack_span[1]:]
        chosen = match_options(spec, probe, loose=False)
        if chosen:
            put(spec.label, ", ".join(chosen), False)

    if pack:
        label = _pack_label(specs)
        if not any(_norm(k) == _norm(label) for k in filing.stored) and not any(_norm(l) == _norm(label) for l in filing.matched):
            put(label, pack[0], False)

    _apply(draft, details, specs, filing)
    return filing


def _file_pending(details: dict[str, Any], text: str, pending: dict[str, Any], filing: Filing) -> None:
    """A bare value ("14", "min 14", "12-14 brix") answers the value question asked last turn."""
    spec = _Spec(label=str(pending.get("label") or ""), kind="numeric", unit=str(pending.get("unit") or ""))
    if not spec.label or len(text.split()) > 6:
        return
    hit = _find_number(text, spec)
    if not hit:
        return
    qual = _qualifier(text) or {"minimum": "min", "maximum": "max"}.get(str(pending.get("qualifier") or ""))
    value = _numeric_value(hit[0], hit[1], qual)
    filing.matched.add(spec.label)
    existing = next((k for k in chat_draft_assist.spec_keys(details) if _norm(k) == _norm(spec.label)), None)
    if existing is None or _norm(details.get(existing)) != _norm(value):
        filing.stored[spec.label] = value


def _apply(draft: dict[str, Any], details: dict[str, Any], specs: list[_Spec], filing: Filing) -> None:
    if not filing.stored and not filing.must:
        return
    for label, value in filing.stored.items():
        spec = next((s for s in specs if _norm(s.label) == _norm(label)), None)
        for k in list(chat_draft_assist.spec_keys(details)):
            same = _norm(k) == _norm(label) or (spec is not None and spec.key and _norm(k) == _norm(spec.key))
            if same and k != label:
                details.pop(k, None)
                details.pop(f"{k}{MUST_SUFFIX}", None)
        details[label] = value
        if spec is not None and spec.must_preset and label not in filing.must:
            # The form's preset would flag it; here the user answered in words and did not say "must": it stays optional.
            details.setdefault(f"{label}{MUST_SUFFIX}", False)
    for label in filing.must:
        if label in details:
            details[f"{label}{MUST_SUFFIX}"] = True
    draft["product_details"] = details


# ---------------------------------------------------------------------------
# After the model: its own spec keys must not duplicate or contradict what was filed
# ---------------------------------------------------------------------------
def _flat(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def prune_model_specs(draft: dict[str, Any], filing: Filing, keys_before: set[str]) -> list[str]:
    """Drop the specs the MODEL added this turn that repeat a filed spec (another key for the same thing, the same
    value, an ``organic: True`` flag next to ``Certifications: Organic``) or that restate the garbled value of a spec
    the next question asks for. Returns the dropped keys."""
    details = draft.get("product_details")
    if not isinstance(details, dict) or not filing.touched:
        return []
    filed = {k: details.get(k) for k in filing.matched if k in details}
    filed_values = {_norm(_flat(v)) for v in filed.values()}
    filed_words = _tokens(" ".join(_flat(v) for v in filed.values()))
    ask_tokens = _tokens((filing.ask_value or {}).get("label", "")) - {"content", "sugar"} if filing.ask_value else set()
    dropped: list[str] = []
    for key in list(chat_draft_assist.spec_keys(details)):
        if key in keys_before or key in filing.matched or key in filed:
            continue
        kt = _tokens(key)
        value = details.get(key)
        duplicate = any(kt and ft and (kt <= ft or ft <= kt) for ft in (_tokens(f) for f in filed))
        duplicate = duplicate or _norm(_flat(value)) in filed_values
        duplicate = duplicate or (value is True and bool(kt) and kt <= filed_words)
        garbled = bool(ask_tokens) and bool(ask_tokens & (kt | _tokens(_flat(value))))
        if duplicate or garbled:
            details.pop(key, None)
            details.pop(f"{key}{MUST_SUFFIX}", None)
            dropped.append(key)
    return dropped

"""Bilingual product vocabulary: local trade names mapped onto one English term.

Indian buyers and sellers write the same commodity several ways -- "pyaz",
"pyaaz", "kanda" and "onion" are one product. Without a shared table a buyer
asking for "pyaz" never sees the "onion" seller, because neither lexical overlap
nor the distinct-product gate knows the two words mean the same thing.

The table is deliberately small and high-precision: only words that are
unambiguous in a trade context. Canonical terms are singular English, in the
form ``match_scoring.singularise`` produces, so they compare directly against
tokenised listing text.

This module must not import ``match_scoring`` (which imports it).
"""

import re
from typing import Iterable, Optional

# canonical english term -> local / variant spellings
_GROUPS: dict[str, tuple[str, ...]] = {
    # vegetables
    "onion": ("pyaz", "pyaaz", "piyaz", "piyaaz", "pyaj", "kanda", "kaanda"),
    "potato": ("aloo", "aaloo", "batata", "potatoe"),
    "tomato": ("tamatar", "tamaatar", "tamater", "tomatoe"),
    "garlic": ("lahsun", "lehsun", "lasun", "lehsan"),
    "ginger": ("adrak", "adrakh"),
    "brinjal": ("baingan", "baigan", "eggplant", "aubergine"),
    "okra": ("bhindi", "ladyfinger"),
    "cauliflower": ("gobhi", "gobi", "phool gobhi"),
    "cabbage": ("patta gobhi", "bandh gobhi"),
    "pea": ("matar", "mattar"),
    "chilli": ("mirch", "mirchi", "chili", "chilly", "chily"),
    "coriander": ("dhaniya", "dhania"),
    "lemon": ("nimbu", "neembu"),
    # fruit
    "banana": ("kela", "kele"),
    "apple": ("seb", "saib"),
    "grape": ("angoor", "angur"),
    # grains, pulses, oilseeds
    "rice": ("chawal", "chaawal", "chaval"),
    "wheat": ("gehun", "gehu", "gahu", "gehoon", "gehuu"),
    "lentil": ("dal", "daal", "dhal"),
    "chickpea": ("chana", "channa", "chole"),
    "maize": ("makka", "makki", "bhutta", "corn"),
    "millet": ("bajra", "jowar", "ragi"),
    "mustard": ("sarson", "sarso", "rai"),
    "sesame": ("til", "gingelly"),
    "groundnut": ("moongfali", "mungfali", "peanut"),
    "soybean": ("soyabean", "soya", "soy"),
    "flour": ("atta", "aata", "maida"),
    # spices
    "turmeric": ("haldi", "haldee"),
    "cumin": ("jeera", "jira", "zeera"),
    "cardamom": ("elaichi", "ilaichi", "elaychi"),
    "clove": ("laung", "long"),
    "black pepper": ("kali mirch",),
    # commodities
    "sugar": ("chini", "cheeni", "shakkar"),
    "jaggery": ("gud", "gur", "gurh"),
    "salt": ("namak",),
    "cashew": ("kaju",),
    "almond": ("badam", "baadam"),
    "milk": ("doodh", "dudh"),
    "cotton": ("kapas", "kapaas", "rui"),
    "silk": ("resham",),
    "wool": ("oon",),
    "iron": ("loha", "lohe"),
    "copper": ("tamba", "taamba"),
    "brass": ("peetal", "pital"),
    "wood": ("lakdi", "lakadi", "lakri"),
    "paper": ("kagaz", "kaagaz", "kagad"),
    "fabric": ("kapda", "kapada", "kapra"),
}

# Words that are real English and must never be rewritten, even if some
# transliteration collides with them ("long" is also clove in Hindi).
_NEVER_REWRITE = frozenset({"long", "rai", "til", "rui", "soy"})

# alias -> canonical, single-word aliases only
ALIASES: dict[str, str] = {}
# multi-word alias phrases -> canonical ("kali mirch" -> "black pepper")
PHRASES: dict[str, str] = {}
for _canonical, _aliases in _GROUPS.items():
    for _alias in _aliases:
        _alias = _alias.lower()
        if " " in _alias:
            PHRASES[_alias] = _canonical
        elif _alias not in _NEVER_REWRITE:
            ALIASES[_alias] = _canonical
# English plural spellings that the crude stemmer does not reach.
ALIASES.update({"chillies": "chilli", "chilies": "chilli", "chillis": "chilli", "chilis": "chilli"})

_PHRASE_RE = (
    re.compile(r"\b(?:" + "|".join(re.escape(p) for p in sorted(PHRASES, key=len, reverse=True)) + r")\b")
    if PHRASES
    else None
)
_WORD_RE = re.compile(r"[a-z]+")


def canonical_token(word: str) -> Optional[str]:
    """The English term for a single local word, or None if it is not one."""
    return ALIASES.get(word.lower())


def canonicalise_text(text: Optional[str]) -> str:
    """Rewrite local product words in free text to their English terms.

    Used before keyword category detection, so "50 quintal pyaz" is recognised
    as Agriculture. Unknown words pass through unchanged.
    """
    if not text:
        return ""
    lowered = text.lower()
    if _PHRASE_RE is not None:
        lowered = _PHRASE_RE.sub(lambda m: PHRASES[m.group(0)], lowered)
    return _WORD_RE.sub(lambda m: ALIASES.get(m.group(0), m.group(0)), lowered)


def variants(term: str) -> set[str]:
    """Every spelling of ``term`` that should be searched for.

    ``variants("pyaz") == variants("onion") == {"onion", "pyaz", "pyaaz", ...}``.
    Unknown terms return just themselves.
    """
    lowered = term.lower()
    canonical = ALIASES.get(lowered) or PHRASES.get(lowered) or lowered
    group = _GROUPS.get(canonical)
    found = {canonical, lowered}
    if group:
        found.update(a.lower() for a in group if " " not in a)
    return found


def expand_terms(terms: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for term in terms:
        out |= variants(term)
    return out

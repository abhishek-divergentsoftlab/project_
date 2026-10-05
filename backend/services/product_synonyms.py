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
    "onion": ("pyaz", "pyaaz", "piyaz", "piyaaz", "pyaj", "kanda", "kaanda", "प्याज", "कांदा"),
    "potato": ("aloo", "aaloo", "batata", "potatoe", "आलू"),
    "tomato": ("tamatar", "tamaatar", "tamater", "tomatoe", "टमाटर"),
    "garlic": ("lahsun", "lehsun", "lasun", "lehsan", "लहसुन"),
    "ginger": ("adrak", "adrakh", "अदरक"),
    "brinjal": ("baingan", "baigan", "eggplant", "aubergine", "बैंगन"),
    "okra": ("bhindi", "ladyfinger", "भिंडी"),
    "cauliflower": ("gobhi", "gobi", "phool gobhi", "गोभी"),
    "cabbage": ("patta gobhi", "bandh gobhi", "पत्ता गोभी"),
    "pea": ("matar", "mattar", "मटर"),
    "chilli": ("mirch", "mirchi", "chili", "chilly", "chily", "मिर्च", "मिर्ची"),
    "coriander": ("dhaniya", "dhania", "धनिया"),
    "lemon": ("nimbu", "neembu", "नींबू"),
    # fruit
    "banana": ("kela", "kele", "केला"),
    "apple": ("seb", "saib", "सेब"),
    "grape": ("angoor", "angur", "अंगूर"),
    # grains, pulses, oilseeds
    "rice": ("chawal", "chaawal", "chaval", "चावल"),
    "wheat": ("gehun", "gehu", "gahu", "gehoon", "gehuu", "गेहूं", "गेहु"),
    "lentil": ("dal", "daal", "dhal", "दाल"),
    "chickpea": ("chana", "channa", "chole", "bengal gram", "gram", "चना", "छोले"),
    "mung": ("moong", "green gram", "मूंग"),
    "urad": ("black gram", "उड़द"),
    "maize": ("makka", "makki", "bhutta", "corn", "sweet corn", "मक्का", "भुट्टा"),
    "millet": ("bajra", "jowar", "ragi", "बाजरा", "ज्वार"),
    "mustard": ("sarson", "sarso", "rai", "सरसों"),
    "sesame": ("til", "gingelly", "तिल"),
    "groundnut": ("moongfali", "mungfali", "peanut", "मूंगफली"),
    "soybean": ("soyabean", "soya", "soy", "सोयाबीन"),
    "flour": ("atta", "aata", "maida", "आटा", "मैदा"),
    # beauty & personal care
    "hair spray": ("hairspray", "hair sprey", "hair mist", "setting spray"),
    "hair color": ("hair dye", "hair colour", "hair tint", "hair bleach", "hair paint"),
    "shampoo": ("hair wash", "hair cleanser"),
    # spices
    "turmeric": ("haldi", "haldee", "हल्दी"),
    "cumin": ("jeera", "jira", "zeera", "जीरा"),
    "cardamom": ("elaichi", "ilaichi", "elaychi", "इलायची"),
    "clove": ("laung", "long", "लौंग"),
    "black pepper": ("kali mirch", "काली मिर्च"),
    # commodities
    "sugar": ("chini", "cheeni", "shakkar", "चीनी", "शक्कर"),
    "jaggery": ("gud", "gur", "gurh", "गुड़"),
    "salt": ("namak", "नमक"),
    "cashew": ("kaju", "काजू"),
    "almond": ("badam", "baadam", "बादाम"),
    "milk": ("doodh", "dudh", "दूध"),
    "cotton": ("kapas", "kapaas", "rui", "कपास", "रुई"),
    "silk": ("resham", "रेशम"),
    "wool": ("oon", "ऊन"),
    "iron": ("loha", "lohe", "लोहा"),
    "copper": ("tamba", "taamba", "तांबा"),
    "brass": ("peetal", "pital", "पीतल"),
    "wood": ("lakdi", "lakadi", "lakri", "लकड़ी"),
    "paper": ("kagaz", "kaagaz", "kagad", "कागज"),
    "fabric": ("kapda", "kapada", "kapra", "कपड़ा"),
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
_WORD_RE = re.compile(r"[a-z\u0900-\u097f]+", re.IGNORECASE)


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

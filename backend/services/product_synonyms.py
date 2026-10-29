"""Bilingual product vocabulary: local trade names mapped onto one English term.

Indian buyers and sellers write the same commodity several ways -- "pyaz",
"pyaaz", "kanda" and "onion" are one product. Without a shared table a buyer
asking for "pyaz" never sees the "onion" seller, because neither lexical overlap
nor the distinct-product gate knows the two words mean the same thing.

The table is deliberately small and high-precision: only words that are
unambiguous in a trade context. It is the VERIFIED seed: ``ai_lexicon`` adds
open-world names learned from the local model on top (remembered on disk), but
where the two overlap this table wins, and it is the whole vocabulary when the
model is offline. Canonical terms are singular English, in the
form ``match_scoring.singularise`` produces, so they compare directly against
tokenised listing text.

This module must not import ``match_scoring`` (which imports it).
"""

import re
from functools import lru_cache
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
    "chickpea": ("chana", "channa", "chole", "bengal gram", "चना", "छोले"),
    "mung": ("moong", "green gram", "मूंग"),
    "urad": ("black gram", "उड़द"),
    "maize": ("makka", "makki", "bhutta", "corn", "मक्का", "भुट्टा"),
    # distinct crops: pearl millet, sorghum and finger millet are not one product
    "pearl millet": ("bajra", "बाजरा"),
    "sorghum": ("jowar", "ज्वार"),
    "finger millet": ("ragi",),
    "mustard": ("sarson", "sarso", "rai", "सरसों"),
    "sesame": ("til", "gingelly", "तिल"),
    "groundnut": ("moongfali", "mungfali", "peanut", "मूंगफली"),
    "soybean": ("soyabean", "soya", "soy", "सोयाबीन"),
    # whole-wheat and refined flour are different products
    "whole wheat flour": ("atta", "aata", "आटा"),
    "refined wheat flour": ("maida", "मैदा"),
    # beauty & personal care
    "hair spray": ("hairspray", "hair sprey", "hair mist", "setting spray"),
    "hair color": ("hair dye", "hair colour", "hair tint", "hair paint"),
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
    # metals & industrial materials
    "steel": ("istil", "faulaad", "faulad", "स्टील"),
    "stainless steel": ("stainless", "ss"),
    "aluminium": ("aluminum", "almunium", "almuniyam", "एल्युमिनियम"),
    "plastic": ("plastik", "plaastic", "प्लास्टिक"),
    # packaging, bags & containers
    "box": ("carton", "cartons", "corrugated box", "corrugated carton", "cardboard box", "packing box", "shipping box", "petti", "pettis", "पेटी", "कार्टन", "खोखा"),
    "bag": ("bora", "boras", "bori", "boris", "katta", "kattas", "gunny bag", "gunny bags", "pp bag", "pp bags", "jute bag", "poly bag", "polybag", "polybags", "sack", "sacks", "बोरी", "बोरा", "कट्टा", "थैला"),
    # construction & industrial steel
    "rebar": ("sariya", "saria", "tmt bar", "tmt bars", "tmt rebar", "tmt rebars", "construction rod", "steel rod", "iron rod", "reinforcement bar", "सरिया"),
    "pipe": ("pipes", "tube", "tubes", "tubing", "conduit", "पाइप"),
    "wire": ("wires", "तार"),
    "cable": ("cables", "cord", "cords", "केबल"),
    # electronics, apparel, footwear, agriculture
    "smartphone": ("mobile", "mobiles", "cellphone", "cell phone", "phone", "handset", "smart phone", "मोबाइल"),
    # a synonym group holds ONE product: no generic words (garment, apparel) and no sibling products
    "shirt": ("शर्ट",),
    "t-shirt": ("t shirt", "tshirt", "tee", "tees"),
    "kurta": ("कुर्ता",),
    "shoes": ("shoe", "जूते", "जूता"),
    "sneakers": ("sneaker",),
    "boots": ("boot",),
    "sandals": ("sandal", "chappal"),
    "fertilizer": ("fertiliser", "khad", "खाद", "उर्वरक"),
    "oil": ("tel", "edible oil", "cooking oil", "vegetable oil", "तेल"),
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
    w = word.lower()
    if w in ALIASES:
        return ALIASES[w]
    try:
        from services import learned_vocabulary
        learned = learned_vocabulary.canonical_product_for(w)
        if learned:
            return learned
    except Exception:
        pass
    try:
        from services import ai_lexicon
        return ai_lexicon.canonical(w)
    except Exception:
        return None


# Hindi / trade names of commodities (chawal -> rice, tamatar -> tomato, pyaz -> onion, aloo -> potato, haldi -> turmeric,
# jeera -> cumin, sarson -> mustard, gehun -> wheat, chana -> chickpea, cheeni -> sugar, ...) that are safe to rewrite in
# any text, e.g. before an HSN lookup: taken from the vocabulary above, minus words that are also ordinary English.
_HSN_LEXICON_GROUPS = (
    "onion", "potato", "tomato", "garlic", "ginger", "brinjal", "okra", "cauliflower", "pea", "coriander", "lemon",
    "banana", "apple", "grape", "rice", "wheat", "lentil", "chickpea", "mung", "maize", "pearl millet", "sorghum",
    "finger millet", "mustard", "sesame", "groundnut", "soybean", "whole wheat flour", "refined wheat flour", "turmeric", "cumin", "cardamom", "clove", "sugar", "jaggery", "salt",
    "cashew", "almond", "milk", "cotton",
)
_ENGLISH_COLLISIONS = frozenset({
    "gram", "corn", "soy", "long", "rai", "til", "rui", "oon", "gud", "gur", "seb", "kela", "kele", "dal",
    "chili", "chilly", "eggplant", "aubergine", "ladyfinger", "peanut", "potatoe", "tomatoe", "sweet corn",
})
HINDI_TRADE_NAMES: dict[str, str] = {
    alias.lower(): canonical
    for canonical in _HSN_LEXICON_GROUPS
    for alias in _GROUPS.get(canonical, ())
    if alias.lower() not in _NEVER_REWRITE and alias.lower() not in _ENGLISH_COLLISIONS and " " not in alias
}
HINDI_TRADE_NAMES.update({"chini": "sugar", "dal": "lentil", "daal": "lentil"})
_HINDI_WORD_RE = re.compile(r"[a-z\u0900-\u097f]+", re.IGNORECASE)


def hindi_trade_names_to_english(text: Optional[str]) -> str:
    """Rewrite Hindi trade names in ``text`` to their English commodity ("10 tonne chawal" -> "10 tonne rice")."""
    if not text:
        return ""
    return _HINDI_WORD_RE.sub(lambda m: HINDI_TRADE_NAMES.get(m.group(0).lower(), m.group(0)), text)


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
    return _WORD_RE.sub(lambda m: canonical_token(m.group(0)) or m.group(0), lowered)


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
    try:
        from services import ai_lexicon
        found |= ai_lexicon.aliases(term)
        found |= ai_lexicon.aliases(canonical)
    except Exception:
        pass
    return found


def expand_terms(terms: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for term in terms:
        out |= variants(term)
    return out


# ---------------------------------------------------------------------------
# Enhancement #1: Phonetic + Fuzzy Typo Recovery
#
# A lightweight Soundex variant + Jaro-Winkler similarity check against all
# canonical product families, known aliases, and learned vocabulary.
# Resolves: "tamoto" → "tomato", "poteto" → "potato", "stainles" → "stainless",
# "alumnium" → "aluminium" in < 2ms without static typo dictionaries.
# ---------------------------------------------------------------------------

def _soundex(word: str) -> str:
    """Simplified Soundex encoding tuned for trade product names.

    Maps consonants to numeric codes (vowels are ignored after the first letter),
    producing a 4-character phonetic fingerprint. ``_soundex("tamoto") ==
    _soundex("tomato")`` because the consonant skeleton T-M-T is the same.
    """
    if not word:
        return ""
    w = word.lower()
    # Phonetic map: B/F/P/V=1, C/G/J/K/Q/S/X/Z=2, D/T=3, L=4, M/N=5, R=6
    _MAP = {
        'b': '1', 'f': '1', 'p': '1', 'v': '1',
        'c': '2', 'g': '2', 'j': '2', 'k': '2', 'q': '2', 's': '2', 'x': '2', 'z': '2',
        'd': '3', 't': '3',
        'l': '4',
        'm': '5', 'n': '5',
        'r': '6',
    }
    codes = [w[0].upper()]
    prev = _MAP.get(w[0], '0')
    for ch in w[1:]:
        code = _MAP.get(ch, '0')
        if code != '0' and code != prev:
            codes.append(code)
        prev = code if code != '0' else prev
    return ''.join(codes)[:4].ljust(4, '0')


def _jaro_winkler(s1: str, s2: str) -> float:
    """Jaro-Winkler similarity (0-1). Tuned for short product names."""
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0
    match_dist = max(len1, len2) // 2 - 1
    if match_dist < 0:
        match_dist = 0
    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0
    transpositions = 0
    for i in range(len1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    jaro = (matches / len1 + matches / len2 + (matches - transpositions / 2) / matches) / 3
    # Winkler bonus for shared prefix (up to 4 chars)
    prefix = 0
    for i in range(min(4, len1, len2)):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break
    return jaro + prefix * 0.1 * (1 - jaro)


# Pre-built phonetic index: soundex_code -> list of (canonical_term, alias)
_PHONETIC_INDEX: dict[str, list[tuple[str, str]]] = {}


def _build_phonetic_index() -> None:
    """Build once at import time. All canonical terms + aliases indexed by Soundex."""
    if _PHONETIC_INDEX:
        return
    for canonical, aliases in _GROUPS.items():
        code = _soundex(canonical)
        if code not in _PHONETIC_INDEX:
            _PHONETIC_INDEX[code] = []
        _PHONETIC_INDEX[code].append((canonical, canonical))
        for alias in aliases:
            if ' ' not in alias and all(ord(c) < 128 for c in alias):
                a_code = _soundex(alias)
                if a_code not in _PHONETIC_INDEX:
                    _PHONETIC_INDEX[a_code] = []
                _PHONETIC_INDEX[a_code].append((canonical, alias))


_build_phonetic_index()


@lru_cache(maxsize=4096)
def fuzzy_match(word: str, threshold: float = 0.86) -> Optional[str]:
    """Find the canonical English product term for a misspelled word.

    Uses a two-phase approach:
    1. Soundex: if the phonetic fingerprint matches a known term, verify
       with Jaro-Winkler (>= threshold) to cut false positives.
    2. Brute-force Jaro-Winkler against canonical terms if Soundex misses (>= 0.89).

    Returns the canonical term or None. Runs in < 1ms on ~80 product families.
    """
    if not word or len(word) < 3:
        return None
    w = word.lower()
    # Common English nouns that must never be fuzzy-matched to something else
    if w in {"cable", "cables", "box", "boxes", "pipe", "pipes", "wire", "wires", "sheet", "sheets"}:
        return None
    # Already known: skip fuzzy
    if w in ALIASES:
        return ALIASES[w]
    if w in _GROUPS:
        return w

    best_score = 0.0
    best_canonical: Optional[str] = None

    # Phase 1: Soundex-guided search
    code = _soundex(w)
    candidates = _PHONETIC_INDEX.get(code, [])
    for canonical, alias in candidates:
        sim = _jaro_winkler(w, alias)
        if sim > best_score:
            best_score = sim
            best_canonical = canonical
        # Also check against the canonical term itself
        sim2 = _jaro_winkler(w, canonical)
        if sim2 > best_score:
            best_score = sim2
            best_canonical = canonical

    # Phase 2: Brute-force against all canonical terms (only ~80) with higher strictness
    phase2_threshold = max(threshold, 0.89)
    if best_score < phase2_threshold:
        for canonical in _GROUPS:
            sim = _jaro_winkler(w, canonical)
            if sim > best_score and sim >= phase2_threshold:
                best_score = sim
                best_canonical = canonical

    # Phase 3: Check learned vocabulary product mappings
    if best_score < threshold:
        try:
            from services import learned_vocabulary
            learned_syns = learned_vocabulary.learned_product_synonyms()
            for raw_term, canon in learned_syns.items():
                sim = _jaro_winkler(w, raw_term)
                if sim > best_score:
                    best_score = sim
                    best_canonical = canon
                sim2 = _jaro_winkler(w, canon)
                if sim2 > best_score:
                    best_score = sim2
                    best_canonical = canon
            from services import ai_lexicon
            for name_key in ai_lexicon.known_names():
                if ' ' in name_key:
                    continue
                sim = _jaro_winkler(w, name_key)
                if sim > best_score:
                    best_score = sim
                    best_canonical = ai_lexicon.canonical(name_key) or name_key
            learned_products = learned_vocabulary._load().get("product_category", {})
            for term in learned_products:
                sim = _jaro_winkler(w, term)
                if sim > best_score:
                    best_score = sim
                    best_canonical = term
        except Exception:
            pass

    if best_score >= threshold and best_canonical is not None:
        return best_canonical
    return None

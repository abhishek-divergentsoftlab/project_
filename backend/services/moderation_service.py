"""AI Content Moderation Service: detects and blocks prohibited and illegal items.

Protects the marketplace against trading of:
1. Child exploitation, minors, underage abuse, and child trafficking
2. Firearms, weapons, guns, explosives, firearm parts/barrels, and ammunition
3. Adult human trafficking, forced labor, slaves, and human organs/tissues
4. Illicit narcotics, fentanyl, cocaine, and controlled substances
5. Endangered wildlife, ivory, and animal body parts
6. WMDs, bio-weapons, and weaponized hazardous materials

Includes advanced normalization against character spacing, leetspeak, invisible characters,
and punctuation-based filter evasions.
"""

import json
import logging
import re
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from models.moderation import ModerationLog
from schemas.moderation import ModerationCheckResult
from services import answer_verifier

logger = logging.getLogger(__name__)

# Safe industrial compound phrases containing "gun" that should NOT be blocked
INDUSTRIAL_GUN_EXEMPTIONS = {
    "glue gun",
    "heat gun",
    "caulking gun",
    "grease gun",
    "staple gun",
    "spray gun",
    "paint gun",
    "barcode gun",
    "water gun",
    "price tag gun",
    "massage gun",
    "radar gun",
    "rivet gun",
    "nail gun",
    "air blow gun",
    "thermometer gun",
    "stud gun",
    "tufting gun",
    "soldering gun",
    "hot air gun",
    "tattoo gun",
    "label gun",
    "foam gun",
    "sealant gun",
}

# Legitimate goods whose names contain a weapon word. Matched on word
# boundaries and removed before the prohibited patterns run, so "gun metal
# grey coating" passes while "gun metal grey rifles" is still caught by
# "rifles". Keep every entry specific: a broad exemption is an evasion route.
INDUSTRIAL_EXEMPTION_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:"
        + "|".join(sorted((re.escape(p).replace(r"\ ", r"\s+") for p in INDUSTRIAL_GUN_EXEMPTIONS), key=len, reverse=True))
        + r")s?\b"
    ),
    re.compile(r"\bgun[\s\-]*metal(?:[\s\-]+(?:grey|gray|finish|colou?r|black|powder|paint|coating|shade))?\b"),
    re.compile(r"\bshotgun[\s\-]+(?:mics?|microphones?)\b"),
    re.compile(r"\bbomb[\s\-]+calorimet(?:er|ers|ry)\b"),
    re.compile(r"\bbath[\s\-]+bombs?\b"),
]

# Longer inputs are truncated before matching. The request schema already caps
# title + description + category at ~5.6k characters; this bounds the work for
# any other caller.
MAX_MODERATION_CHARS = 12_000

# Bounded repetition for filler words ("buy a young fresh ..."). An unbounded
# `(?:word\s+)*` next to another one backtracks quadratically on "a a a a ...".
_FILLER_MAX = "{0,4}"

# Negative lookahead for legitimate juvenile & adult apparel, toys, and commercial merchandise
PRODUCT_SUFFIX = (
    r"(?:\s+(?:clothes|clothing|t-?shirts?|shirts?|shoes?|pants?|toys?|apparel|wear|garments?|"
    r"footwear|uniforms?|products?|items?|gear|accessories|bottles?|cribs?|strollers?|diapers?|"
    r"dresses?|jackets?|sweaters?|hoodies?|pajamas?|pyjamas?|underwear|socks|books?|supplies|"
    r"fashion|size|costumes?|sports?\s+gear|salon|care|parlor|cosmetics?|handbags?|bags?|"
    r"kurti|kurtis|sarees?|salwar|suits?|tops?|jeans|skirts?|sandals?|heels?|jewellery|jewelry))\b"
)

# Categories of prohibited / illegal items with regex patterns (ordered by severity)
PROHIBITED_CATEGORIES: dict[str, list[re.Pattern]] = {
    "child_exploitation_and_minors": [
        # Explicit under-18 minor designations (e.g., 'under 18 yo girl', 'under 18 female', 'under-18 escort')
        re.compile(
            r"\b(?:under|below|sub|less\s+than|<)[\s\-_]*(?:18|eighteen)"
            r"[\s\-_]*(?:years?[\s\-_]*old|yrs?[\s\-_]*old|yo|years?|yrs?|yr|age)?"
            r"[\s\-_]*(?:girls?|boys?|kids?|child|children|minors?|teens?|teenagers?|infants?|babies|baby|toddlers?|females?|males?|daughters?|sons?|schoolgirls?|schoolboys?|virgins?|escorts?|models?|women|woman)"
            r"(?!"
            + PRODUCT_SUFFIX
            + r")\b",
            re.IGNORECASE,
        ),
        # Underage age (1-17) combined with minor nouns (girls, boys, kids, child, teen)
        re.compile(
            r"\b(?:(?:under|below|sub|less\s+than|<)[\s\-_]*)?"
            r"(?:1[0-7]|[1-9]|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen)"
            r"[\s\-_]*(?:years?[\s\-_]*old|yrs?[\s\-_]*old|yo|years?|yrs?|yr|age)?"
            r"[\s\-_]*(?:girls?|boys?|kids?|child|children|minors?|teens?|teenagers?|infants?|babies|baby|toddlers?|females?|males?|daughters?|sons?|schoolgirls?|schoolboys?|virgins?|escorts?|models?|women|woman)(?!"
            + PRODUCT_SUFFIX
            + r")\b",
            re.IGNORECASE,
        ),
        # Commercial exchange targeting minors or youth (e.g., 'order under 18 yo girl', 'buy 15 yo girl', 'hire escort girl')
        re.compile(
            r"\b(?:buy|sell|rent|hire|auction|deliver|trade|order|provide|export|import|purchase|book|supply)\s+"
            r"(?:(?:a|an|the|some|any|young|fresh|virgin|local|foreign|asian|russian|indian|cheap|underage|minor|sub)\s+)"
            + _FILLER_MAX
            + r""
            r"(?:(?:under|below|<)[\s\-_]*(?:1[0-8]|[1-9])[\s\-_]*)?"
            r"(?:(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen)"
            r"[\s\-_]*(?:years?[\s\-_]*old|yrs?[\s\-_]*old|yo|years?|yrs?|yr)[\s\-_]*)?"
            r"(?:girls?|boys?|kids?|child|children|minors?|babies|baby|infants?|toddlers?|daughters?|sons?|virgins?|schoolgirls?|schoolboys?|escorts?|prostitut\w*)"
            r"(?!"
            + PRODUCT_SUFFIX
            + r")\b",
            re.IGNORECASE,
        ),
        # Explicit child exploitation, minor prostitution, abuse & trafficking terms
        re.compile(
            r"\b(underage\s+(?:girls?|boys?|minors?|children|child|kids?|teens?|teenagers?|females?|males?|escorts?|prostitut\w*|sex|porn\w*|models?)|"
            r"minor\s+(?:girls?|boys?|children|child|escorts?|prostitut\w*|sex|brides?|for\s+sale)|"
            r"child\s+(?:prostitut\w*|porn\w*|traffick\w*|abuse|brides?|slaves?|slavery|labor\s+contract|for\s+sale|sold)|"
            r"csam|pedophil\w*|paedophil\w*|jailbait|teen\s+(?:escorts?|prostitut\w*|virgins?\s+for\s+sale))\b",
            re.IGNORECASE,
        ),
    ],
    "firearms_and_weapons": [
        re.compile(
            r"\b(gun|guns|handgun|handguns|pistol|pistols|revolver|revolvers|rifle|rifles|"
            r"shotgun|shotguns|firearm|firearms|carbine|carbines|uzi|uzis)\b",
            re.I,
        ),
        re.compile(r"\b(glock|ak-?47|ar-?15|m16|m4a1|kalashnikov|submachine\s+gun|assault\s+rifle|sniper\s+rifle)\b", re.I),
        re.compile(r"\b(ammo|ammunition|bullets|silencer|suppressor|grenade|grenades|rpg|bazooka|rocket\s+launcher)\b", re.I),
        re.compile(r"\b(landmine|military\s+explosive|c4\s+explosive|dynamite|bomb|bombs|pipe\s+bomb|buckshot|birdshot)\b", re.I),
        # Weapon parts, gun barrels, receivers, switches, sears
        re.compile(
            r"\b((?:gun|rifle|pistol|shotgun|firearm|carbine|sniper|threaded)\s+barrels?|"
            r"barrels?\s+(?:for|of)\s+(?:gun|guns|rifle|rifles|pistol|pistols|ak-?47|ar-?15|glock|firearm|firearms))\b",
            re.I,
        ),
        re.compile(r"\b((?:upper|lower)\s+receivers?|80%\s+lower|glock\s+switch|auto\s+sear|bump\s+stock|firing\s+pin)\b", re.I),
        re.compile(
            # "9mm" alone or with a firearm word; "9mm plywood" / "9 mm rebar" are goods.
            r"\b(9\s?mm(?=\s*(?:\Z|ammo|ammunition|luger|parabellum|rounds?\b|bullets?|pistols?|handguns?|"
            r"guns?\b|cartridges?|caliber|calibre|glock))(?:\s*(?:ammo|luger|parabellum|rounds?|bullets?))?|"
            r"5\.56(?:\s*mm)?(?:\s*(?:ammo|nato|rounds?|bullets?|x\s*45|caliber|cal))?|"
            r"7\.62(?:\s*mm)?(?:\s*(?:ammo|nato|rounds?|bullets?|x\s*39|x\s*51|caliber|cal))?|"
            r"\.?223(?:\s*(?:rem|remington|ammo|rounds?|bullets?))|"
            r"\.?308(?:\s*(?:win|winchester|ammo|rounds?|bullets?))|"
            r"\.50\s*(?:cal|caliber|bmg|ammo|rounds?)|"
            r"\.45\s*(?:acp|ammo|rounds?|bullets?)|"
            r"(?:12|20)\s*gauge\s*(?:shells?|ammo|slugs?|rounds?)|"
            r"hollow\s+points?(?:\s+bullets?|\s+ammo)?)\b",
            re.I,
        ),
    ],
    "human_trafficking_and_organs": [
        re.compile(r"\b(human\s+trafficking|buy\s+human|sell\s+human|slaves?|slavery)\b", re.I),
        re.compile(r"\b(human\s+organ|human\s+organs|sell\s+kidney|buy\s+kidney|sell\s+liver|buy\s+liver)\b", re.I),
        re.compile(r"\b(human\s+tissue|human\s+fetus|human\s+bone|human\s+skeleton|human\s+blood\s+for\s+sale)\b", re.I),
        re.compile(r"\b(mail\s+order\s+bride|sex\s+trafficking|forced\s+labor)\b", re.I),
        # Commercial supply, sale, purchase, or trade of human beings (e.g. 'i suppy 67 yo leddy', 'buy 30 yo woman', 'sell lady')
        re.compile(
            r"\b(?:buy|buys|buying|sell|sells|selling|rent|rents|renting|hire|hires|hiring|auction|auctioning|"
            r"deliver|delivering|trade|trading|order|orders|ordering|provide|provides|providing|export|exporting|"
            r"import|importing|purchase|purchasing|book|booking|supply|supplies|supplying|suppy|suplly|need|needs|"
            r"want|wants|looking\s+for)\s+"
            r"(?:(?:a|an|the|some|any|young|old|fresh|virgin|local|foreign|asian|russian|indian|cheap|domestic|house|beautiful|single|mature|elderly)\s+)"
            + _FILLER_MAX
            + r""
            r"(?:(?:\d{1,3})\s*(?:years?[\s\-_]*old|yrs?[\s\-_]*old|yo|years?|yrs?|yr)[\s\-_]*)?"
            r"(?:(?:a|an|the|some|any|young|old|fresh|virgin|local|foreign|asian|russian|indian|cheap|domestic|house|beautiful|single|mature|elderly)\s+)"
            + _FILLER_MAX
            + r""
            r"(?:wom[ae]n|lad(?:y|ies)|ledd?y|females?|m[ae]n|males?|girls?|boys?|humans?|persons?|people|maids?|escorts?|wives|wife|brides?|slaves?|concubines?)"
            r"(?!"
            + PRODUCT_SUFFIX
            + r")\b",
            re.IGNORECASE,
        ),
        # Standalone age + human noun without legitimate merchandise context (e.g. '67 yo leddy', '67 yo lady')
        re.compile(
            r"\b(?:\d{1,3})\s*(?:years?[\s\-_]*old|yrs?[\s\-_]*old|yo|years?|yrs?|yr)[\s\-_]*"
            r"(?:wom[ae]n|lad(?:y|ies)|ledd?y|females?|m[ae]n|males?|girls?|boys?|humans?|persons?|people|maids?|escorts?|wives|wife|brides?|slaves?)"
            r"(?!"
            + PRODUCT_SUFFIX
            + r")\b",
            re.IGNORECASE,
        ),
    ],
    "narcotics_and_controlled_substances": [
        re.compile(r"\b(cocaine|heroin|methamphetamine|crystal\s+meth|fentanyl|lsd|mdma|ecstasy)\b", re.I),
        re.compile(r"\b(crack\s+cocaine|opium|morphine\s+powder|illicit\s+narcotic|drug\s+cartel)\b", re.I),
        re.compile(r"\b(synthetic\s+opioid|psilocybin\s+mushrooms|ketamine\s+illicit)\b", re.I),
    ],
    "endangered_wildlife": [
        re.compile(r"\b(elephant\s+ivory|raw\s+ivory|tiger\s+bone|tiger\s+skin|rhino\s+horn)\b", re.I),
        re.compile(r"\b(pangolin\s+scales|sea\s+turtle\s+shell|chimpanzee\s+meat|bear\s+bile)\b", re.I),
    ],
    "hazardous_and_wmd": [
        re.compile(r"\b(enriched\s+uranium|plutonium-?239|dirty\s+bomb|weaponized\s+anthrax)\b", re.I),
        re.compile(r"\b(sarin\s+gas|vx\s+nerve\s+agent|ricin\s+toxin|chemical\s+weapon)\b", re.I),
    ],
}

# Text that talks to the platform's AI instead of describing goods, or moves payment off the platform. The assistant
# reads listing titles and descriptions as data, but a listing like this is held anyway: it only exists to mislead.
INJECTION_CATEGORY = "ai_instructions_or_offplatform_payment"
_INJECTION_PATTERNS = [
    re.compile(r"\bnotes?\s+(?:to|for)\s+(?:the\s+)?(?:ai|a\.i\.|assistant|llm|chat\s?bot|bot|model)\b", re.I),
    re.compile(r"\b(?:assistant|chatbot)\s*:", re.I),
    re.compile(r"\bignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|your)?\s*(?:instructions?|prompts?|rules)\b", re.I),
    re.compile(r"\bdisregard\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|your)\s+(?:instructions?|prompts?|rules)\b", re.I),
    re.compile(r"\bsystem\s+prompt\b", re.I),
    re.compile(r"\b(?:tell|instruct|advise|ask)\s+(?:the\s+)?(?:buyers?|users?|customers?|clients?)\s+to\s+pay\b", re.I),
    re.compile(r"\balways\s+(?:tell|say|reply|answer|recommend)\b[^.\n]{0,40}\b(?:buyers?|users?|pay)\b", re.I),
]

CATEGORY_DESCRIPTIONS = {
    INJECTION_CATEGORY: "Instructions to the platform's AI assistant or off-platform payment instructions",
    "child_exploitation_and_minors": "Child Sexual Exploitation, Underage Abuse & Minor Trafficking",
    "firearms_and_weapons": "Firearms, Weapons, Ammunition & Explosives",
    "human_trafficking_and_organs": "Human Trafficking, Slavery & Human Body Parts/Organs",
    "narcotics_and_controlled_substances": "Illicit Narcotics & Controlled Substances",
    "endangered_wildlife": "Endangered Wildlife, Ivory & Illegal Animal Parts",
    "hazardous_and_wmd": "Hazardous Contraband, Chemical Weapons & WMDs",
}


# Returned (is_safe=True) by check_text_async when the AI classifier could not run: the listing is allowed but
# audit_and_verify records it, unblocked, in the moderation log so ops can review it.
REVIEW_CATEGORY = "needs_review_ai_unavailable"

_AI_CATEGORIES = tuple(k for k in CATEGORY_DESCRIPTIONS if k != INJECTION_CATEGORY) + ("other_illegal_goods",)
CATEGORY_DESCRIPTIONS.setdefault("other_illegal_goods", "Other Illegal, Counterfeit or Stolen Goods")
_AI_TIMEOUT = 25.0
_VERDICT_TTL_MISS = 120.0
_MAX_CACHED = 5000
_VERDICT_FILE = Path("data/moderation_verdicts.json")
_verdicts: dict[str, dict] = {}
_verdicts_loaded = False


def _verdict_key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").lower().split())[:MAX_MODERATION_CHARS]


def _load_verdicts() -> None:
    global _verdicts_loaded
    if _verdicts_loaded:
        return
    _verdicts_loaded = True
    try:
        _verdicts.update(json.loads(_VERDICT_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def _save_verdicts() -> None:
    try:
        _VERDICT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _VERDICT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_verdicts, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_VERDICT_FILE)
    except OSError as exc:
        logger.debug("moderation verdict cache not saved: %s", exc)


def _moderation_prompt(text: str) -> str:
    return (
        "You are the safety reviewer of a B2B trade marketplace (industrial, agricultural, consumer and "
        "commercial goods; users in India and worldwide). Decide whether this listing text offers or requests "
        "goods or services that are ILLEGAL or banned to trade: narcotics and controlled substances "
        "(cannabis, ganja, marijuana, charas, opium, heroin, cocaine, MDMA, ...), firearms, ammunition, "
        "explosives and weapon parts, human trafficking or human organs, child exploitation, endangered wildlife "
        "or ivory, chemical/biological/nuclear weapons material, counterfeit currency or documents, stolen goods.\n"
        "Legal commodities are ALLOWED even when the word looks alarming (glue gun, gun metal grey paint, "
        "industrial hemp fibre, kitchen knife, pesticide, bleach, licensed pharmaceuticals, tobacco leaf, "
        "fireworks packaging). When it is genuinely ambiguous, answer allowed.\n"
        f"Allowed prohibited-categories: {json.dumps(list(_AI_CATEGORIES))}\n"
        f"Listing text: {json.dumps(text[:1500], ensure_ascii=False)}\n"
        'Reply as JSON: {"verdict": "allowed" | "prohibited", "category": "<category or empty>", '
        '"flagged": "<the offending words or empty>", "reason": "<one short sentence>"}'
    )


async def _ai_verdict(text: str) -> Optional[dict]:
    """The model's verdict for ``text`` (cached by normalized text), or None when the AI is unavailable."""
    from services import llm_client

    key = _verdict_key(text)
    if not key:
        return {"verdict": "allowed"}
    _load_verdicts()
    hit = _verdicts.get(key)
    if hit is not None:
        return hit
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["allowed", "prohibited"]},
            "category": {"type": "string"},
            "flagged": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["verdict"],
    }
    data = await llm_client.generate_json(_moderation_prompt(text), schema, timeout=_AI_TIMEOUT)
    if not isinstance(data, dict) or data.get("verdict") not in ("allowed", "prohibited"):
        return None
    verdict = {
        "verdict": data["verdict"],
        "category": str(data.get("category") or "")[:64],
        "flagged": str(data.get("flagged") or "")[:80],
        "reason": str(data.get("reason") or "")[:200],
    }
    if len(_verdicts) > _MAX_CACHED:
        _verdicts.clear()
    _verdicts[key] = verdict
    _save_verdicts()
    return verdict


class AIContentModerator:
    @classmethod
    def _normalize_text(cls, text: str) -> list[str]:
        """Generates normalized variants of input text to catch obfuscated bypass attempts.

        Handles:
        - Unicode NFKD decomposition (full-width characters, circled letters)
        - Stripping zero-width, invisible, and format control characters
        - Normalizing weapon model spacing (e.g. 'ak4 7', 'a k 4 7', 'ar 1 5', 'm 1 6')
        - De-spacing single-character sequences (e.g. 'g u n' -> 'gun', 'b o m b' -> 'bomb')
        - Basic leetspeak canonicalization (e.g. '@' -> 'a', '$' -> 's', 'b0mb' -> 'bomb')
        """
        # 1. Unicode decomposition
        decomposed = unicodedata.normalize("NFKD", text)

        # 2. Strip zero-width, soft hyphens, and invisible formatting
        clean_text = re.sub(r"[\u200b-\u200f\ufeff\u00ad\u2060\u180e]", "", decomposed)
        lowered = clean_text.lower()

        # 3. Canonicalize weapon models with internal spacing or punctuation
        t_weapons = lowered
        t_weapons = re.sub(r"\ba[\s\-_.]*k[\s\-_.]*(?:47|74|12|103|4[\s\-_.]*7|7[\s\-_.]*4)\b", "ak47", t_weapons)
        t_weapons = re.sub(r"\ba[\s\-_.]*r[\s\-_.]*(?:15|10|1[\s\-_.]*5|1[\s\-_.]*0)\b", "ar15", t_weapons)
        t_weapons = re.sub(r"\bm[\s\-_.]*1[\s\-_.]*6\b", "m16", t_weapons)
        t_weapons = re.sub(r"\bm[\s\-_.]*4[\s\-_.]*a[\s\-_.]*1\b", "m4a1", t_weapons)
        t_weapons = re.sub(r"\bg[\s\-_.]*l[\s\-_.]*o[\s\-_.]*c[\s\-_.]*k\b", "glock", t_weapons)
        t_weapons = re.sub(r"\bm[\s\-_.]*p[\s\-_.]*(?:5|7)\b", "mp5", t_weapons)
        t_weapons = re.sub(r"\br[\s\-_.]*p[\s\-_.]*g[\s\-_.]*(?:7)?\b", "rpg", t_weapons)

        # 4. Collapse single-character spaced tokens: "g u n" -> "gun", "1 3" -> "13"
        def _unspace(match: re.Match) -> str:
            return re.sub(r"[\s._-]+", "", match.group(0))

        t_unspaced = re.sub(r"\b(?:[a-z0-9][\s._-]+)+[a-z0-9]\b", _unspace, t_weapons)

        # 5. De-leetspeak substitutions
        t_deleet = t_unspaced.replace("@", "a").replace("$", "s").replace("!", "i").replace("+", "t")
        t_deleet = re.sub(r"(?<=[a-z])0(?=[a-z])", "o", t_deleet)
        t_deleet = re.sub(r"(?<=[a-z])3(?=[a-z])", "e", t_deleet)
        t_deleet = re.sub(r"(?<=[a-z])1(?=[a-z])", "i", t_deleet)
        t_deleet = re.sub(r"(?<=[a-z])5(?=[a-z])", "s", t_deleet)

        # Return unique variants to evaluate
        return list(dict.fromkeys([lowered, t_weapons, t_unspaced, t_deleet]))

    @classmethod
    def check_text(cls, text: str) -> ModerationCheckResult:
        """Analyzes text for prohibited items with normalization and industrial exemptions."""
        variants = cls._normalize_text((text or "")[:MAX_MODERATION_CHARS])

        # Remove exempt industrial phrases from all variants
        filtered_variants: list[str] = []
        for variant in variants:
            v_clean = variant
            for exemption in INDUSTRIAL_EXEMPTION_PATTERNS:
                v_clean = exemption.sub(" ", v_clean)
            filtered_variants.append(v_clean)

        flagged_categories: list[str] = []
        flagged_terms: list[str] = []

        for category, patterns in PROHIBITED_CATEGORIES.items():
            for pattern in patterns:
                for v in filtered_variants:
                    matches = pattern.findall(v)
                    if matches:
                        if category not in flagged_categories:
                            flagged_categories.append(category)
                        for m in matches:
                            term = m if isinstance(m, str) else m[0]
                            term_str = str(term).strip()
                            if term_str and term_str not in flagged_terms:
                                flagged_terms.append(term_str)

        if flagged_terms:
            primary_cat = flagged_categories[0]
            cat_desc = CATEGORY_DESCRIPTIONS.get(primary_cat, primary_cat)
            return ModerationCheckResult(
                is_safe=False,
                category=primary_cat,
                reason=(
                    f"Listing blocked by AI Safety Moderation: Content contains prohibited items "
                    f"under '{cat_desc}' (detected terms: {', '.join(flagged_terms[:3])}). "
                    f"Trading of these items is strictly illegal and banned on this platform."
                ),
                flagged_terms=flagged_terms,
            )

        held = cls._injection_or_offplatform(variants[0])
        if held:
            return ModerationCheckResult(
                is_safe=False,
                category=INJECTION_CATEGORY,
                reason=(
                    "Listing held by AI Safety Moderation: the text addresses the AI assistant or contains "
                    f"off-platform payment details ({', '.join(held[:3])}). Remove it and describe the goods only; "
                    "payments on this platform go through escrow."
                ),
                flagged_terms=held,
            )

        return ModerationCheckResult(
            is_safe=True,
            category=None,
            reason=None,
            flagged_terms=[],
        )

    @classmethod
    async def check_text_async(cls, text: str) -> ModerationCheckResult:
        """Regex pre-filter, then one cached AI classification of the listing text.

        A regex hit blocks at once (no model call). Otherwise the model decides allowed/prohibited. When the
        model is unavailable the text is allowed but returned with ``category == REVIEW_CATEGORY`` so the
        audit path logs it for ops review instead of pretending it was checked.
        """
        fast = cls.check_text(text)
        if not fast.is_safe:
            return fast
        clean = (text or "")[:MAX_MODERATION_CHARS]
        if not any(ch.isalnum() for ch in clean):
            return fast
        verdict = await _ai_verdict(clean)
        if verdict is None:
            return ModerationCheckResult(
                is_safe=True,
                category=REVIEW_CATEGORY,
                reason="AI moderation was unavailable; passed the keyword filter and is queued for review",
                flagged_terms=[],
            )
        if verdict["verdict"] == "prohibited":
            category = verdict.get("category") if verdict.get("category") in _AI_CATEGORIES else "other_illegal_goods"
            terms = [verdict["flagged"]] if verdict.get("flagged") else []
            desc = CATEGORY_DESCRIPTIONS.get(category, category)
            why = f" ({verdict['reason']})" if verdict.get("reason") else ""
            return ModerationCheckResult(
                is_safe=False,
                category=category,
                reason=(
                    f"Listing blocked by AI Safety Moderation: content appears to offer prohibited items under "
                    f"'{desc}'{why}. Trading of these items is illegal and banned on this platform."
                ),
                flagged_terms=terms,
            )
        return fast

    @classmethod
    def _injection_or_offplatform(cls, text: str) -> list[str]:
        """What in ``text`` talks to the AI or asks for payment outside the platform (empty when clean)."""
        found: list[str] = []
        for rx in _INJECTION_PATTERNS:
            m = rx.search(text)
            if m:
                found.append(m.group(0).strip()[:40])
        found += answer_verifier.unsafe_payment_issues(text, include_advance=False)
        return list(dict.fromkeys(found))

    @classmethod
    async def audit_and_verify(
        cls,
        db: AsyncSession,
        user_id: Optional[uuid.UUID],
        action: str,
        title: str,
        description: Optional[str] = None,
        category: Optional[str] = None,
    ) -> ModerationCheckResult:
        """Full check that logs any violations to the moderation audit table."""
        full_content = f"{title}\n{description or ''}\n{category or ''}"
        result = await cls.check_text_async(full_content)

        if result.category == REVIEW_CATEGORY:
            db.add(
                ModerationLog(
                    user_id=user_id,
                    action=action,
                    category=REVIEW_CATEGORY[:64],
                    flagged_terms="ai_unavailable",
                    snippet=(title[:100] + " | " + (description or "")[:100]).strip(),
                    blocked=False,
                )
            )
            await db.commit()
        elif not result.is_safe:
            log_entry = ModerationLog(
                user_id=user_id,
                action=action,
                category=result.category or "unknown",
                flagged_terms=", ".join(result.flagged_terms[:5]),
                snippet=(title[:100] + " | " + (description or "")[:100]).strip(),
                blocked=True,
            )
            db.add(log_entry)
            await db.commit()

        return result


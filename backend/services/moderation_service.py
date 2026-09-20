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

import re
import unicodedata
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from models.moderation import ModerationLog
from schemas.moderation import ModerationCheckResult

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
}

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
            r"(?:(?:a|an|the|some|any|young|fresh|virgin|local|foreign|asian|russian|indian|cheap|underage|minor|sub)\s+)*"
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
        re.compile(r"\b(gun|guns|pistol|pistols|revolver|revolvers|rifle|rifles|shotgun|shotguns|firearm|firearms)\b", re.I),
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
            r"\b(9mm(?:\s*(?:ammo|luger|parabellum|rounds?|bullets?))?|"
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
            r"(?:(?:a|an|the|some|any|young|old|fresh|virgin|local|foreign|asian|russian|indian|cheap|domestic|house|beautiful|single|mature|elderly)\s+)*"
            r"(?:(?:\d{1,3})\s*(?:years?[\s\-_]*old|yrs?[\s\-_]*old|yo|years?|yrs?|yr)[\s\-_]*)?"
            r"(?:(?:a|an|the|some|any|young|old|fresh|virgin|local|foreign|asian|russian|indian|cheap|domestic|house|beautiful|single|mature|elderly)\s+)*"
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

CATEGORY_DESCRIPTIONS = {
    "child_exploitation_and_minors": "Child Sexual Exploitation, Underage Abuse & Minor Trafficking",
    "firearms_and_weapons": "Firearms, Weapons, Ammunition & Explosives",
    "human_trafficking_and_organs": "Human Trafficking, Slavery & Human Body Parts/Organs",
    "narcotics_and_controlled_substances": "Illicit Narcotics & Controlled Substances",
    "endangered_wildlife": "Endangered Wildlife, Ivory & Illegal Animal Parts",
    "hazardous_and_wmd": "Hazardous Contraband, Chemical Weapons & WMDs",
}


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
        variants = cls._normalize_text(text)

        # Remove exempt industrial phrases from all variants
        filtered_variants: list[str] = []
        for variant in variants:
            v_clean = variant
            for industrial in INDUSTRIAL_GUN_EXEMPTIONS:
                if industrial in v_clean:
                    v_clean = v_clean.replace(industrial, " ")
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

        return ModerationCheckResult(
            is_safe=True,
            category=None,
            reason=None,
            flagged_terms=[],
        )

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
        result = cls.check_text(full_content)

        if not result.is_safe:
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


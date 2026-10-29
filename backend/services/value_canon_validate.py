"""Independent evidence for a canonical product term, before it is allowed into the permanent cache.

The model sees a bare, possibly regional word ("chini") and may answer from its SPELLING ("chili") when the context
(grade S-30, HSN chapter 17) says sugar. A wrong canonical cached globally would silently mismatch every future listing
with that word. So a canonical product is accepted only when it is consistent with evidence the model did not produce:

* the listing's tariff chapter (stored HSN, else the keyword prediction from its name/title) against the canonical
  term's predicted chapter: agreement confirms, a different chapter contradicts (``hsn_service`` is read-only here);
* a canonical that is just a near-spelling of the raw word ("chini" -> "chili") is suspicious: with context pointing
  elsewhere, or no way to confirm it against known context, it is rejected.

Pure functions (no model, no network): ``assess`` returns a confidence and the evidence it used, which are stored with
the cache entry so a wrong entry can be audited and purged (``value_canon.revalidate_cache``).
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Optional

ACCEPT = 0.60   # at or above: cached permanently and used for matching
WEAK = 0.35     # between WEAK and ACCEPT: weak signal, used for nothing, cached with a short TTL
DEFAULT_MODEL_CONFIDENCE = 0.70
ECHO_RATIO = 0.75


def _squash(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def spelling_echo(raw: Any, term: Any) -> bool:
    """True when ``term`` is a near-spelling of ``raw`` (but not the same word): "chini" -> "chili"."""
    a, b = _squash(raw), _squash(term)
    if not a or not b or a == b:
        return False
    if re.sub(r"(.)\1+", r"\1", a) == re.sub(r"(.)\1+", r"\1", b):
        return False  # chilli / chili: a spelling variant of the same word, not a different word that looks alike
    return difflib.SequenceMatcher(None, a, b).ratio() >= ECHO_RATIO


def _chapter(code: Optional[str]) -> Optional[str]:
    from services import hsn_service

    return hsn_service.get_chapter(code) if code else None


def predicted_hsn(text: Any) -> Optional[str]:
    from services import hsn_service

    text = str(text or "").strip()
    if not text:
        return None
    try:
        return hsn_service.predict_hsn_from_text(text)
    except Exception:  # noqa: BLE001
        return None


def listing_hsn(details: Optional[dict[str, Any]], raw_name: Any, title: Any = None) -> tuple[Optional[str], str]:
    """(HSN code, where it came from) for the listing: stored, else predicted from its name, else from its title."""
    from services import hsn_service

    stored = hsn_service.stored_hsn(details or {})
    if stored:
        return stored, "stored"
    for source, text in (("name", raw_name), ("title", title)):
        code = predicted_hsn(text)
        if code:
            return code, f"predicted from {source}"
    return None, ""


def chapter_hint(code: Optional[str]) -> str:
    """"17 (Sugars and sugar confectionery)" for the prompt, or ""."""
    from services import hsn_service

    ch = _chapter(code)
    if not ch:
        return ""
    desc = hsn_service.get_chapter_description(code) or ""
    return f"{ch} ({desc})" if desc else ch


def assess(
    raw: Any, term: Any, *, model_confidence: Optional[float] = None, details: Optional[dict[str, Any]] = None,
    title: Any = None,
) -> dict[str, Any]:
    """{"conf": 0..1, "verdict": "agree"|"conflict"|"unconfirmed"|"none", "echo": bool, "evidence": {...},
    "objection": str|None}. ``objection`` is set when the answer should be re-asked with the context made explicit."""
    try:
        base = float(model_confidence) if model_confidence is not None else DEFAULT_MODEL_CONFIDENCE
    except (TypeError, ValueError):
        base = DEFAULT_MODEL_CONFIDENCE
    base = min(0.95, max(0.05, base))
    echo = spelling_echo(raw, term)
    lcode, source = listing_hsn(details, raw, title)
    tcode = predicted_hsn(term)
    same_word = _squash(raw) == _squash(term)
    evidence: dict[str, Any] = {"listing_hsn": lcode, "listing_hsn_source": source, "term_hsn": tcode, "echo": echo}
    verdict, conf, objection = "none", base, None
    lch, tch = _chapter(lcode), _chapter(tcode)
    if same_word:
        verdict = "none"
    elif lch and tch:
        if lch == tch:
            verdict, conf = "agree", max(base, 0.85)
        elif not echo and source != "stored":
            # Two keyword predictions disagree about synonyms ("cotton cloth" 52 / "cotton fabric" 59): too noisy to
            # decide alone. Only a stored HSN, or a canonical that merely echoes the spelling, is decisive.
            verdict, conf = "soft_conflict", min(base, 0.65)
        else:
            verdict, conf = "conflict", 0.0
            objection = (
                f"'{term}' belongs to HSN chapter {chapter_hint(tcode)}, but this listing's context points to chapter "
                f"{chapter_hint(lcode)}."
            )
    elif lch and not tch:
        verdict = "unconfirmed"
        if echo:
            conf = 0.2
            objection = (
                f"'{term}' is only a near-spelling of '{raw}', while the listing's context points to HSN chapter "
                f"{chapter_hint(lcode)}."
            )
        else:
            conf = min(base, 0.65)
    elif echo:
        conf = min(base, 0.62)  # nothing to confirm it with, nothing against it
    return {"conf": round(conf, 2), "verdict": verdict, "echo": echo, "evidence": evidence, "objection": objection}


def tier(conf: float) -> str:
    return "accept" if conf >= ACCEPT else "weak" if conf >= WEAK else "reject"

"""AI value canonicalizer: one canonical English trade term for a product name or a free-text spec value.

"pyaz" and "onion", "lal" and "red", "sella" and "parboiled", "makka" and "maize" are the same thing written in different
languages or trade slang. Static synonym tables can never cover them, so at save time ONE batched model call maps the
product name and every free-text categorical value of a listing to the most common standard English trade term. The
result is stored on the listing under the reserved key ``_canon`` (ignored by every render / display / index function):

    {"v": 1, "product": "onion", "values": {"color": "red", "type": "parboiled"}, "n": 1}

``n`` is the number of distinct products the listing names (a keyword-stuffed listing names many).

Matching compares canonical values when BOTH sides carry them and falls back to the existing logic otherwise.

Cache: positive results are persistent (``data/value_canon.json``) per (product family, key, normalized raw value), so the
same raw value always maps to the same canonical value, whatever the model would say on another day. Failures are never
cached and never raise: a model that is down simply leaves the listing without ``_canon`` (it is retried lazily at
match time for the requester).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from services import value_canon_validate

logger = logging.getLogger(__name__)

CANON_KEY = "_canon"
# Set on a listing saved without a complete "_canon" (model busy / down): the background job finishes it.
PENDING_KEY = "_canon_pending"
CANON_VERSION = 2
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "value_canon.json"
SAVE_TIMEOUT_SECONDS = 25.0
MAX_VALUES = 14
_MAX_VALUE_CHARS = 60

# Keys that never carry a free-text categorical value.
_SKIP_KEYS = frozenset({
    "name", "product", "product_name", "title", "hsn_code", "hsn_source", "_negated", "negated_attributes",
    "min_quantity", "max_quantity", "moq", CANON_KEY, PENDING_KEY,
})

_cache: Optional[dict[str, Any]] = None


def _norm(text: Any) -> str:
    return re.sub(r"[\s\-_/]+", " ", re.sub(r"[^\w\s\-/]", " ", str(text or "").lower(), flags=re.UNICODE)).strip()


def canon_text(text: Any) -> str:
    """Comparison form of a canonical term: lowercase, plurals folded ("onions" == "onion")."""
    out = []
    for word in _norm(text).split():
        if len(word) > 3 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("es") and word[-3] in "sxz":
            word = word[:-2]
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.append(word)
    return " ".join(out)


def term_key(text: Any) -> str:
    """Identity of a canonical term: plurals folded, case, spaces and separators ignored ("s-30" = "s 30" = "s30",
    "sun-dried" = "sun dried"), grade codes collapsed through the same normalizer the matcher uses everywhere."""
    from services import spec_codes

    base = spec_codes.canonical_value(canon_text(text))
    return re.sub(r"[\s\-_./]+", "", base)


def same_term(a: Any, b: Any) -> bool:
    ka, kb = term_key(a), term_key(b)
    return bool(ka) and ka == kb


# ---------------------------------------------------------------------------
# Persistent, versioned, revisable cache
# ---------------------------------------------------------------------------
# Entry (version 2): {"v": 2, "term": str, "conf": 0..1, "model": str, "ts": epoch, "exp": epoch|None,
#                     "evidence": {...}}. ``exp`` is None for an accepted entry (conf >= ACCEPT, permanent); a weak or
# rejected answer is kept only for a short TTL so the model is neither re-asked on every save nor trusted. Entries
# written before versioning are plain strings: product names among them are NOT trusted (``revalidate_cache`` re-checks
# them), plain value strings still are. Counts (``|n|``) are plain ints.
WEAK_TTL_SECONDS = 24 * 3600.0
REJECT_TTL_SECONDS = 3600.0
_PRODUCT_PREFIX = "|name|"


def _load() -> dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")).get("entries", {})
            if not isinstance(_cache, dict):
                _cache = {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save() -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": CANON_VERSION, "entries": _cache}, fh, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:  # noqa: BLE001
        logger.debug("value canon cache not saved: %s", exc)


def reset_cache(path: Optional[Path] = None) -> None:
    """Forget the in-memory cache (tests); optionally point it at another file."""
    global _cache, CACHE_PATH
    _cache = None
    if path is not None:
        CACHE_PATH = path


def _family(name: Any) -> str:
    try:
        from services import spec_catalog

        return spec_catalog.family_key_for(name) or ""
    except Exception:  # noqa: BLE001
        return _norm(name)


def _model_name() -> str:
    try:
        from services import llm_client

        return str(llm_client.chat_model())
    except Exception:  # noqa: BLE001
        return ""


def _context(details: dict[str, Any], category: Any, raw_name: Any = None, title: Any = None) -> str:
    """Coarse context a product name was read in: its category and, when known, its HSN chapter. The same word in
    another context is not forced to one meaning."""
    from services import hsn_service

    code, _src = value_canon_validate.listing_hsn(details, raw_name, title)
    chapter = hsn_service.get_chapter(code) if code else None
    return f"{_norm(category)}:{chapter or ''}"


def _k_product(ctx: str, raw: Any) -> str:
    return f"{_PRODUCT_PREFIX}{ctx}|{_norm(raw)}"


def _k_value(family: str, category: Any, key: str, raw: Any) -> str:
    return f"{family}|{_norm(category)}|{key}|{_norm(raw)}"


def _k_count(title: Any, name: Any) -> str:
    return f"|n|{_norm(title)}|{_norm(name)}"


def _put(key: str, term: Optional[str], conf: float, evidence: Optional[dict[str, Any]] = None) -> None:
    now = time.time()
    tier = value_canon_validate.tier(conf)
    ttl = None if tier == "accept" else WEAK_TTL_SECONDS if tier == "weak" else REJECT_TTL_SECONDS
    _load()[key] = {
        "v": CANON_VERSION, "term": term, "conf": round(float(conf), 2), "model": _model_name(), "ts": round(now),
        "exp": None if ttl is None else round(now + ttl), "evidence": evidence or {},
    }


def _lookup(key: str, *, product: bool = False) -> tuple[str, Optional[str]]:
    """("ok", term) for a trusted entry, ("asked", None) for a weak/rejected answer still inside its TTL (do not ask
    again, do not use), ("miss", None) otherwise (including expired entries and untrusted legacy product names)."""
    entry = _load().get(key)
    if isinstance(entry, str):
        return ("miss", None) if product else ("ok", entry)
    if not isinstance(entry, dict) or entry.get("v") != CANON_VERSION:
        return "miss", None
    exp = entry.get("exp")
    if exp is not None and exp <= time.time():
        return "miss", None
    term, conf = entry.get("term"), float(entry.get("conf") or 0.0)
    if term and conf >= value_canon_validate.ACCEPT:
        return "ok", str(term)
    return "asked", None


# ---------------------------------------------------------------------------
# Revalidate / purge (a wrong canonical must be correctable without hand-editing JSON)
# ---------------------------------------------------------------------------
def revalidate_cache(*, apply: bool = True, path: Optional[Path] = None) -> dict[str, Any]:
    """Re-check every cached product name against independent evidence (``value_canon_validate.assess``).

    Entries that now fail are purged; trusted legacy entries are upgraded to the versioned form; the rest are
    kept. ``apply=False`` only reports. Returns {"purged": [...], "upgraded": [...], "kept": n}.
    """
    original = CACHE_PATH
    if path is not None:
        reset_cache(path)
    try:
        cache = _load()
        report: dict[str, Any] = {"purged": [], "upgraded": [], "kept": 0}
        for key in sorted(cache):
            entry = cache[key]
            if not key.startswith(_PRODUCT_PREFIX):
                if isinstance(entry, str):  # legacy value entry: wrap, trusted as before
                    report["upgraded"].append(key)
                    if apply:
                        _put(key, entry, 0.7, {"legacy": True})
                else:
                    report["kept"] += 1
                continue
            parts = key[len(_PRODUCT_PREFIX):].split("|", 1)
            raw = parts[-1]
            term = entry if isinstance(entry, str) else (entry or {}).get("term")
            if not term:
                report["kept"] += 1
                continue
            evidence = {} if isinstance(entry, str) else (entry.get("evidence") or {})
            details = {"hsn_code": evidence["listing_hsn"]} if evidence.get("listing_hsn") and evidence.get("listing_hsn_source") == "stored" else {}
            model_conf = None if isinstance(entry, str) else entry.get("conf")
            verdict = value_canon_validate.assess(raw, term, model_confidence=model_conf, details=details)
            if verdict["conf"] < value_canon_validate.WEAK:
                report["purged"].append({"key": key, "term": term, "why": verdict["objection"] or "low confidence", "conf": verdict["conf"]})
                if apply:
                    cache.pop(key, None)
            elif isinstance(entry, str):
                report["upgraded"].append(key)
                if apply:
                    _put(key, term, verdict["conf"], {**verdict["evidence"], "legacy": True})
            else:
                report["kept"] += 1
        if apply and (report["purged"] or report["upgraded"]):
            _save()
        return report
    finally:
        if path is not None:
            reset_cache(original)


def purge_cache(*, contains: Optional[str] = None, term: Optional[str] = None, apply: bool = True, path: Optional[Path] = None) -> list[str]:
    """Remove entries whose key contains ``contains`` and/or whose canonical term equals ``term``; returns the keys."""
    original = CACHE_PATH
    if path is not None:
        reset_cache(path)
    try:
        cache = _load()
        gone = []
        for key, entry in list(cache.items()):
            value = entry if isinstance(entry, str) else (entry.get("term") if isinstance(entry, dict) else None)
            if contains and contains.lower() not in key.lower():
                continue
            if term and _norm(value) != _norm(term):
                continue
            if not contains and not term:
                continue
            gone.append(key)
            if apply:
                cache.pop(key, None)
        if apply and gone:
            _save()
        return gone
    finally:
        if path is not None:
            reset_cache(original)


# ---------------------------------------------------------------------------
# What to canonicalize
# ---------------------------------------------------------------------------
def categorical_values(details: dict[str, Any]) -> dict[str, str]:
    """The free-text values of ``details`` worth mapping (not numbers, measurements or flags)."""
    from services import spec_catalog

    out: dict[str, str] = {}
    for key, value in details.items():
        k = str(key)
        if k in _SKIP_KEYS or k.startswith("_") or k.endswith("__must_match"):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value if v not in (None, ""))
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or len(text) > _MAX_VALUE_CHARS or not re.search(r"[^\W\d_]", text, flags=re.UNICODE):
            continue
        parsed = spec_catalog.parse_number(text)
        if parsed is not None and (not parsed["unit"] or spec_catalog.is_known_unit(parsed["unit"])):
            continue  # a number or a measurement: compared numerically
        out[k] = text
        if len(out) >= MAX_VALUES:
            break
    return out


def _product_name(details: dict[str, Any], title: Any) -> str:
    for key in ("name", "product", "product_name"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(title or "").strip()


_PROMPT = """You normalise B2B trade listings so that buyers and sellers who describe the same thing in different words, \
languages or spellings can be matched.

Listing JSON (use ALL of it to decide what the product is):
{payload}

Return JSON with exactly these fields:
- "product": the most common standard English trade term for the product named in "product_name" (lowercase, singular, \
no brand, no marketing words). Decide from the whole listing: title, category, tariff (HSN) hint, and every attribute \
(a grade such as S-30 or ICUMSA 45 implies a different product than a grade such as 316L). A regional-language word \
(Hindi, Gujarati, Tamil, Marathi, Arabic, Chinese ...) often means something its spelling does not suggest: NEVER answer \
with an English word merely because it looks like the spelling of the given word. If it is a synonym, translation or \
transliteration of a more common English term, return that term. Keep qualifiers that make it a different product \
(sweet corn is not corn; groundnut oil is not groundnut).
- "product_confidence": 0 to 1, how sure you are of "product" given the context.
- "values": an object with one entry per key of "attributes_to_normalise": the most common standard English trade term \
for that value (lowercase). Translate or transliterate words from other languages. KEEP IDENTIFIERS UNCHANGED: grade \
codes, model numbers, variety numbers, standards, brand names. If the value is already a standard English term return it \
unchanged.
- "distinct_products": how many different products the "title" and "product_name" name. One product with its specs is 1; \
a keyword list of several commodities is that many (a generic word such as "vegetables" or "everything" is not itself a product).

Only JSON, no commentary."""

_PRODUCT_RETRY = """A previous answer for this listing was rejected: {objection}

Listing JSON:
{payload}

Return JSON {{"product": "<most common standard English trade term>", "product_confidence": <0 to 1>}}, deciding from the \
whole listing and the objection above. Only JSON."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "product": {"type": "string"},
        "product_confidence": {"type": "number"},
        "values": {"type": "object", "additionalProperties": {"type": "string"}},
        "distinct_products": {"type": "integer"},
    },
    "required": ["product", "product_confidence", "values", "distinct_products"],
}
_RETRY_SCHEMA = {
    "type": "object",
    "properties": {"product": {"type": "string"}, "product_confidence": {"type": "number"}},
    "required": ["product", "product_confidence"],
}


def _clean_term(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().lower().split())
    if not text or len(text) > 80:
        return None
    return text


def _listing_context(details: dict[str, Any], title: Any, category: Any, raw_name: str) -> dict[str, Any]:
    """Everything the model should see besides the word itself."""
    code, source = value_canon_validate.listing_hsn(details, raw_name, title)
    ctx: dict[str, Any] = {"title": str(title or ""), "category": str(category or "")}
    if code:
        label = "HSN code on the listing" if source == "stored" else "tariff hint (keyword based, may be wrong)"
        ctx[label] = f"{code}: chapter {value_canon_validate.chapter_hint(code)}"
    ctx["all_attributes"] = {
        str(k): (v if isinstance(v, (str, int, float, bool)) else str(v))
        for k, v in list(details.items())[:24]
        if not str(k).startswith("_") and not str(k).endswith("__must_match") and k not in ("name", "product", "product_name")
    }
    return ctx


async def _ask(
    product_name: str, title: str, attrs: dict[str, str], timeout: float, *, details: Optional[dict[str, Any]] = None,
    category: Any = None, objection: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    from services import llm_client

    payload_obj: dict[str, Any] = {"product_name": product_name, **_listing_context(details or {}, title, category, product_name)}
    if objection is None:
        payload_obj["attributes_to_normalise"] = attrs
    payload = json.dumps(payload_obj, ensure_ascii=False)
    prompt = _PRODUCT_RETRY.format(objection=objection, payload=payload) if objection else _PROMPT.format(payload=payload)
    try:
        reply = await llm_client.generate_json(prompt, _RETRY_SCHEMA if objection else _SCHEMA, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.debug("value canon call failed: %s", exc)
        return None
    return reply if isinstance(reply, dict) else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _from_cache(details: dict[str, Any], title: Any, category: Any = None) -> Optional[dict[str, Any]]:
    """The ``_canon`` block built from the persistent cache alone (None unless the product and every value are cached)."""
    name = _product_name(details, title)
    if not name:
        return None
    state, product = _lookup(_k_product(_context(details, category, name, title), name), product=True)
    if state != "ok":
        return None
    family = _family(name)
    values: dict[str, str] = {}
    for key, raw in categorical_values(details).items():
        state, hit = _lookup(_k_value(family, category, key, raw))
        if state != "ok":
            return None
        values[key] = str(hit)
    out: dict[str, Any] = {"v": CANON_VERSION, "product": product, "values": values}
    count = _load().get(_k_count(title, name))
    if isinstance(count, int):
        out["n"] = count
    return out


async def canonicalize(
    details: Optional[dict[str, Any]], title: Any = None, *, allow_llm: bool = True,
    timeout: float = SAVE_TIMEOUT_SECONDS, category: Any = None,
) -> Optional[dict[str, Any]]:
    """The ``_canon`` block for a listing's (already normalized) ``details``; None when nothing could be resolved.

    The product name is read WITH its context (title, category, HSN, every attribute) and validated against
    independent evidence before it is cached permanently. Uses the persistent cache for everything it can; only the
    uncached items go to the model, in ONE call. Never raises.
    """
    try:
        return await _canonicalize(details, title, allow_llm, timeout, category)
    except Exception as exc:  # noqa: BLE001
        logger.warning("value canonicalization skipped: %s", exc)
        return None


async def _canonicalize(
    details: Optional[dict[str, Any]], title: Any, allow_llm: bool, timeout: float, category: Any
) -> Optional[dict[str, Any]]:
    if not isinstance(details, dict) or not details:
        return None
    cache = _load()
    name = _product_name(details, title)
    ctx = _context(details, category, name, title)
    family = _family(name)
    attrs = categorical_values(details)

    pkey = _k_product(ctx, name) if name else ""
    pstate, product = _lookup(pkey, product=True) if name else ("miss", None)
    values: dict[str, str] = {}
    missing: dict[str, str] = {}
    for key, raw in attrs.items():
        state, hit = _lookup(_k_value(family, category, key, raw))
        if state == "ok":
            values[key] = str(hit)
        elif state == "miss":
            missing[key] = raw
    count = cache.get(_k_count(title, name))

    need_call = (name and pstate == "miss") or missing or count is None
    if need_call and allow_llm and (name or missing):
        reply = await _ask(name, str(title or ""), missing, timeout, details=details, category=category)
        if reply:
            changed = False
            if name and pstate == "miss":
                product = await _accept_product(
                    pkey, name, reply.get("product"), reply.get("product_confidence"), details, title, category, timeout
                )
                changed = True
            got = reply.get("values") if isinstance(reply.get("values"), dict) else {}
            for key, raw in missing.items():
                term = _clean_term(got.get(key))
                if term:
                    _put(_k_value(family, category, key, raw), term, value_canon_validate.DEFAULT_MODEL_CONFIDENCE, {"key": key})
                    values[key] = term
                    changed = True
            n = reply.get("distinct_products")
            if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 50 and count is None:
                count = cache[_k_count(title, name)] = n
                changed = True
            if changed:
                _save()
    if not product and not values:
        return None
    out: dict[str, Any] = {"v": CANON_VERSION}
    if product:
        out["product"] = product
    out["values"] = values
    if isinstance(count, int):
        out["n"] = count
    return out


async def _accept_product(
    pkey: str, name: str, term_raw: Any, conf_raw: Any, details: dict[str, Any], title: Any, category: Any, timeout: float
) -> Optional[str]:
    """Validate the model's product term against independent evidence, re-ask once with the objection made explicit when
    it contradicts the context, and cache the outcome (permanent only when accepted). Returns the term when accepted."""
    term = _clean_term(term_raw)
    if term is None:  # the batch reply left the product out: ask for it alone
        again = await _ask(name, str(title or ""), {}, timeout, details=details, category=category,
                           objection="the previous reply contained no product term")
        term, conf_raw = _clean_term(again.get("product")) if again else None, (again or {}).get("product_confidence")
        if term is None:
            return None  # model unavailable or useless: nothing cached, the job retries
    verdict = value_canon_validate.assess(name, term, model_confidence=conf_raw, details=details, title=title)
    if verdict["objection"] and value_canon_validate.tier(verdict["conf"]) != "accept":
        again = await _ask(name, str(title or ""), {}, timeout, details=details, category=category, objection=verdict["objection"])
        retry_term = _clean_term(again.get("product")) if again else None
        if retry_term and retry_term != term:
            retry = value_canon_validate.assess(
                name, retry_term, model_confidence=again.get("product_confidence"), details=details, title=title
            )
            if retry["conf"] > verdict["conf"]:
                term, verdict = retry_term, retry
    conf = verdict["conf"]
    _put(pkey, term, conf, {**verdict["evidence"], "verdict": verdict["verdict"], "category": str(category or "")})
    return term if value_canon_validate.tier(conf) == "accept" else None


def is_complete(details: dict[str, Any], canon: Optional[dict[str, Any]], title: Any = None) -> bool:
    """True when ``canon`` covers the product, every free-text value and the distinct-products count."""
    if not canon or not canon.get("product") or not isinstance(canon.get("n"), int):
        return False
    return set(categorical_values(details)) <= set(canon.get("values") or {})


def _needs_canon(details: dict[str, Any], title: Any) -> bool:
    return bool(_product_name(details, title) or categorical_values(details))


async def add_canon(
    details: dict[str, Any], title: Any = None, *, allow_llm: bool = True, timeout: float = SAVE_TIMEOUT_SECONDS,
    category: Any = None,
) -> dict[str, Any]:
    """``details`` with a fresh ``_canon`` block. Never raises.

    When the block could not be completed (model busy or down) and the model was allowed, the listing is marked
    ``_canon_pending`` so ``complete_pending`` (background job) finishes it later; a complete block clears the mark.
    """
    base = {k: v for k, v in (details or {}).items() if k not in (CANON_KEY, PENDING_KEY)}
    canon = await canonicalize(base, title, allow_llm=allow_llm, timeout=timeout, category=category)
    if canon:
        base[CANON_KEY] = canon
    if allow_llm and _needs_canon(base, title) and not is_complete(base, canon, title):
        base[PENDING_KEY] = True
    return base


async def complete_pending(
    db: Any, *, limit: int = 20, timeout: float = 60.0, commit: bool = True, reindex: bool = True,
    only_ids: Optional[list[Any]] = None,
) -> int:
    """Finish the listings marked ``_canon_pending`` (oldest first). Returns how many are now complete.

    Plain ``async def job(db) -> int`` like the other scheduler jobs. A listing the model still cannot finish keeps its
    mark and is retried on the next run; one bad row never stops the batch. ``commit=False`` flushes only (tests and the
    scenario harness, which roll back); ``reindex`` flags a completed listing for re-embedding (its search text gained
    the canonical product).
    """
    from sqlalchemy import select

    from models.enums import RFQStatus
    from models.rfq import RFQ
    from services import rfq_indexing

    query = select(RFQ.id).where(RFQ.product_details.has_key(PENDING_KEY))
    if only_ids is not None:
        query = query.where(RFQ.id.in_(only_ids))
    else:
        query = query.where(RFQ.status == RFQStatus.ACTIVE)
    ids = (await db.scalars(query.order_by(RFQ.updated_at).limit(limit))).all()
    done = 0
    for rfq_id in ids:
        try:
            rfq = await db.get(RFQ, rfq_id)
            if rfq is None or not isinstance(rfq.product_details, dict):
                continue
            had_product = bool(canon_product(rfq.product_details))
            fresh = await add_canon(rfq.product_details, rfq.title, timeout=timeout, category=rfq.category)
            if fresh == rfq.product_details:
                continue
            rfq.product_details = fresh
            if not had_product and canon_product(fresh):
                if reindex:
                    rfq_indexing.refresh_index_fields(rfq)
                else:
                    keep = rfq.embedding_status
                    rfq_indexing.refresh_index_fields(rfq)
                    rfq.embedding_status = keep
            if PENDING_KEY not in fresh:
                done += 1
            if commit:
                await db.commit()
            else:
                await db.flush()
        except Exception:  # noqa: BLE001 - one bad row must not stop the batch
            logger.exception("canonicalization retry failed for rfq %s", rfq_id)
            if commit:
                await db.rollback()
    return done


def get_canon(details: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    canon = (details or {}).get(CANON_KEY) if isinstance(details, dict) else None
    return canon if isinstance(canon, dict) else None


def canon_product(details: Optional[dict[str, Any]]) -> Optional[str]:
    canon = get_canon(details)
    return term_key(canon.get("product")) if canon and canon.get("product") else None


def canon_value(details: Optional[dict[str, Any]], key: str) -> Optional[str]:
    canon = get_canon(details)
    values = canon.get("values") if canon else None
    if isinstance(values, dict) and values.get(key):
        return str(values[key])
    return None


def distinct_products(details: Optional[dict[str, Any]]) -> int:
    canon = get_canon(details)
    n = canon.get("n") if canon else None
    return n if isinstance(n, int) and n >= 1 else 1


_WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def vocabulary_product_count(text: Any) -> int:
    """Distinct products a text names, from the platform's own product vocabulary (aliases, learned canonical
    products, the AI lexicon): no model call, so it works when the canonicalizer could not run.

    Each word is looked up and the distinct canonical products counted; a canonical that merely contains another
    ("stainless steel" / "steel") is one product."""
    found: set[str] = set()
    try:
        from services import product_synonyms

        for word in _WORD_RE.findall(str(text or "").lower()):
            canon = product_synonyms.canonical_token(word)
            if canon:
                found.add(canon_text(canon))
    except Exception:  # noqa: BLE001
        return 1
    distinct = {c for c in found if not any(c != o and c in o for o in found)}
    return max(1, len(distinct))


# A fallback count below this is ignored: two product words are usually one product ("stainless steel", "rice flour").
_FALLBACK_MIN_PRODUCTS = 3


def listing_breadth(details: Optional[dict[str, Any]], title: Any = None) -> int:
    """Distinct products a listing names: the model's count from ``_canon`` and, independently, the vocabulary count
    over its title and product name when that reaches ``_FALLBACK_MIN_PRODUCTS`` (the larger one wins)."""
    n = distinct_products(details)
    name = (details or {}).get("name") if isinstance(details, dict) else None
    fallback = vocabulary_product_count(f"{title or ''} {name or ''}")
    return max(n, fallback) if fallback >= _FALLBACK_MIN_PRODUCTS else n


def attach_to_rfq(rfq: Any, canon: Optional[dict[str, Any]]) -> None:
    """Put ``canon`` on the in-memory listing WITHOUT marking the row modified (a read must not write)."""
    if not canon or not isinstance(getattr(rfq, "product_details", None), dict):
        return
    from sqlalchemy.orm.attributes import set_committed_value

    merged = {**rfq.product_details, CANON_KEY: canon}
    try:
        set_committed_value(rfq, "product_details", merged)
    except Exception:  # noqa: BLE001  (not an ORM instance)
        rfq.product_details = merged


async def ensure_for_rfq(rfq: Any, *, allow_llm: bool, timeout: float = SAVE_TIMEOUT_SECONDS) -> None:
    """Give ``rfq`` a ``_canon`` block in memory when it has none: from the cache, and (``allow_llm``) from the model."""
    details = getattr(rfq, "product_details", None)
    if not isinstance(details, dict) or get_canon(details):
        return
    canon = await canonicalize(
        details, getattr(rfq, "title", None), allow_llm=allow_llm, timeout=timeout, category=getattr(rfq, "category", None)
    )
    attach_to_rfq(rfq, canon)


def ensure_for_rfq_cached(rfq: Any) -> None:
    """Cache-only (synchronous, no model) version of ``ensure_for_rfq`` for candidates scored in bulk."""
    try:
        details = getattr(rfq, "product_details", None)
        if not isinstance(details, dict) or get_canon(details):
            return
        attach_to_rfq(rfq, _from_cache(details, getattr(rfq, "title", None), getattr(rfq, "category", None)))
    except Exception:  # noqa: BLE001
        return

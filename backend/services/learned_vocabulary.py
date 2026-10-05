"""Vocabulary that grows from grounded AI parses.

Closed lists in ``query_extractor`` are only seeds. Every time the model
understands something the rules did not (a new category, a role phrase, an
attribute value, a unit), the grounded result is remembered here so the offline
rule parser understands it next time without a model call.
"""

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from core.config import settings
from models.enums import RFQRole
from services.categories import CANONICAL_CATEGORIES

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 5000
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_lock = threading.RLock()
_data: Optional[dict[str, Any]] = None
_path_override: Optional[Path] = None


def _empty() -> dict[str, Any]:
    return {
        "product_category": {}, "role_phrases": {}, "attribute_values": {},
        "units": [], "categories": [], "unknown_terms": {},
    }


def _path() -> Path:
    if _path_override is not None:
        return _path_override
    raw = Path(getattr(settings, "LEARNED_VOCAB_PATH", "") or "data/learned_vocab.json")
    return raw if raw.is_absolute() else _BACKEND_DIR / raw


def set_path(path: Optional[Path | str]) -> None:
    """Point the store at another file (tests) and drop the in-memory copy."""
    global _path_override, _data
    with _lock:
        _path_override = Path(path) if path is not None else None
        _data = None


def _load() -> dict[str, Any]:
    global _data
    with _lock:
        if _data is not None:
            return _data
        data = _empty()
        try:
            path = _path()
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for key, default in data.items():
                        value = loaded.get(key)
                        if isinstance(value, type(default)):
                            data[key] = value
        except Exception as exc:
            logger.warning("learned vocabulary unreadable (%s); starting empty", exc)
        _data = data
        return data


def _save(data: dict[str, Any]) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("could not persist learned vocabulary (%s)", exc)


def _put(mapping: dict[str, Any], key: str, value: Any) -> bool:
    if key not in mapping and len(mapping) >= _MAX_ENTRIES:
        return False
    if mapping.get(key) == value:
        return False
    mapping[key] = value
    return True


def learn_from(parse: Any, message: str) -> None:
    """Remember the grounded fields of ``parse``. Never raises."""
    try:
        _learn(parse, message)
    except Exception as exc:
        logger.debug("learning skipped (%s)", exc)


def _learn(parse: Any, message: str) -> None:
    changed = False
    with _lock:
        data = _load()

        product = (parse.product or "").strip().lower()
        if product and parse.category:
            for name in {product, (parse.product_as_written or "").strip().lower()}:
                if name and len(name.split()) <= 5:
                    changed |= _put(data["product_category"], name, parse.category)
            canonical = {c.lower() for c in CANONICAL_CATEGORIES}
            if parse.category.lower() not in canonical and parse.category not in data["categories"]:
                if len(data["categories"]) < _MAX_ENTRIES:
                    data["categories"].append(parse.category)
                    changed = True

        evidence = (parse.role_evidence or "").strip().lower()
        if parse.role is not None and evidence and 2 <= len(evidence.split()) <= 6 \
                and not re.search(r"\d", evidence):
            changed |= _put(data["role_phrases"], evidence, parse.role.value)

        for key, value in (parse.attributes or {}).items():
            if isinstance(value, str) and 0 < len(value.split()) <= 3:
                changed |= _put(data["attribute_values"], value.strip().lower(), key)

        unit = (parse.quantity_unit or "").strip().lower()
        if unit and len(unit) <= 20 and unit not in data["units"] and len(data["units"]) < _MAX_ENTRIES:
            data["units"].append(unit)
            changed = True

        for term in parse.unknown_terms or []:
            term = term.strip().lower()
            if term and len(term) <= 40:
                if term in data["unknown_terms"] or len(data["unknown_terms"]) < _MAX_ENTRIES:
                    data["unknown_terms"][term] = data["unknown_terms"].get(term, 0) + 1
                    changed = True
        if changed:
            _save(data)


def _find_longest(mapping: dict[str, Any], text: str) -> Optional[Any]:
    lowered = " ".join((text or "").lower().replace("’", "'").split())
    if not lowered:
        return None
    for key in sorted((k for k in mapping if k in lowered), key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(key)}(?:s|es)?(?!\w)", lowered):
            return mapping[key]
    return None


def category_for(text: str) -> Optional[str]:
    """Category of the longest learned product mentioned in ``text``."""
    return _find_longest(_load()["product_category"], text)


def role_for(text: str) -> Optional[RFQRole]:
    value = _find_longest(_load()["role_phrases"], text)
    try:
        return RFQRole(value) if value else None
    except ValueError:
        return None


def attribute_key_for(value: str) -> Optional[str]:
    return _load()["attribute_values"].get((value or "").strip().lower())


def learned_attribute_values() -> dict[str, str]:
    return dict(_load()["attribute_values"])


def known_units() -> list[str]:
    return list(_load()["units"])


def known_categories() -> list[str]:
    seen: dict[str, str] = {}
    for name in [*CANONICAL_CATEGORIES, *_load()["categories"]]:
        seen.setdefault(name.lower(), name)
    return list(seen.values())

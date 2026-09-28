"""HMAC-signed, expiring URLs for private media.

Deal-room live captures are private to the two parties of a connection, but
the browser loads them with ``<img src>``, which cannot carry an Authorization
header. So the API hands out URLs of the form::

    /api/v1/media/live_captures/live_<id>.jpg?exp=<unix ts>&sig=<hmac>

where ``sig = HMAC-SHA256(key, "<relative path>\\n<exp>")`` and ``key`` is
derived from ``settings.SECRET_KEY``. ``api/v1/media.py`` serves a private file
only with a valid, unexpired signature (or a bearer token of a participant).

Store the *unsigned* path in the database and sign at response time with
``sign_private_media_url``; a signed URL in the database would expire.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional
from urllib.parse import unquote

from core.config import settings

MEDIA_URL_PREFIX = "/api/v1/media/"
# Sub-directories of UPLOAD_DIR that are never served without a signature or token.
PRIVATE_MEDIA_DIRS = ("live_captures",)

# Expiry is rounded up to this many seconds so the same image gets the same URL
# for a while and the browser cache keeps working across list refreshes.
_EXPIRY_GRANULARITY = 900


def _key() -> bytes:
    # Domain-separated from JWT signing, which uses SECRET_KEY directly.
    return hashlib.sha256(b"media-url-signing\x00" + settings.SECRET_KEY.encode()).digest()


def media_relpath(path: str) -> str:
    """``/api/v1/media/live_captures/x.jpg?...`` -> ``live_captures/x.jpg``."""
    rel = path.split("?", 1)[0].split("#", 1)[0]
    for prefix in (MEDIA_URL_PREFIX, "/media/"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
            break
    return unquote(rel).lstrip("/")


def is_private_media(path: Optional[str]) -> bool:
    if not path:
        return False
    rel = media_relpath(path)
    return any(rel == d or rel.startswith(d + "/") for d in PRIVATE_MEDIA_DIRS)


def _signature(relpath: str, exp: int) -> str:
    mac = hmac.new(_key(), f"{relpath}\n{exp}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def sign_media_url(path: str, ttl: Optional[int] = None) -> str:
    """Return ``path`` (any existing query dropped) with ``?exp=&sig=`` appended."""
    ttl = settings.MEDIA_SIGNED_URL_TTL_SECONDS if ttl is None else int(ttl)
    base = path.split("?", 1)[0]
    exp = int(time.time()) + max(1, ttl)
    if ttl >= 2 * _EXPIRY_GRANULARITY:
        exp = -(-exp // _EXPIRY_GRANULARITY) * _EXPIRY_GRANULARITY
    return f"{base}?exp={exp}&sig={_signature(media_relpath(base), exp)}"


def sign_private_media_url(url: Optional[str], ttl: Optional[int] = None) -> Optional[str]:
    """Sign ``url`` if it points at private media; return anything else unchanged.

    Safe to call on every ``image_url`` in a response (None, public and external
    URLs pass through).
    """
    if url and is_private_media(url):
        return sign_media_url(url, ttl)
    return url


def verify_media_signature(relpath: str, exp: Optional[str | int], sig: Optional[str]) -> bool:
    if exp is None or not sig:
        return False
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_int < int(time.time()):
        return False
    return hmac.compare_digest(_signature(relpath, exp_int), sig)

"""In-memory sliding-window rate limiting.

Scope and limits
----------------
The counters live in this process's memory. That is enough to stop a single
client hammering login, signup or the upload endpoints on a one-worker
deployment, but it is **per process**: with N uvicorn/gunicorn workers the
effective limit is N times the configured one, and a restart forgets
everything. Moving to Redis (``settings.REDIS_URL`` is already declared) means
replacing ``SlidingWindowRateLimiter`` with a sorted-set implementation behind
the same ``hit`` / ``check`` interface; the call sites do not change.

Limits are configured in ``core.config.Settings`` as ``"<count>/<seconds>"``
strings (``RATE_LIMIT_LOGIN`` and friends) and read on every request, so a test
or an operator can change them at runtime.

Test isolation
--------------
The test suite runs every request from ``127.0.0.1`` and signs up hundreds of
accounts in one process. While pytest is running a test it exports
``PYTEST_CURRENT_TEST``; the limiter uses that test's node id as a key
namespace, so each test starts with empty buckets and no test can be throttled
by requests another test made. Outside pytest the variable is absent and the
namespace is empty. ``limiter.reset()`` clears everything explicitly.
"""

from __future__ import annotations

import math
import os
import threading
import time
import uuid
from collections import deque
from typing import Literal, Optional

from fastapi import HTTPException, Request, status

from core.config import settings

# Hard cap on distinct keys so a spray of random emails/IPs cannot grow memory
# without bound. When exceeded, the stalest keys are pruned.
MAX_KEYS = 100_000


def parse_rate(rate: str) -> tuple[int, float]:
    count, _, window = rate.strip().partition("/")
    return int(count), float(window)


class SlidingWindowRateLimiter:
    """Exact sliding-window log: a deque of hit timestamps per key.

    Guarded by a ``threading.Lock``: every operation is short and never awaits,
    so the same lock is safe from the event loop and from worker threads.
    """

    def __init__(self, max_keys: int = MAX_KEYS) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    @staticmethod
    def _prune(bucket: deque[float], now: float, window: float) -> None:
        cutoff = now - window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def _retry_after(self, bucket: deque[float], now: float, limit: int, window: float) -> float:
        if len(bucket) < limit:
            return 0.0
        # The oldest hit that still counts has to age out before a slot frees.
        oldest_counted = bucket[len(bucket) - limit]
        return max(0.001, oldest_counted + window - now)

    def check(self, key: str, limit: int, window: float, *, now: Optional[float] = None) -> float:
        """Seconds until ``key`` may proceed (0 = allowed). Records nothing."""
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._hits.get(key)
            if not bucket:
                return 0.0 if limit > 0 else window
            self._prune(bucket, now, window)
            return self._retry_after(bucket, now, limit, window)

    def hit(self, key: str, limit: int, window: float, *, now: Optional[float] = None) -> float:
        """Record one request for ``key``; return seconds to wait (0 = allowed).

        A refused request is not recorded, so a client that keeps retrying
        while blocked is not locked out for longer than the window.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= self._max_keys:
                    self._evict(now)
                bucket = self._hits[key] = deque()
            self._prune(bucket, now, window)
            wait = self._retry_after(bucket, now, limit, window)
            if wait == 0.0:
                bucket.append(now)
            return wait

    def _evict(self, now: float) -> None:
        # Drop empty buckets first, then the least recently used tenth.
        for key in [k for k, b in self._hits.items() if not b]:
            del self._hits[key]
        if len(self._hits) >= self._max_keys:
            by_age = sorted(self._hits.items(), key=lambda item: item[1][-1])
            for key, _ in by_age[: max(1, len(by_age) // 10)]:
                del self._hits[key]

    def reset(self, prefix: Optional[str] = None) -> None:
        with self._lock:
            if prefix is None:
                self._hits.clear()
            else:
                for key in [k for k in self._hits if k.startswith(prefix)]:
                    del self._hits[key]


limiter = SlidingWindowRateLimiter()


def _namespace() -> str:
    current = os.environ.get("PYTEST_CURRENT_TEST")
    if not current:
        return ""
    # "tests/test_x.py::test_y (call)" -> strip the phase so setup and call share.
    return current.rsplit(" ", 1)[0] + "|"


def client_ip(request: Request) -> str:
    """The peer address as uvicorn reports it.

    Behind a reverse proxy, run uvicorn with ``--proxy-headers`` and a correct
    ``--forwarded-allow-ips`` so this is the real client, never a spoofable
    X-Forwarded-For read directly here.
    """
    return request.client.host if request.client else "unknown"


def _too_many(retry_after: float) -> HTTPException:
    seconds = max(1, math.ceil(retry_after))
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        f"Too many requests. Try again in {seconds} seconds.",
        headers={"Retry-After": str(seconds)},
    )


def _key(scope: str, identity: str) -> str:
    return f"{_namespace()}{scope}:{identity}"


def enforce(scope: str, identity: str, rate: str) -> None:
    """Count one request against ``scope``/``identity``; raise 429 when over."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    limit, window = parse_rate(rate)
    wait = limiter.hit(_key(scope, identity), limit, window)
    if wait:
        raise _too_many(wait)


def ensure_not_blocked(scope: str, identity: str, rate: str) -> None:
    """Raise 429 if ``identity`` is already over the limit, without counting."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    limit, window = parse_rate(rate)
    wait = limiter.check(_key(scope, identity), limit, window)
    if wait:
        raise _too_many(wait)


def record(scope: str, identity: str, rate: str) -> None:
    """Count one event (e.g. a failed login) without raising."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    limit, window = parse_rate(rate)
    limiter.hit(_key(scope, identity), limit, window)


def _user_identity(request: Request) -> Optional[str]:
    """User id from a valid bearer access token, without a DB round trip.

    Only used to pick the bucket; the endpoint's own auth dependency still
    decides whether the request is allowed at all.
    """
    from core.security import TokenError, decode_token

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        return str(uuid.UUID(decode_token(token.strip(), expected_type="access")["sub"]))
    except (TokenError, ValueError, KeyError):
        return None


class RateLimit:
    """FastAPI dependency: ``Depends(RateLimit("signup", "RATE_LIMIT_SIGNUP"))``.

    ``per="ip"`` keys on the client address; ``per="user"`` keys on the
    authenticated user and falls back to the address for anonymous callers.
    """

    def __init__(self, scope: str, setting: str, per: Literal["ip", "user"] = "ip") -> None:
        self.scope = scope
        self.setting = setting
        self.per = per

    async def __call__(self, request: Request) -> None:
        identity = None
        if self.per == "user":
            identity = _user_identity(request)
            identity = f"user:{identity}" if identity else None
        if identity is None:
            identity = f"ip:{client_ip(request)}"
        enforce(self.scope, identity, getattr(settings, self.setting))

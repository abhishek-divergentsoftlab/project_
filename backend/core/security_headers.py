"""Baseline security headers for every HTTP response.

A pure ASGI middleware rather than ``BaseHTTPMiddleware``: it only touches the
``http.response.start`` message, so streaming responses (the AI chat SSE
stream, file downloads) pass through unbuffered, and websockets are untouched.

No Content-Security-Policy is set: the API serves JSON, files and Swagger UI
(which loads from a CDN), and the SPA itself is served by Vite, so a CSP here
would protect nothing while breaking /docs.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": (
        "accelerometer=(), autoplay=(), camera=(), display-capture=(), "
        "geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
        "payment=(), usb=(), interest-cohort=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
}

# Uploaded certificate PDFs are previewed in an <iframe> by the profile page
# (same origin through the Vite proxy), so media may be framed by our own
# origin -- but never by anyone else.
FRAMEABLE_PREFIXES = ("/api/v1/media/",)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        frameable = scope.get("path", "").startswith(FRAMEABLE_PREFIXES)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for name, value in SECURITY_HEADERS.items():
                    if name == "X-Frame-Options" and frameable:
                        value = "SAMEORIGIN"
                    key = name.lower().encode("latin-1")
                    if key not in present:
                        headers.append((key, value.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLarge(Exception):
    pass


class MaxBodySizeMiddleware:
    """Reject request bodies over ``limit`` bytes with 413 before they are buffered.

    A declared Content-Length is refused up front; chunked / lying bodies are
    counted as they stream in and cut off at the limit. Pure ASGI, so streamed
    responses are untouched.
    """

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.limit
            except ValueError:
                too_big = False
            if too_big:
                await self._reject(send)
                return

        received = 0
        exceeded = False
        responded = False

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal responded
            if not exceeded:
                await send(message)
                return
            # The app reacted to the aborted read (usually with a 400): answer 413 instead.
            if message["type"] == "http.response.start" and not responded:
                responded = True
                await self._reject(send)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except _BodyTooLarge:
            if not responded:
                responded = True
                await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = json.dumps({"detail": "Request body too large"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

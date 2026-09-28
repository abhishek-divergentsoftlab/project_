"""Baseline security headers for every HTTP response.

A pure ASGI middleware rather than ``BaseHTTPMiddleware``: it only touches the
``http.response.start`` message, so streaming responses (the AI chat SSE
stream, file downloads) pass through unbuffered, and websockets are untouched.

No Content-Security-Policy is set: the API serves JSON, files and Swagger UI
(which loads from a CDN), and the SPA itself is served by Vite, so a CSP here
would protect nothing while breaking /docs.
"""

from __future__ import annotations

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

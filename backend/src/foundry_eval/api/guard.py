"""Loopback-only request guard (pure ASGI, covers API and static files).

* Host must be the app's own loopback host:port (DNS-rebinding protection).
* ``Sec-Fetch-Site`` (when sent) must be ``same-origin``, or ``none`` for top-level GET
  navigations. ``same-site`` is rejected: pages served from other localhost ports are same-site.
* State-changing requests must carry an ``Origin`` equal to the app origin, a JSON content type
  (when they have a body) and ``X-FME-Client: 1`` (forces a CORS preflight that is never granted).
* No CORS headers are ever sent. Security headers are added to every response.
"""

from __future__ import annotations

import json
import logging

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD"}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
SECURITY_HEADERS = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


class HttpGuard:
    def __init__(self, app: ASGIApp, *, allowed_hosts: set[str], allowed_origins: set[str]) -> None:
        self.app = app
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self._reject(send, 403, "Only HTTP requests are accepted.")
            return
        headers = Headers(scope=scope)
        problem = self.check(scope["method"], headers)
        if problem is not None:
            status, message = problem
            await self._reject(send, status, message)
            return

        path: str = scope.get("path", "")
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                response_headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    response_headers[name] = value
                if path.startswith("/api/"):
                    response_headers["cache-control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            # Answer unexpected errors here, so even a 500 carries the security headers (the
            # framework's outermost error handler would answer without them).
            log.exception("Unhandled error while serving %s %s", scope.get("method"), path)
            if not started:
                await self._reject(send, 500, "Internal error. Details are in the app's log file.")

    def check(self, method: str, headers: Headers) -> tuple[int, str] | None:
        host = headers.get("host", "").lower()
        if host not in self.allowed_hosts:
            return 421, "Invalid Host header."
        fetch_site = headers.get("sec-fetch-site")
        if fetch_site is not None and fetch_site not in ("same-origin", "none"):
            return 403, "Cross-site requests are not allowed."
        if fetch_site == "none" and method not in SAFE_METHODS:
            return 403, "Cross-site requests are not allowed."
        origin = headers.get("origin")
        if method in SAFE_METHODS:
            if origin is not None and origin not in self.allowed_origins:
                return 403, "Cross-origin requests are not allowed."
            return None
        if method == "OPTIONS":
            return 405, "CORS preflight is not supported."
        if origin not in self.allowed_origins:
            return 403, "State-changing requests must come from the app itself."
        if headers.get("x-fme-client") != "1":
            return 403, "Missing X-FME-Client header."
        has_body = headers.get("content-length", "0") not in ("0", "") or "transfer-encoding" in headers
        if has_body and not headers.get("content-type", "").lower().startswith("application/json"):
            return 415, "Request bodies must be JSON."
        return None

    async def _reject(self, send: Send, status: int, message: str) -> None:
        body = json.dumps({"detail": message}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        headers += [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

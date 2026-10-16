"""Pure ASGI middleware: Host validation and security headers (no body buffering)."""

from __future__ import annotations

import re

_HOST_RE = re.compile(r"^(?P<host>[a-z0-9.-]+)(?::(?P<port>[0-9]{1,5}))?$")

BASE_CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
# The Telegram Login Widget needs its script and its iframe, on the login page only.
LOGIN_CSP = (
    "default-src 'none'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self'; "
    "script-src https://telegram.org; frame-src https://oauth.telegram.org; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
# Telegram's login popup talks to the page that opened it through window.opener. With
# "same-origin" the browser severs that link (the login then never completes), so the login page
# alone uses "same-origin-allow-popups"; every other page keeps "same-origin".
LOGIN_COOP = b"same-origin-allow-popups"
# Why not "no-referrer": with that policy browsers send "Origin: null" (and no Referer) on EVERY
# same-origin form POST (Fetch spec), which the CSRF origin check must reject, so every admin
# form would answer 403. "same-origin" keeps the real Origin / Referer on our own POSTs while
# sending nothing at all to other sites (the Telegram widget, outbound links).
REFERRER_POLICY = b"same-origin"
HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", REFERRER_POLICY),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"strict-transport-security", b"max-age=31536000"),
    (b"cache-control", b"no-store"),
]


class HostValidationMiddleware:
    """Only the configured admin domain is served. Anything else gets a bare 400."""

    def __init__(self, app, domain: str) -> None:
        self.app = app
        self.domain = domain

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and not self._ok(scope):
            if scope["type"] == "http":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 400,
                        "headers": [
                            (b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", b"12"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": b"Invalid host"})
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)

    def _ok(self, scope) -> bool:
        hosts = [v for k, v in scope["headers"] if k == b"host"]
        if len(hosts) != 1:
            return False
        try:
            match = _HOST_RE.fullmatch(hosts[0].decode("ascii").lower())
        except UnicodeDecodeError:
            return False
        return match is not None and match["host"] == self.domain


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        login_page = scope["path"] == "/login"  # only the page that hosts the Telegram widget

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message["headers"] if k not in _OWNED]
                headers += HEADERS
                csp = LOGIN_CSP if login_page else BASE_CSP
                if login_page:
                    headers = [
                        (k, LOGIN_COOP if k == b"cross-origin-opener-policy" else v)
                        for k, v in headers
                    ]
                headers.append((b"content-security-policy", csp.encode()))
                if scope["path"].startswith("/static/"):
                    headers = [(k, v) for k, v in headers if k != b"cache-control"]
                    headers.append((b"cache-control", b"public, max-age=3600"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


_OWNED = {k for k, _ in HEADERS} | {b"content-security-policy"}

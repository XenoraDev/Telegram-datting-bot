"""CSRF protection for every state-changing request.

The token is HMAC(server secret, session id): bound to the session, unguessable, never stored.
Compared in constant time. Origin / Referer, when the browser sends them, must be this site.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlsplit


def make_token(secret: str, sid: str) -> str:
    return hmac.new(secret.encode(), b"csrf:" + sid.encode(), hashlib.sha256).hexdigest()


def valid_token(secret: str, sid: str, supplied: str | None) -> bool:
    if not supplied or len(supplied) > 128:
        return False
    return hmac.compare_digest(make_token(secret, sid), supplied)


def origin_ok(domain: str, origin: str | None, referer: str | None) -> bool:
    """Reject requests that a browser says came from another site. Absent headers are fine
    (the token is the real defence); present-but-foreign ones are not."""
    for value in (origin, referer):
        if value is None:
            continue
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.hostname != domain:
            return False
    return True

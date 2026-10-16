"""Request dependencies: the single place where authorization and CSRF are enforced.

Every protected route depends on `current_admin` (or `admin_form`, which adds CSRF for POSTs).
The browser never tells us who is an admin; the cookie is just a random handle to a server-side
session that was only ever created after a verified Telegram login for an ADMIN_IDS member.
"""

from __future__ import annotations

import ipaddress
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import Depends, HTTPException, Request

from . import csrf
from .auth import Session
from .state import WebState

COOKIE = "__Host-admin_session"
MAX_FORM_BYTES = 8192
FORM_TYPE = "application/x-www-form-urlencoded"


class AuthRequired(Exception):
    """No valid admin session."""


def get_state(request: Request) -> WebState:
    return request.app.state.web


def current_admin(request: Request) -> Session:
    session = get_state(request).sessions.get(request.cookies.get(COOKIE))
    if session is None:
        raise AuthRequired
    return session


async def admin_form(request: Request, session: Annotated[Session, Depends(current_admin)]):
    """POST body of an authenticated admin, after the CSRF checks. Returns the form fields."""
    state = get_state(request)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype and ctype != FORM_TYPE:  # no body at all falls through to the CSRF check
        raise HTTPException(415)
    if not csrf.origin_ok(
        state.settings.admin_domain,
        request.headers.get("origin"),
        request.headers.get("referer"),
    ):
        raise HTTPException(403)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise HTTPException(413)
    fields = dict(
        parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=20)
    )
    if not csrf.valid_token(state.settings.session_secret, session.sid, fields.get("csrf_token")):
        raise HTTPException(403)
    return fields


def client_ip(request: Request, trust_proxy: bool) -> str:
    """Caddy appends the real client address as the LAST X-Forwarded-For entry."""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        last = forwarded.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


AdminSession = Annotated[Session, Depends(current_admin)]
AdminForm = Annotated[dict[str, str], Depends(admin_form)]
State = Annotated[WebState, Depends(get_state)]

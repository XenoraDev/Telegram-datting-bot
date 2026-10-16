"""Login (Telegram Login Widget), logout."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..auth import LoginError
from ..dependencies import COOKIE, AdminForm, State, client_ip
from ..ui import render

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/admin/", status_code=303)


@router.get("/login")
def login_page(request: Request, state: State):
    if state.sessions.get(request.cookies.get(COOKIE)) is not None:
        return RedirectResponse("/admin/", status_code=303)
    return render(request, "login.html", bot_username=state.settings.bot_username)


@router.get("/login/callback")
def login_callback(request: Request, state: State):
    """Telegram redirects here with the signed payload in the query string."""
    if not state.throttle.allow(client_ip(request, state.settings.trust_proxy)):
        log.warning("admin login throttled")
        return render(
            request,
            "error.html",
            status_code=429,
            title="Too many attempts",
            text="Too many login attempts. Wait a few minutes and try again.",
        )
    try:
        admin_id = state.verifier.verify(dict(request.query_params))
    except LoginError as exc:
        log.warning("admin login rejected: %s", exc)
        return render(
            request,
            "error.html",
            status_code=403,
            title="Login failed",
            text="This Telegram login was not accepted.",
        )
    state.sessions.destroy(request.cookies.get(COOKIE))
    session = state.sessions.create(admin_id)
    log.info("admin login ok")  # never log the callback payload, hash or ids
    response = RedirectResponse("/admin/", status_code=303)
    response.set_cookie(
        COOKIE,
        session.sid,
        max_age=int(state.settings.session_ttl),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout(request: Request, state: State, form: AdminForm):
    state.sessions.destroy(request.cookies.get(COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response

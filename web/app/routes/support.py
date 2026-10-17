"""Support tickets: list by status, read a thread, answer, close.

A user's text is shown escaped (Jinja autoescape) and only to a signed-in admin. An answer is
delivered to the user in Telegram, in their language, by WebState.notify. Every POST goes through
AdminForm (session + origin + CSRF); who may act is decided again in TicketService.
"""

from __future__ import annotations

from html import escape
from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import RedirectResponse

from bot.services.tickets import CATEGORIES, MAX_REPLY, STATUSES

from .. import params
from ..dependencies import AdminForm, AdminSession, State
from ..ui import redirect_url, render

router = APIRouter()
TicketId = Annotated[int, Path(ge=1, lt=params.MAX_ID)]

_FLASH = {
    "empty": "tk_empty",
    "too_long": "tk_too_long",
    "closed": "tk_closed",
    "full": "tk_full",
    "rate_limited": "rate",
    "not_found": "missing",
}
_LABEL = {
    "open": "waiting for support",
    "answered": "answered",
    "closed": "closed",
    "all": "all",
}


@router.get("/admin/support")
async def tickets(request: Request, session: AdminSession, state: State):
    status = request.query_params.get("status", "open")
    status = status if status in STATUSES else None  # "all" or anything else = every status
    page = await state.services.tickets.list_tickets(
        session.admin_id, status=status, page=params.page_no(request.query_params.get("page"))
    )
    return render(
        request,
        "support.html",
        session,
        active="support",
        page=page,
        status=status or "all",
        statuses=(*STATUSES, "all"),
        labels=_LABEL,
        categories=CATEGORIES,
    )


@router.get("/admin/support/{tid}")
async def ticket_detail(request: Request, tid: TicketId, session: AdminSession, state: State):
    detail = await state.services.tickets.detail(session.admin_id, tid)
    if detail is None:
        return render(
            request,
            "error.html",
            session,
            status_code=404,
            title="Not found",
            text="No such ticket.",
        )
    return render(
        request,
        "support_detail.html",
        session,
        active="support",
        labels=_LABEL,
        max_reply=MAX_REPLY,
        **detail,
    )


@router.post("/admin/support/{tid}/reply")
async def reply(tid: TicketId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.tickets.reply(session.admin_id, tid, form.get("reply"))
    if res.kind == "denied":
        return RedirectResponse("/admin/support", status_code=303)
    if res.kind == "ok":
        await state.notify(res.user_id, "support.admin_replied", id=tid, reply=escape(res.text))
    flash = "tk_replied" if res.kind == "ok" else _FLASH.get(res.kind, "missing")
    return RedirectResponse(redirect_url(f"/admin/support/{tid}", flash), status_code=303)


@router.post("/admin/support/{tid}/close")
async def close(tid: TicketId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.tickets.close(session.admin_id, tid)
    if res.kind == "denied":
        return RedirectResponse("/admin/support", status_code=303)
    if res.kind == "ok":
        await state.notify(res.user_id, "support.admin_closed", id=tid)
    flash = "tk_done" if res.kind == "ok" else _FLASH.get(res.kind, "missing")
    return RedirectResponse(redirect_url(f"/admin/support/{tid}", flash), status_code=303)

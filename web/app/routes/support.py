"""Support tickets: list by status, read a thread, answer, close.

A user's text is shown escaped (Jinja autoescape) and only to a signed-in admin. An answer is
delivered to the user in Telegram, in their language, by WebState.notify. Every POST goes through
AdminForm (session + origin + CSRF); who may act is decided again in TicketService.
"""

from __future__ import annotations

from datetime import UTC
from html import escape
from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse, RedirectResponse

from bot.services.tickets import CATEGORIES, MAX_REPLY, STATUSES

from .. import params
from ..dependencies import AdminForm, AdminSession, State
from ..ui import fmt_dt, redirect_url, render

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


def _msg(m) -> dict:
    """One message for the live page. Text only travels as JSON and is inserted with textContent."""
    when = m["created_at"].astimezone(UTC)
    return {
        "id": m["id"],
        "sender": m["sender"],
        "text": m["ticket_text"],
        "at": fmt_dt(m["created_at"]),
        "hm": when.strftime("%H:%M"),
        "admin": m["admin_id"],
    }


def _ticket(t) -> dict:
    return {
        "id": t["id"],
        "status": t["status"],
        "status_label": _LABEL[t["status"]],
        "updated": fmt_dt(t["updated_at"]),
        "closed": t["status"] == "closed",
    }


def _cursor(raw: str | None) -> int:
    return int(raw) if raw and raw.isascii() and raw.isdecimal() and len(raw) <= 15 else 0


@router.get("/admin/support/api/tickets")
async def api_tickets(request: Request, session: AdminSession, state: State):
    """The inbox list, refreshed by the page without a reload. Admin session required."""
    status = request.query_params.get("status", "open")
    status = status if status in STATUSES else None
    rows = await state.services.tickets.inbox(
        session.admin_id, status, params.page_no(request.query_params.get("page"))
    )
    return JSONResponse(
        {
            "tickets": [
                {
                    **_ticket(r),
                    "user": r["user_id"],
                    "topic": r["category"],
                    "preview": r["first_text"] or "",
                    "last_sender": r["last_sender"],
                    "last_id": r["last_id"] or 0,
                    "n": r["n_messages"],
                }
                for r in rows
            ]
        }
    )


@router.get("/admin/support/api/{tid}/messages")
async def api_messages(request: Request, tid: TicketId, session: AdminSession, state: State):
    """Messages of ONE ticket newer than ?after=<message id>. The ticket is looked up on the
    server and only an admin gets an answer; an unknown ticket is a plain 404."""
    data = await state.services.tickets.new_messages(
        session.admin_id, tid, _cursor(request.query_params.get("after"))
    )
    if data is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(
        {"ticket": _ticket(data["ticket"]), "messages": [_msg(m) for m in data["messages"]]}
    )


@router.post("/admin/support/api/{tid}/reply")
async def api_reply(tid: TicketId, session: AdminSession, state: State, form: AdminForm):
    """The same reply as the form, without leaving the page: JSON in, JSON out."""
    res = await state.services.tickets.reply(session.admin_id, tid, form.get("reply"))
    if res.kind != "ok":
        status = 403 if res.kind == "denied" else 404 if res.kind == "not_found" else 422
        return JSONResponse({"ok": False, "error": res.kind}, status_code=status)
    await _tell_user(state, res.user_id, tid, res.text)
    data = await state.services.tickets.new_messages(session.admin_id, tid, res.message_id - 1)
    sent = [m for m in data["messages"] if m["id"] == res.message_id]
    return JSONResponse({"ok": True, "message": _msg(sent[0]), "ticket": _ticket(data["ticket"])})


async def _tell_user(state, user_id: int, tid: int, text: str) -> None:
    """Deliver an answer in Telegram. If the user is ALREADY writing live to this ticket the
    answer just arrives (no second "Reply to support" button, no instructions); otherwise it
    carries the button bound to this exact ticket."""
    if await state.services.tickets.is_live(user_id, tid):
        await state.notify(user_id, "support.admin_replied_live", id=tid, reply=escape(text))
    else:
        await state.notify(
            user_id, "support.admin_replied", live_ticket=tid, id=tid, reply=escape(text)
        )


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
        inbox=await state.services.tickets.inbox(session.admin_id, status, page.page),
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
    status = request.query_params.get("status", "all")
    status = status if status in STATUSES else None
    return render(
        request,
        "support_detail.html",
        session,
        active="support",
        labels=_LABEL,
        max_reply=MAX_REPLY,
        inbox=await state.services.tickets.inbox(session.admin_id, status),
        status=status or "all",
        statuses=(*STATUSES, "all"),
        last_id=detail["messages"][-1]["id"] if detail["messages"] else 0,
        **detail,
    )


@router.post("/admin/support/{tid}/reply")
async def reply(tid: TicketId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.tickets.reply(session.admin_id, tid, form.get("reply"))
    if res.kind == "denied":
        return RedirectResponse("/admin/support", status_code=303)
    if res.kind == "ok":
        await _tell_user(state, res.user_id, tid, res.text)
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

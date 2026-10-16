"""Read-only view of the append-only audit log. There is deliberately no POST route here."""

from __future__ import annotations

from fastapi import APIRouter, Request

from bot.services.admin import AUDIT_ACTIONS

from .. import params
from ..dependencies import AdminSession, State
from ..ui import render

router = APIRouter()


@router.get("/admin/audit")
async def audit(request: Request, session: AdminSession, state: State):
    q = request.query_params
    since, until = params.day(q.get("from")), params.day(q.get("to"))
    page = await state.services.admin.audit_page(
        session.admin_id,
        action=q.get("action"),
        actor=params.int_id(q.get("admin")),
        target=params.int_id(q.get("user")),
        since=since,
        until=params.next_day(until) if until else None,
        page=params.page_no(q.get("page")),
    )
    return render(
        request,
        "audit.html",
        session,
        active="audit",
        page=page,
        actions=AUDIT_ACTIONS,
        f={k: q.get(k, "") for k in ("action", "admin", "user", "from", "to")},
    )

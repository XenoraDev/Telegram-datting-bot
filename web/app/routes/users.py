"""Users: search, list, detail, ban (with confirmation) and unban."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import RedirectResponse

from bot.services.admin import USER_STATUSES

from .. import params
from ..dependencies import AdminForm, AdminSession, State
from ..ui import redirect_url, render

router = APIRouter()
UserId = Annotated[int, Path(ge=1, lt=params.MAX_ID)]

# ActionResult.kind -> flash code
_FLASH = {
    "ok": None,
    "protected": "protected",
    "invalid_state": "state",
    "rate_limited": "rate",
    "not_found": "missing",
    "already_handled": "handled",
}


@router.get("/admin/users")
async def users(request: Request, session: AdminSession, state: State):
    q = request.query_params
    query = (q.get("q") or "").strip()[:64]
    status = q.get("status") if q.get("status") in USER_STATUSES else None
    uid = params.int_id(query)
    public_id = None if uid else await state.services.admin.find_profile(session.admin_id, query)
    bad_query = bool(query) and uid is None and public_id is None
    page = await state.services.admin.users_page(
        session.admin_id,
        uid=uid,
        public_id=public_id,
        status=None if bad_query else status,
        page=params.page_no(q.get("page")),
    )
    if bad_query:
        page = type(page)([], 0, 0, 1)
    return render(
        request,
        "users.html",
        session,
        active="users",
        page=page,
        statuses=USER_STATUSES,
        q=query,
        status=status or "",
        bad_query=bad_query,
    )


@router.get("/admin/users/{uid}")
async def user_detail(request: Request, uid: UserId, session: AdminSession, state: State):
    detail = await state.services.admin.user_detail(session.admin_id, uid)
    if detail is None:
        return render(
            request, "error.html", session, status_code=404, title="Not found", text="No such user."
        )
    return render(
        request,
        "user_detail.html",
        session,
        active="users",
        is_admin_target=state.services.admin.is_admin(uid),
        **detail,
    )


@router.get("/admin/users/{uid}/ban")
async def ban_confirm(request: Request, uid: UserId, session: AdminSession, state: State):
    """A confirmation page only; GET never changes anything."""
    detail = await state.services.admin.user_detail(session.admin_id, uid)
    if detail is None:
        return render(
            request, "error.html", session, status_code=404, title="Not found", text="No such user."
        )
    return render(
        request,
        "ban_confirm.html",
        session,
        active="users",
        user=detail["user"],
        action=f"/admin/users/{uid}/ban",
        back=f"/admin/users/{uid}",
        report_id=None,
    )


async def _ban(state, session, form, uid: int, report_id: int | None, ok_path: str):
    res = await state.services.admin.ban(
        session.admin_id, uid, report_id=report_id, note=form.get("note")
    )
    if res.kind == "denied":
        return None
    await state.notify_partner(res.partner_id)
    return redirect_url(ok_path, _FLASH.get(res.kind, "state") or "banned")


@router.post("/admin/users/{uid}/ban")
async def ban(uid: UserId, session: AdminSession, state: State, form: AdminForm):
    target = await _ban(state, session, form, uid, None, f"/admin/users/{uid}")
    return RedirectResponse(target or "/admin/users", status_code=303)


@router.post("/admin/users/{uid}/unban")
async def unban(uid: UserId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.admin.unban(session.admin_id, uid)
    return RedirectResponse(
        redirect_url(f"/admin/users/{uid}", _FLASH.get(res.kind, "state") or "unbanned"),
        status_code=303,
    )

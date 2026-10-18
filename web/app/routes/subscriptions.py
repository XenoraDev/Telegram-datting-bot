"""Subscription management (S2): cancel one subscription, revoke an approved payment's subscription.

Both are two steps: a GET confirmation page (changes nothing) and a POST (admin session + CSRF + our
origin, a ticked confirmation and a written reason). The target is resolved on the server from the
id in the URL: the form carries no user id, so there is nothing else to tamper with.
"""

from __future__ import annotations

from html import escape
from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import RedirectResponse

from .. import params
from ..dependencies import AdminForm, AdminSession, State
from ..ui import redirect_url, render

router = APIRouter()
SubId = Annotated[int, Path(ge=1, lt=params.MAX_ID)]
PaymentId = Annotated[int, Path(ge=1, lt=params.MAX_ID)]

_FLASH = {
    "confirm_required": "sub_confirm",
    "reason_required": "sub_reason",
    "not_active": "sub_not_active",
    "already_ended": "sub_not_active",
    "not_approved": "sub_not_approved",
    "no_subscription": "sub_no_link",
    "ambiguous": "sub_ambiguous",
    "rate_limited": "rate",
    "not_found": "missing",
}


def _not_found(request: Request, session):
    return render(
        request, "error.html", session, status_code=404, title="Not found", text="Not found."
    )


@router.get("/admin/subscriptions/{sid}/cancel")
async def cancel_confirm(request: Request, sid: SubId, session: AdminSession, state: State):
    """A confirmation page only; GET never changes anything."""
    preview = await state.services.subs.preview_cancel(session.admin_id, sid)
    if preview is None:
        return _not_found(request, session)
    sub = preview["sub"]
    return render(
        request,
        "subscription_cancel.html",
        session,
        active="users",
        sub=sub,
        others=preview["others"],
        action=f"/admin/subscriptions/{sid}/cancel",
        back=f"/admin/users/{sub['user_id']}",
    )


@router.post("/admin/subscriptions/{sid}/cancel")
async def cancel(sid: SubId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.subs.cancel(
        session.admin_id, sid, form.get("reason"), confirmed=form.get("confirm") == "1"
    )
    if res.kind == "denied":
        return RedirectResponse("/admin/users", status_code=303)
    if res.kind == "ok":
        queued = res.phase == "queued"
        await state.notify(
            res.user_id,
            "sub.canceled_queued" if queued else "sub.canceled",
            plan=escape(res.plan_name),
            date=res.starts_at.strftime("%d-%m-%Y"),
        )
        return RedirectResponse(
            redirect_url(f"/admin/users/{res.user_id}", "sub_canceled"), status_code=303
        )
    if res.kind in ("confirm_required", "reason_required"):  # back to the form, nothing changed
        return RedirectResponse(
            redirect_url(f"/admin/subscriptions/{sid}/cancel", _FLASH[res.kind]), status_code=303
        )
    return RedirectResponse(
        redirect_url("/admin/users", _FLASH.get(res.kind, "missing")), status_code=303
    )


@router.get("/admin/payments/{pid}/revoke")
async def revoke_confirm(request: Request, pid: PaymentId, session: AdminSession, state: State):
    """A confirmation page only; GET never changes anything."""
    check = await state.services.subs.check_revoke(session.admin_id, pid)
    if check is None or check.problem == "not_found":
        return _not_found(request, session)
    return render(
        request,
        "payment_revoke.html",
        session,
        active="payments",
        check=check,
        pid=pid,
        action=f"/admin/payments/{pid}/revoke",
        back=f"/admin/payments/{pid}",
    )


@router.post("/admin/payments/{pid}/revoke")
async def revoke(pid: PaymentId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.subs.revoke_payment(
        session.admin_id, pid, form.get("reason"), confirmed=form.get("confirm") == "1"
    )
    if res.kind == "denied":
        return RedirectResponse("/admin/payments", status_code=303)
    if res.kind == "ok":
        await state.notify(
            res.user_id,
            "sub.revoked",
            plan=escape(res.plan_name),
            reference=res.reference,
            date=res.starts_at.strftime("%d-%m-%Y"),
        )
        return RedirectResponse(
            redirect_url(f"/admin/payments/{pid}", "sub_revoked"), status_code=303
        )
    if res.kind in ("confirm_required", "reason_required"):
        return RedirectResponse(
            redirect_url(f"/admin/payments/{pid}/revoke", _FLASH[res.kind]), status_code=303
        )
    return RedirectResponse(
        redirect_url(f"/admin/payments/{pid}", _FLASH.get(res.kind, "missing")), status_code=303
    )

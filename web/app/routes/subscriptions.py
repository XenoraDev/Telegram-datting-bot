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
    "changed": "sub_changed",
    "too_much": "sub_too_much",
    "bad_days": "sub_bad_days",
    "no_end": "sub_no_end",
    "overlap": "sub_overlap",
    "already_removed": "sub_removed",
    "user_inactive": "sub_user_inactive",
    "plan_unavailable": "sub_plan_unavailable",
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


# ------------------------------------------------------------------ S3: shorten / remove / grant
# Every change is two steps again: a GET preview that changes nothing, then a POST (admin session,
# CSRF, our origin, a ticked confirmation, a written reason). The target comes from the URL; the
# only other thing the form carries is the end date / state the admin was shown, so a stale page
# or a double submit changes nothing.

UserId = Annotated[int, Path(ge=1, lt=params.MAX_ID)]
MAX_DAYS_FIELD = 9  # digits: a bigger number is junk, it is cut off before it reaches the service


def _days(raw: str | None) -> int:
    raw = (raw or "").strip()
    return int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= MAX_DAYS_FIELD else 0


async def _told(state, res, key: str, **values) -> None:
    await state.notify(res.user_id, key, **values)


@router.get("/admin/subscriptions/{sid}/shorten")
async def shorten_form(request: Request, sid: SubId, session: AdminSession, state: State):
    """The form and, once a number of days is given, the exact before / after. Changes nothing."""
    days = _days(request.query_params.get("days"))
    check = await state.services.subs.preview_shorten(session.admin_id, sid, days or 1)
    if check is None or check.problem == "not_found":
        return _not_found(request, session)
    return render(
        request,
        "subscription_shorten.html",
        session,
        active="users",
        check=check,
        asked=bool(days),
        days=days,
        sid=sid,
        back=f"/admin/users/{check.sub['user_id']}",
    )


@router.post("/admin/subscriptions/{sid}/shorten")
async def shorten(sid: SubId, session: AdminSession, state: State, form: AdminForm):
    days = _days(form.get("days"))
    res = await state.services.subs.shorten(
        session.admin_id,
        sid,
        days,
        form.get("reason"),
        confirmed=form.get("confirm") == "1",
        expected=form.get("expected") or "",
    )
    if res.kind == "denied":
        return RedirectResponse("/admin/users", status_code=303)
    if res.kind == "ok":
        await _told(
            state,
            res,
            "sub.shortened",
            plan=escape(res.plan_name),
            date=res.ends_at.strftime("%d-%m-%Y"),
        )
        return RedirectResponse(
            redirect_url(f"/admin/users/{res.user_id}", "sub_shortened"), status_code=303
        )
    key = _FLASH.get(res.kind, "missing")
    if res.kind in ("confirm_required", "reason_required", "changed", "too_much", "bad_days"):
        return RedirectResponse(
            redirect_url(f"/admin/subscriptions/{sid}/shorten?days={days}", key), status_code=303
        )
    return RedirectResponse(redirect_url("/admin/users", key), status_code=303)


@router.get("/admin/payments/{pid}/remove-extension")
async def remove_extension_form(
    request: Request, pid: PaymentId, session: AdminSession, state: State
):
    check = await state.services.subs.preview_remove_extension(session.admin_id, pid)
    if check is None or check.problem == "not_found":
        return _not_found(request, session)
    return render(
        request,
        "payment_remove_extension.html",
        session,
        active="payments",
        check=check,
        pid=pid,
        back=f"/admin/payments/{pid}",
    )


@router.post("/admin/payments/{pid}/remove-extension")
async def remove_extension(pid: PaymentId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.subs.remove_extension(
        session.admin_id,
        pid,
        form.get("reason"),
        confirmed=form.get("confirm") == "1",
        expected=form.get("expected") or "",
    )
    if res.kind == "denied":
        return RedirectResponse("/admin/payments", status_code=303)
    if res.kind == "ok":
        await _told(
            state,
            res,
            "sub.shortened",
            plan=escape(res.plan_name),
            date=res.ends_at.strftime("%d-%m-%Y"),
        )
        return RedirectResponse(
            redirect_url(f"/admin/users/{res.user_id}", "sub_shortened"), status_code=303
        )
    key = _FLASH.get(res.kind, "missing")
    if res.kind in ("confirm_required", "reason_required", "changed", "too_much"):
        return RedirectResponse(
            redirect_url(f"/admin/payments/{pid}/remove-extension", key), status_code=303
        )
    return RedirectResponse(redirect_url(f"/admin/payments/{pid}", key), status_code=303)


@router.get("/admin/users/{uid}/grant")
async def grant_form(request: Request, uid: UserId, session: AdminSession, state: State):
    plans = await state.services.subs.grantable_plans(session.admin_id)
    plan_id = params.int_id(request.query_params.get("plan")) or 0
    days = _days(request.query_params.get("days"))
    probe = await state.services.subs.preview_grant(session.admin_id, uid, plan_id, days or 1)
    if probe is None or probe.problem == "not_found":  # not an admin, or no such account
        return _not_found(request, session)
    check = probe if plan_id and days else None  # a preview only once a choice was made
    return render(
        request,
        "subscription_grant.html",
        session,
        active="users",
        check=check,
        plans=plans,
        plan_id=plan_id,
        days=days,
        uid=uid,
        back=f"/admin/users/{uid}",
    )


@router.post("/admin/users/{uid}/grant")
async def grant(uid: UserId, session: AdminSession, state: State, form: AdminForm):
    plan_id = params.int_id(form.get("plan")) or 0
    days = _days(form.get("days"))
    res = await state.services.subs.grant(
        session.admin_id,
        uid,
        plan_id,
        days,
        form.get("reason"),
        confirmed=form.get("confirm") == "1",
        expected=form.get("expected") or "",
    )
    if res.kind == "denied":
        return RedirectResponse("/admin/users", status_code=303)
    if res.kind == "ok":
        date = res.ends_at.strftime("%d-%m-%Y")
        if res.mode == "queued":
            await _told(
                state,
                res,
                "sub.granted_queued",
                plan=escape(res.plan_name),
                start=res.starts_at.strftime("%d-%m-%Y"),
                date=date,
            )
        else:
            await _told(state, res, "sub.granted", plan=escape(res.plan_name), date=date)
        return RedirectResponse(redirect_url(f"/admin/users/{uid}", "sub_granted"), status_code=303)
    key = _FLASH.get(res.kind, "missing")
    if res.kind == "not_found":
        return RedirectResponse(redirect_url("/admin/users", key), status_code=303)
    return RedirectResponse(
        redirect_url(f"/admin/users/{uid}/grant?plan={plan_id}&days={days}", key), status_code=303
    )

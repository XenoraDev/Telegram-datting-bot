"""Payment review: list, detail with the screenshot, approve / reject.

The screenshot is evidence for a human. Approving is the only thing that activates premium, and
it runs in PaymentService.approve (one transaction, payment row locked, still-pending re-check).
"""

from __future__ import annotations

from html import escape
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from fastapi.responses import RedirectResponse

from bot.services.payments import STATUSES

from .. import params
from ..dependencies import AdminForm, AdminSession, State
from ..ui import redirect_url, render

router = APIRouter()
PaymentId = Annotated[int, Path(ge=1, lt=params.MAX_ID)]

_FLASH = {
    "already_handled": "pay_handled",
    "reason_required": "pay_reason",
    "user_inactive": "pay_user_inactive",
    "plan_inactive": "pay_plan_inactive",
    "duplicate_utr": "pay_duplicate_utr",
    "no_evidence": "pay_no_evidence",
    "rate_limited": "rate",
    "not_found": "missing",
}


@router.get("/admin/payments")
async def payments(request: Request, session: AdminSession, state: State):
    q = request.query_params
    status = q.get("status", "pending")
    status = status if status in STATUSES else None  # "all" or anything else = every status
    page = await state.services.payments.list_payments(
        session.admin_id, status=status, page=params.page_no(q.get("page"))
    )
    return render(
        request,
        "payments.html",
        session,
        active="payments",
        page=page,
        status=status or "all",
        statuses=STATUSES,
    )


async def _detail_or_404(request: Request, session, state, pid: int):
    detail = await state.services.payments.detail(session.admin_id, pid)
    if detail is None:
        return None, render(
            request,
            "error.html",
            session,
            status_code=404,
            title="Not found",
            text="No such payment.",
        )
    return detail, None


@router.get("/admin/payments/{pid}")
async def payment_detail(request: Request, pid: PaymentId, session: AdminSession, state: State):
    detail, missing = await _detail_or_404(request, session, state, pid)
    if missing is not None:
        return missing
    return render(request, "payment_detail.html", session, active="payments", **detail)


@router.get("/admin/payments/{pid}/evidence")
async def payment_evidence(pid: PaymentId, session: AdminSession, state: State):
    """The stored screenshot, served with the type decided by content validation at upload."""
    found = await state.services.payments.evidence(session.admin_id, pid)
    if found is None:
        return Response(status_code=404)
    content_type, data = found
    return Response(
        data,
        media_type=content_type,
        headers={"Content-Disposition": 'inline; filename="evidence"'},
    )


@router.post("/admin/payments/{pid}/approve")
async def approve(pid: PaymentId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.payments.approve(session.admin_id, pid)
    if res.kind == "denied":
        return RedirectResponse("/admin/payments", status_code=303)
    if res.kind == "ok":
        queued = res.mode == "queued"
        await state.notify(
            res.user_id,
            "premium.queued" if queued else "premium.approved",
            reference=res.reference,
            date=res.period_end.strftime("%d-%m-%Y"),
            start=res.starts_at.strftime("%d-%m-%Y"),
        )
    flash = "pay_approved" if res.kind == "ok" else _FLASH.get(res.kind, "pay_handled")
    return RedirectResponse(redirect_url(f"/admin/payments/{pid}", flash), status_code=303)


@router.post("/admin/payments/{pid}/reject")
async def reject(pid: PaymentId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.payments.reject(session.admin_id, pid, form.get("reason"))
    if res.kind == "denied":
        return RedirectResponse("/admin/payments", status_code=303)
    if res.kind == "ok":
        await state.notify(
            res.user_id, "premium.rejected", reference=res.reference, reason=escape(res.reason)
        )
    flash = "pay_rejected" if res.kind == "ok" else _FLASH.get(res.kind, "pay_handled")
    return RedirectResponse(redirect_url(f"/admin/payments/{pid}", flash), status_code=303)

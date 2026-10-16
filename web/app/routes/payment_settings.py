"""Payment configuration (UPI destination) and the plans users can buy."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import RedirectResponse

from bot.services.upi import format_inr

from ..dependencies import AdminForm, AdminSession, State
from ..ui import redirect_url, render

router = APIRouter()
PlanId = Annotated[int, Path(ge=1, lt=2**31)]


def _flash(res, ok: str) -> str:
    if res.kind == "ok":
        return ok
    if res.kind == "invalid":
        return f"bad_{res.field}"
    return {"exists": "plan_exists", "not_found": "missing", "rate_limited": "rate"}.get(
        res.kind, "missing"
    )


@router.get("/admin/payment-settings")
async def settings_page(request: Request, session: AdminSession, state: State):
    settings = await state.services.payments.get_settings(session.admin_id)
    return render(request, "payment_settings.html", session, active="pay_settings", s=settings)


@router.post("/admin/payment-settings")
async def save_settings(session: AdminSession, state: State, form: AdminForm):
    res = await state.services.payments.save_settings(
        session.admin_id,
        upi_id=form.get("upi_id", ""),
        payee_name=form.get("payee_name", ""),
        instructions=form.get("instructions", ""),
        active=form.get("active") == "1",
    )
    return RedirectResponse(
        redirect_url("/admin/payment-settings", _flash(res, "settings_saved")), status_code=303
    )


@router.get("/admin/plans")
async def plans_page(request: Request, session: AdminSession, state: State):
    plans = await state.services.payments.plans(session.admin_id)
    return render(
        request, "plans.html", session, active="plans", plans=plans, format_inr=format_inr
    )


@router.post("/admin/plans")
async def create_plan(session: AdminSession, state: State, form: AdminForm):
    res = await state.services.payments.save_plan(
        session.admin_id,
        None,
        code=form.get("code", ""),
        name=form.get("name", ""),
        days=form.get("days", ""),
        price=form.get("price", ""),
        active=form.get("active") == "1",
    )
    return RedirectResponse(
        redirect_url("/admin/plans", _flash(res, "plan_saved")), status_code=303
    )


@router.post("/admin/plans/{plan_id}")
async def edit_plan(plan_id: PlanId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.payments.save_plan(
        session.admin_id,
        plan_id,
        code="",
        name=form.get("name", ""),
        days=form.get("days", ""),
        price=form.get("price", ""),
        active=form.get("active") == "1",
    )
    return RedirectResponse(
        redirect_url("/admin/plans", _flash(res, "plan_saved")), status_code=303
    )

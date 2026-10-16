"""Tiers & limits: the entitlements of Free / Premium / Pro / VIP, editable without code."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import RedirectResponse

from bot.services.tier_admin import ALL_TIERS, CAPABILITIES, LIMITS

from ..dependencies import AdminForm, AdminSession, State
from ..ui import redirect_url, render

router = APIRouter()
TierName = Annotated[str, Path(pattern="^(free|premium|pro|vip)$")]

_BAD = {
    "random_chat_limit": "bad_limit",
    "browse_limit": "bad_limit",
    "swipe_limit": "bad_limit",
    "match_priority": "bad_priority",
    "plan_badge": "bad_badge",
}


def _flash(res, ok: str) -> str:
    if res.kind == "ok":
        return ok
    if res.kind == "invalid":
        return _BAD.get(res.field or "", "bad_mode")
    return {"rate_limited": "rate"}.get(res.kind, "missing")


@router.get("/admin/tiers")
async def tiers_page(request: Request, session: AdminSession, state: State):
    views = await state.services.tiers.overview(session.admin_id)
    return render(
        request,
        "tiers.html",
        session,
        active="tiers",
        views=views,
        limits=LIMITS,
        capabilities=CAPABILITIES,
        all_tiers=ALL_TIERS,
    )


@router.post("/admin/tiers/{tier}")
async def save_tier(tier: TierName, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.tiers.save(session.admin_id, tier, form)
    if res.kind == "denied":
        return RedirectResponse("/admin/", status_code=303)
    return RedirectResponse(
        redirect_url("/admin/tiers", _flash(res, "tier_saved")) + f"#tier-{tier}", status_code=303
    )


@router.post("/admin/tiers/{tier}/reset")
async def reset_tier(tier: TierName, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.tiers.reset(session.admin_id, tier)
    if res.kind == "denied":
        return RedirectResponse("/admin/", status_code=303)
    return RedirectResponse(
        redirect_url("/admin/tiers", _flash(res, "tier_reset")) + f"#tier-{tier}", status_code=303
    )

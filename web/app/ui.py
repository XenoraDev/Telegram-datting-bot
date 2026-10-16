"""Rendering helpers shared by the routes."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse

from bot.services.upi import format_inr

from . import csrf
from .auth import Session
from .dependencies import get_state

# Flash messages are chosen by a fixed code, never echoed from the request.
FLASH = {
    "banned": ("ok", "User banned."),
    "unbanned": ("ok", "User unbanned."),
    "resolved": ("ok", "Report resolved."),
    "dismissed": ("ok", "Report dismissed."),
    "protected": ("err", "Admins cannot be banned from the panel."),
    "state": ("err", "That user is not in a state where this action applies."),
    "handled": ("err", "That report was already handled."),
    "rate": ("err", "Too many actions in a short time. Wait a moment and retry."),
    "missing": ("err", "Not found."),
    "pay_approved": ("ok", "Payment approved. Premium is active for the user."),
    "pay_rejected": ("ok", "Payment rejected. The user has been told why."),
    "pay_handled": ("err", "That payment was already handled."),
    "pay_reason": ("err", "A reason of at least 3 characters is required to reject."),
    "pay_user_inactive": ("err", "That account is not active (banned / deleted); reject instead."),
    "pay_plan_inactive": ("err", "That plan is switched off; switch it on first or reject."),
    "pay_duplicate_utr": ("err", "This UTR already backs another approved payment."),
    "pay_no_evidence": ("err", "No screenshot is stored for this payment."),
    "settings_saved": ("ok", "Payment settings saved."),
    "plan_saved": ("ok", "Plan saved."),
    "plan_exists": ("err", "A plan with that code already exists."),
    "bad_upi_id": (
        "err",
        "Enter a valid UPI ID (like name@bank); it is needed to switch payments on.",
    ),
    "bad_payee_name": ("err", "A payee name is needed to switch payments on."),
    "bad_code": ("err", "Plan code: lower-case letters, digits and _ (starts with a letter)."),
    "bad_name": ("err", "Enter a plan name."),
    "bad_days": ("err", "Days must be a whole number from 1 to 3660."),
    "bad_tier": ("err", "Choose a tier: premium, pro or vip."),
    "bad_price": ("err", "Price must be a positive amount in rupees, up to 50000, 2 decimals max."),
}
REPORT_LABEL = {"open": "open", "actioned": "resolved", "dismissed": "dismissed"}
PAYMENT_LABEL = {
    "pending": "pending review",
    "approved": "approved",
    "rejected": "rejected",
    "cancelled": "cancelled",
}
NAV = (
    ("dashboard", "/admin/", "Dashboard"),
    ("users", "/admin/users", "Users"),
    ("reports", "/admin/reports", "Reports"),
    ("payments", "/admin/payments", "Payments"),
    ("plans", "/admin/plans", "Plans"),
    ("pay_settings", "/admin/payment-settings", "Payment settings"),
    ("statistics", "/admin/statistics", "Statistics"),
    ("audit", "/admin/audit", "Audit log"),
)


def fmt_dt(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "—"


def render(
    request: Request,
    name: str,
    session: Session | None = None,
    *,
    status_code: int = 200,
    active: str = "",
    **context,
) -> HTMLResponse:
    state = get_state(request)
    flash = FLASH.get(request.query_params.get("msg", ""))
    base = {
        "nav": NAV,
        "active": active,
        "admin_id": session.admin_id if session else None,
        "csrf": csrf.make_token(state.settings.session_secret, session.sid) if session else "",
        "flash": flash,
        "report_label": REPORT_LABEL,
        "payment_label": PAYMENT_LABEL,
        "fmt_dt": fmt_dt,
        "inr": inr,
    }
    return state.templates.TemplateResponse(
        request, name, {**base, **context}, status_code=status_code
    )


def redirect_url(path: str, msg: str | None = None) -> str:
    """Server-built redirect target (path is always one of our own routes)."""
    return f"{path}?msg={quote(msg)}" if msg else path


def inr(minor: int) -> str:
    return "₹" + format_inr(minor)

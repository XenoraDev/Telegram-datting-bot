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
    "sub_canceled": ("ok", "Subscription canceled. The user was told. Nothing was deleted."),
    "sub_revoked": (
        "ok",
        "Approval revoked: its subscription was canceled and the user was told. "
        "No refund was made or recorded.",
    ),
    "sub_confirm": ("err", "Tick the box to confirm. Nothing was changed."),
    "sub_reason": ("err", "A reason of at least 3 characters is required. Nothing was changed."),
    "sub_not_active": (
        "err",
        "That subscription is not running or queued any more. Nothing changed.",
    ),
    "sub_not_approved": ("err", "Only an approved payment can be revoked."),
    "sub_no_link": ("err", "That payment is not linked to a subscription. Nothing was changed."),
    "sub_ambiguous": (
        "err",
        "That payment's subscription is shared with other payments, so it was not touched.",
    ),
    "sub_shortened": (
        "ok",
        "Time removed. Queued subscriptions moved with it, the user was told, and the "
        "change is in the audit log. Nothing was deleted and no payment was changed.",
    ),
    "sub_granted": (
        "ok",
        "Plan granted. The user was told. No payment was created; the change is in the audit log.",
    ),
    "sub_changed": (
        "err",
        "That user's subscriptions changed after you opened the page (or this was already done). "
        "Nothing was changed; check the current state and try again.",
    ),
    "sub_too_much": (
        "err",
        "That would leave the subscription with no time. Take off less, or cancel it instead.",
    ),
    "sub_bad_days": ("err", "Enter a whole number of days in the allowed range."),
    "sub_no_end": ("err", "That subscription has no end date, so it can not be adjusted."),
    "sub_overlap": (
        "err",
        "That subscription overlaps another one, which should not happen. Nothing was changed.",
    ),
    "sub_removed": ("err", "That payment's extension was already removed. Nothing was changed."),
    "sub_user_inactive": ("err", "That account is not active (banned / deleted)."),
    "sub_plan_unavailable": ("err", "That plan is not available to grant (off or not for sale)."),
    "auto_on": (
        "ok",
        "Automatic payment approval is ON. Only fully verified payments approve themselves.",
    ),
    "auto_off": ("ok", "Automatic payment approval is OFF. Every payment waits for you again."),
    "bad_auto_confirm": ("err", "Tick the box to confirm before switching automatic approval on."),
    "bad_auto_payments_off": (
        "err",
        "Set up and switch on payments (UPI ID and payee) before enabling automatic approval.",
    ),
    "plan_saved": ("ok", "Plan saved."),
    "plan_defaults": (
        "ok",
        "Premium, Pro and VIP plans are in place. Set a length and price, then switch a plan "
        "on to sell it.",
    ),
    "tier_saved": (
        "ok",
        "Tier saved. Every plan of this tier now has these limits and capabilities.",
    ),
    "tier_reset": ("ok", "Tier reset to the built-in defaults."),
    "bad_limit": (
        "err",
        "A daily limit must be a whole number from 0 to 1,000,000 (or choose Unlimited).",
    ),
    "bad_mode": ("err", "Choose Limited or Unlimited for every daily limit."),
    "bad_priority": ("err", "Ranking priority must be a whole number from 0 to 100."),
    "bad_badge": ("err", "Badge must be none, Premium, Pro or VIP."),
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
    "lt_sent": (
        "ok",
        "Confirmation sent to your Telegram. The test starts only after you press CONFIRM there; "
        "it expires in 5 minutes.",
    ),
    "lt_blocked": (
        "err",
        "🚫 LOAD TEST BLOCKED: production or unknown environment detected. See Safety Status.",
    ),
    "lt_busy": ("err", "🚫 Another load test is already running."),
    "lt_telegram": (
        "err",
        "The confirmation could not be delivered. Open the bot in Telegram, press Start, retry.",
    ),
    "lt_stop": (
        "ok",
        "Stop requested. The worker winds the test down gracefully and writes a partial report.",
    ),
    "lt_cancelled": ("ok", "The test had not started yet and was cancelled."),
    "lt_not_active": ("err", "That test is not running."),
    "lt_thresholds": ("ok", "Thresholds saved. They apply to tests started from now on."),
    "lt_bad_thresholds": ("err", "Some thresholds are invalid. Nothing was saved."),
    "lt_withdrawn": ("ok", "Confirmation withdrawn. Nothing will start."),
    "tk_replied": ("ok", "Reply sent. The user has been told in Telegram."),
    "tk_done": ("ok", "Ticket closed. The user has been told."),
    "tk_empty": ("err", "Write a reply first."),
    "tk_too_long": ("err", "That reply is too long (800 characters at most)."),
    "tk_closed": ("err", "That ticket is already closed."),
    "tk_full": ("err", "This ticket has too many messages; close it."),
    "bad_price": ("err", "Price must be a positive amount in rupees, up to 50000, 2 decimals max."),
}
EVIDENCE_VERDICT = {
    "VALIDATION_OK": ("ok", "Looks consistent"),
    "VALIDATION_WARNING": ("warn", "Check carefully"),
    "VALIDATION_MISMATCH": ("bad", "Does not match the order"),
    "VALIDATION_UNREADABLE": ("unread", "Could not be read"),
}
EVIDENCE_FINDING = {
    "amount_mismatch": ("bad", "The amount on the screenshot is different from the order."),
    "amount_not_found": ("warn", "No clear rupee amount could be read."),
    "amount_unclear": (
        "warn",
        "The amount is unclear (several amounts, or the rupee sign was read as a digit).",
    ),
    "payee_mismatch": ("bad", "A different UPI ID than the one on the order is shown."),
    "payee_unclear": (
        "warn",
        "A UPI ID very similar to the order's is shown (misread or lookalike).",
    ),
    "payee_not_found": ("warn", "The payee (UPI ID or name) could not be read."),
    "payment_failed": ("bad", "The screenshot says the payment failed, was declined or reversed."),
    "payment_pending": ("warn", "The screenshot says the payment is pending / processing."),
    "success_not_shown": ("warn", "No success wording or reference number was read."),
    "time_too_old": ("warn", "The printed date/time is earlier than this order was created."),
    "time_in_future": ("warn", "The printed date/time is in the future."),
    "no_text": ("warn", "Almost no text could be read (a photo, a blank or a very small image)."),
    "no_payment_info": ("warn", "Text was read but nothing that looks like a payment."),
    "ocr_unavailable": ("warn", "Text reading is not installed on the bot server."),
    "ocr_timeout": ("warn", "Reading the image took too long."),
    "ocr_too_large": ("warn", "The image is too large to read."),
    "ocr_failed": ("warn", "Reading the image failed."),
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
    ("support", "/admin/support", "Support"),
    ("plans", "/admin/plans", "Plans"),
    ("tiers", "/admin/tiers", "Tiers & limits"),
    ("loadtest", "/admin/loadtest", "Server capacity"),
    ("pay_settings", "/admin/payment-settings", "Payment settings"),
    ("statistics", "/admin/statistics", "Statistics"),
    ("insights", "/admin/insights", "Queue & funnel"),
    ("audit", "/admin/audit", "Audit log"),
)


def spark(values: list[float | None], width: int = 320, height: int = 48) -> str:
    """SVG polyline points for a small trend chart ('' when there is nothing to draw)."""
    pts = [float(v) for v in values if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    step = width / (len(pts) - 1)
    return " ".join(
        f"{i * step:.1f},{height - 3 - (v - lo) / span * (height - 6):.1f}"
        for i, v in enumerate(pts)
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
        "evidence_verdict": EVIDENCE_VERDICT,
        "evidence_finding": EVIDENCE_FINDING,
        "fmt_dt": fmt_dt,
        "spark": spark,
        "inr": inr,
    }
    return state.templates.TemplateResponse(
        request, name, {**base, **context}, status_code=status_code
    )


def redirect_url(path: str, msg: str | None = None) -> str:
    """Server-built redirect target (path is always one of our own routes)."""
    if not msg:
        return path
    return f"{path}{'&' if '?' in path else '?'}msg={quote(msg)}"


def inr(minor: int) -> str:
    return "₹" + format_inr(minor)

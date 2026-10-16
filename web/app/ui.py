"""Rendering helpers shared by the routes."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse

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
}
REPORT_LABEL = {"open": "open", "actioned": "resolved", "dismissed": "dismissed"}
NAV = (
    ("dashboard", "/admin/", "Dashboard"),
    ("users", "/admin/users", "Users"),
    ("reports", "/admin/reports", "Reports"),
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
        "fmt_dt": fmt_dt,
    }
    return state.templates.TemplateResponse(
        request, name, {**base, **context}, status_code=status_code
    )


def redirect_url(path: str, msg: str | None = None) -> str:
    """Server-built redirect target (path is always one of our own routes)."""
    return f"{path}?msg={quote(msg)}" if msg else path

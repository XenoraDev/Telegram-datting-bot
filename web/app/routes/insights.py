"""Queue and funnel numbers (read-only page; the data are aggregates, no user ids)."""

from fastapi import APIRouter, Request

from ..dependencies import AdminSession, State
from ..ui import render

router = APIRouter()


@router.get("/admin/insights")
async def insights(request: Request, session: AdminSession, state: State):
    raw = request.query_params.get("days", "7")
    days = int(raw) if raw.isdecimal() and len(raw) <= 3 else 7  # anything else: 7
    data = await state.services.insights.report(session.admin_id, days)
    return render(request, "insights.html", session, active="insights", **(data or {}))

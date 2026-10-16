from fastapi import APIRouter, Request

from ..dependencies import AdminSession, State
from ..ui import render

router = APIRouter()


@router.get("/admin/statistics")
async def statistics(request: Request, session: AdminSession, state: State):
    data = await state.services.admin.statistics(session.admin_id)
    return render(request, "statistics.html", session, active="statistics", **data)

from fastapi import APIRouter, Request

from ..dependencies import AdminSession, State
from ..ui import render

router = APIRouter()


@router.get("/admin/")
async def dashboard(request: Request, session: AdminSession, state: State):
    data = await state.services.admin.statistics(session.admin_id)
    return render(request, "dashboard.html", session, active="dashboard", **data)

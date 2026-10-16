"""Reports: list, detail (retained context only), resolve / dismiss / ban."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import RedirectResponse

from bot.services.admin import REPORT_STATUSES

from .. import params
from ..dependencies import AdminForm, AdminSession, State
from ..ui import redirect_url, render
from .users import _FLASH, _ban

router = APIRouter()
ReportId = Annotated[int, Path(ge=1, lt=params.MAX_ID)]


@router.get("/admin/reports")
async def reports(request: Request, session: AdminSession, state: State):
    q = request.query_params
    status = q.get("status", "open")
    status = status if status in REPORT_STATUSES else None  # anything else (or "all") = all
    page = await state.services.admin.reports_list(
        session.admin_id, status=status, page=params.page_no(q.get("page"))
    )
    return render(
        request,
        "reports.html",
        session,
        active="reports",
        page=page,
        status=status or "all",
        statuses=REPORT_STATUSES,
    )


def _context(raw) -> list[dict]:
    """The retained snapshot (already expiry-filtered by the query). Never anything older."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [m for m in (raw or []) if isinstance(m, dict)]


async def _report_or_404(request, session, state, rid: int):
    report = await state.services.admin.report(session.admin_id, rid)
    if report is None:
        return None, render(
            request,
            "error.html",
            session,
            status_code=404,
            title="Not found",
            text="No such report.",
        )
    return report, None


@router.get("/admin/reports/{rid}")
async def report_detail(request: Request, rid: ReportId, session: AdminSession, state: State):
    report, missing = await _report_or_404(request, session, state, rid)
    if missing is not None:
        return missing
    return render(
        request,
        "report_detail.html",
        session,
        active="reports",
        report=report,
        context=_context(report["context"]),
    )


@router.get("/admin/reports/{rid}/ban")
async def report_ban_confirm(request: Request, rid: ReportId, session: AdminSession, state: State):
    report, missing = await _report_or_404(request, session, state, rid)
    if missing is not None:
        return missing
    detail = await state.services.admin.user_detail(session.admin_id, report["reported_id"])
    if detail is None:
        return render(
            request, "error.html", session, status_code=404, title="Not found", text="No such user."
        )
    return render(
        request,
        "ban_confirm.html",
        session,
        active="reports",
        user=detail["user"],
        action=f"/admin/reports/{rid}/ban",
        back=f"/admin/reports/{rid}",
        report_id=rid,
    )


@router.post("/admin/reports/{rid}/resolve")
async def resolve(rid: ReportId, session: AdminSession, state: State, form: AdminForm):
    return await _close(rid, session, state, dismiss=False)


@router.post("/admin/reports/{rid}/dismiss")
async def dismiss(rid: ReportId, session: AdminSession, state: State, form: AdminForm):
    return await _close(rid, session, state, dismiss=True)


async def _close(rid: int, session, state, *, dismiss: bool):
    res = await state.services.admin.close_report(session.admin_id, rid, dismiss=dismiss)
    flash = _FLASH.get(res.kind, "state") or ("dismissed" if dismiss else "resolved")
    return RedirectResponse(redirect_url(f"/admin/reports/{rid}", flash), status_code=303)


@router.post("/admin/reports/{rid}/ban")
async def report_ban(rid: ReportId, session: AdminSession, state: State, form: AdminForm):
    report = await state.services.admin.report(session.admin_id, rid)
    if report is None:
        return RedirectResponse(redirect_url("/admin/reports", "missing"), status_code=303)
    target = await _ban(state, session, form, report["reported_id"], rid, f"/admin/reports/{rid}")
    return RedirectResponse(target or "/admin/reports", status_code=303)

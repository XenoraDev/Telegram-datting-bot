"""Traffic / acquisition: where new users come from and which campaigns convert.

Everything on this page is an aggregate (counts and rupees per source / campaign): no user id,
username or message is ever read here. Campaigns are created here (the admin never builds a start
parameter by hand), paused or resumed, and the numbers can be exported as CSV (aggregates only).
Reading needs the admin session; creating / pausing is a POST with CSRF and our own origin.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import RedirectResponse, Response

from bot.services.attribution import start_link

from .. import params
from ..dependencies import AdminForm, AdminSession, State
from ..ui import redirect_url, render

router = APIRouter()
CampaignId = Annotated[int, Path(ge=1, lt=params.MAX_ID)]
WINDOWS = (7, 30, 90)
DEFAULT_DAYS = 30
MAX_SPAN_DAYS = 3660

_FLASH = {
    "bad_source": "camp_bad_source",
    "bad_name": "camp_bad_name",
    "unchanged": "camp_unchanged",
    "rate_limited": "rate",
    "not_found": "missing",
}


def _period(query) -> tuple[datetime, datetime, dict]:
    """[since, until) in UTC from ?from=&to= (YYYY-MM-DD, both days included) or ?days=7|30|90.
    Anything else falls back to the last 30 days. The window is never wider than ~10 years."""
    start, end = params.day(query.get("from")), params.day(query.get("to"))
    if start is not None and end is not None and start <= end:
        until = params.next_day(end)
        if until - start <= timedelta(days=MAX_SPAN_DAYS):
            return start, until, {"from": start.date().isoformat(), "to": end.date().isoformat()}
    raw = query.get("days", "")
    days = int(raw) if raw.isdecimal() and len(raw) <= 3 and int(raw) in WINDOWS else DEFAULT_DAYS
    now = datetime.now(UTC)
    return now - timedelta(days=days), now + timedelta(seconds=1), {"days": days}


def _keep(period: dict, tests: bool) -> str:
    """The period (and the test switch) as a query string, for links and the export."""
    parts = [f"{k}={v}" for k, v in period.items()] + (["tests=1"] if tests else [])
    return "&".join(parts)


@router.get("/admin/traffic")
async def traffic(request: Request, session: AdminSession, state: State):
    since, until, period = _period(request.query_params)
    tests = request.query_params.get("tests") == "1"
    report = await state.services.attribution.report(
        session.admin_id, since, until, include_tests=tests
    )
    bot = state.settings.bot_username
    links = {c["id"]: start_link(bot, c["code"]) for c in report["campaigns"]} if report else {}
    sources = await state.services.attribution.sources(session.admin_id)
    return render(
        request,
        "traffic.html",
        session,
        active="traffic",
        report=report,
        links=links,
        sources=sources,
        period=period,
        windows=WINDOWS,
        tests=tests,
        keep=_keep(period, tests),
        shown_from=since.date().isoformat(),
        shown_to=(until - timedelta(seconds=1)).date().isoformat(),
    )


@router.post("/admin/traffic/campaigns")
async def create_campaign(session: AdminSession, state: State, form: AdminForm):
    res = await state.services.attribution.create_campaign(
        session.admin_id, form.get("source"), form.get("name"), is_test=form.get("test") == "1"
    )
    if res.kind == "denied":
        return RedirectResponse("/admin/", status_code=303)
    key = "camp_created" if res.kind == "ok" else _FLASH.get(res.kind, "missing")
    return RedirectResponse(redirect_url("/admin/traffic", key), status_code=303)


@router.post("/admin/traffic/campaigns/{cid}/status")
async def campaign_status(cid: CampaignId, session: AdminSession, state: State, form: AdminForm):
    paused = form.get("paused") == "1"
    res = await state.services.attribution.set_status(session.admin_id, cid, paused=paused)
    if res.kind == "denied":
        return RedirectResponse("/admin/", status_code=303)
    key = (
        ("camp_paused" if paused else "camp_resumed")
        if res.kind == "ok"
        else _FLASH.get(res.kind, "missing")
    )
    return RedirectResponse(redirect_url("/admin/traffic", key), status_code=303)


def _safe(cell) -> str | int | float:
    """A spreadsheet runs text that starts with = + - @ as a formula: defuse it."""
    if isinstance(cell, str) and cell[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + cell
    return cell


@router.get("/admin/traffic/export.csv")
async def export(request: Request, session: AdminSession, state: State):
    """The same aggregate numbers as the page. No user is in it, only counts and rupees."""
    since, until, _ = _period(request.query_params)
    report = await state.services.attribution.report(
        session.admin_id, since, until, include_tests=request.query_params.get("tests") == "1"
    )
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        ["level", "source", "campaign", "status", "acquired", "age_confirmed", "profile_done",
         "chat", "follow", "match", "payment_started", "paid", "revenue_inr",
         "paused_link_starts"]
    )  # fmt: skip
    if report:

        def line(level, source, campaign, status, d, dead=""):
            writer.writerow(
                [_safe(x) for x in (level, source, campaign, status, d["acquired"], d["age"],
                                    d["profile"], d["chat"], d["follow"], d["matched"],
                                    d["pay_started"], d["paid"], f"{d['revenue'] / 100:.2f}", dead)]
            )  # fmt: skip

        line("total", "", "", "", report["totals"])
        for kind, d in report["kinds"].items():
            line("kind", kind, "", "", d)
        for s in report["sources"]:
            line("source", s["source"], "", "", s)
        for c in report["campaigns"]:
            line("campaign", c["source"], c["name"], c["status"], c, c["paused_link_starts"])
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="traffic-{stamp}.csv"',
            "Cache-Control": "no-store",
        },
    )

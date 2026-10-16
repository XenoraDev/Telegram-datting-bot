"""Server Capacity / Load Testing.

The web request only configures, confirms and displays: it never runs a workload. A test starts
after (1) the admin session, (2) the reviewed configuration and (3) a one-time Telegram
confirmation; a separate worker process executes it (see bot.loadtest).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import RedirectResponse, Response

from bot.handlers.loadtest import send_confirmation
from bot.loadtest.config import LADDER, PRESETS, TestConfig, parse_config, preset_config
from bot.loadtest.evaluate import ICON, THRESHOLD_SPEC
from bot.loadtest.safety import GROUPS

from ..dependencies import AdminForm, AdminSession, State
from ..ui import redirect_url, render

router = APIRouter()
RunId = Annotated[int, Path(ge=1, le=2_000_000_000)]
Preset = Annotated[str | None, Query(pattern="^(light|medium|heavy|stress|custom)$")]
FORM_KEYS = ("profile", "chats", "msgs_per_chat_min", "duration_min", "ramp_s")
THRESHOLD_GROUPS = (
    ("Verdict: errors and latency", ("error_warn_pct", "error_fail_pct", "p95_warn_ms", "p95_fail_ms", "p99_warn_ms", "p99_fail_ms")),  # noqa: E501
    ("Verdict: throughput and resources", ("throughput_warn_pct", "throughput_fail_pct", "cpu_warn_pct", "cpu_fail_pct", "ram_warn_pct", "ram_fail_pct", "swap_warn_pct", "swap_fail_pct")),  # noqa: E501
    ("Verdict: database", ("db_conn_warn_pct", "db_conn_fail_pct", "db_latency_warn_ms", "db_latency_fail_ms")),  # noqa: E501
    ("Safety margins for the recommended capacity", ("margin_cpu_pct", "margin_ram_pct", "margin_p95_ms", "margin_error_pct")),  # noqa: E501
    ("Automatic stop (fail-safe)", ("failsafe_ram_pct", "failsafe_cpu_pct", "failsafe_cpu_seconds", "failsafe_db_conn_pct", "failsafe_db_error_pct", "failsafe_health_fails")),  # noqa: E501
)  # fmt: skip
THRESHOLD_LABEL = {
    "error_warn_pct": "Error rate: warning from (%)",
    "error_fail_pct": "Error rate: fail from (%)",
    "p95_warn_ms": "P95 latency: warning from (ms)",
    "p95_fail_ms": "P95 latency: fail from (ms)",
    "p99_warn_ms": "P99 latency: warning from (ms)",
    "p99_fail_ms": "P99 latency: fail from (ms)",
    "throughput_warn_pct": "Sustained target rate: warning below (%)",
    "throughput_fail_pct": "Sustained target rate: fail below (%)",
    "cpu_warn_pct": "CPU (95th percentile): warning from (%)",
    "cpu_fail_pct": "CPU (95th percentile): fail from (%)",
    "ram_warn_pct": "Peak RAM: warning from (%)",
    "ram_fail_pct": "Peak RAM: fail from (%)",
    "swap_warn_pct": "Peak swap: warning from (%)",
    "swap_fail_pct": "Peak swap: fail from (%)",
    "db_conn_warn_pct": "DB connection usage: warning from (%)",
    "db_conn_fail_pct": "DB connection usage: fail from (%)",
    "db_latency_warn_ms": "DB latency P95: warning from (ms)",
    "db_latency_fail_ms": "DB latency P95: fail from (ms)",
    "margin_cpu_pct": "Recommended capacity: CPU at most (%)",
    "margin_ram_pct": "Recommended capacity: RAM at most (%)",
    "margin_p95_ms": "Recommended capacity: P95 at most (ms)",
    "margin_error_pct": "Recommended capacity: errors at most (%)",
    "failsafe_ram_pct": "Stop when RAM reaches (%)",
    "failsafe_cpu_pct": "Stop when CPU stays at (%)",
    "failsafe_cpu_seconds": "...for this many seconds",
    "failsafe_db_conn_pct": "Stop when DB connections reach (%)",
    "failsafe_db_error_pct": "Stop when DB errors reach (% of operations)",
    "failsafe_health_fails": "Stop after this many failed health checks in a row",
}


def _values_from(source: dict[str, str]) -> dict[str, str]:
    return {k: source.get(k, "") for k in FORM_KEYS}


def _preset_values(name: str, limits_cfg: TestConfig | None = None) -> dict[str, str]:
    cfg = limits_cfg or preset_config(name)
    return {
        "profile": cfg.profile,
        "chats": str(cfg.chats),
        "msgs_per_chat_min": str(cfg.msgs_per_chat_min),
        "duration_min": str(cfg.duration_s // 60),
        "ramp_s": str(cfg.ramp_s),
    }


async def _page(
    request: Request,
    session,
    state,
    values: dict[str, str],
    *,
    review=None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
):
    svc = state.services.loadtest
    admin = session.admin_id
    config, _ = parse_config(values, svc.settings.limits)
    live = await svc.live(admin)
    confirmation = await svc.confirmation_state(admin)
    safety = await svc.safety()
    waiting = bool(confirmation and confirmation["state"] == "pending")
    refresh = 5 if live.get("active") else (8 if waiting and review is None else 0)
    return render(
        request,
        "loadtest.html",
        session,
        status_code=status_code,
        active="loadtest",
        values=values,
        config=config,
        errors=errors if errors is not None else {},
        review=review,
        safety=safety,
        groups=GROUPS,
        live=live,
        confirmation=confirmation,
        runs=await svc.runs(admin, 50),
        capacity=await svc.capacity(admin),
        thresholds=await svc.thresholds(admin),
        threshold_groups=THRESHOLD_GROUPS,
        threshold_label=THRESHOLD_LABEL,
        threshold_spec=THRESHOLD_SPEC,
        next_test=await svc.next_recommended(admin),
        audit=await svc.audit_trail(admin, 15),
        limits=svc.settings.limits,
        presets=PRESETS,
        ladder=LADDER,
        icon=ICON,
        refresh=refresh,
    )


@router.get("/admin/loadtest")
async def loadtest_page(
    request: Request,
    session: AdminSession,
    state: State,
    preset: Preset = None,
    nxt: Annotated[int | None, Query(alias="next", ge=1, le=100_000)] = None,
):
    svc = state.services.loadtest
    values = _preset_values(preset or "heavy")
    if nxt is not None:
        want = await svc.next_recommended(session.admin_id)
        if want and want["chats"] == nxt:
            cfg = TestConfig(
                "custom",
                nxt,
                int(want["msgs_per_chat_min"]),
                300,
                60,  # type: ignore[call-overload]
            )
            values = _preset_values("custom", cfg)
    return await _page(request, session, state, values)


@router.post("/admin/loadtest/review")
async def review(request: Request, session: AdminSession, state: State, form: AdminForm):
    values = _values_from(form)
    res = await state.services.loadtest.review(session.admin_id, values)
    if res.kind == "denied":
        return RedirectResponse("/admin/", status_code=303)
    if res.kind == "rate_limited":
        return RedirectResponse(redirect_url("/admin/loadtest", "rate"), status_code=303)
    if res.kind == "invalid":
        return await _page(request, session, state, values, errors=res.errors, status_code=400)
    return await _page(request, session, state, values, review=res)


@router.post("/admin/loadtest/confirmation")
async def send_telegram_confirmation(
    request: Request, session: AdminSession, state: State, form: AdminForm
):
    svc = state.services.loadtest
    values = _values_from(form)
    res = await svc.request_confirmation(session.admin_id, values)
    flash = {
        "denied": None,
        "rate_limited": "rate",
        "blocked": "lt_blocked",
        "busy": "lt_busy",
    }
    if res.kind == "invalid":
        return await _page(request, session, state, values, errors=res.errors, status_code=400)
    if res.kind in flash:
        if res.kind == "denied":
            return RedirectResponse("/admin/", status_code=303)
        return RedirectResponse(redirect_url("/admin/loadtest", flash[res.kind]), status_code=303)
    delivered = state.bot is not None and await send_confirmation(
        state.bot, state.services, session.admin_id, res.confirmation_id, res.token, res.config
    )
    if state.bot is None:
        await svc.delivery_failed(session.admin_id, res.confirmation_id)
    msg = "lt_sent" if delivered else "lt_telegram"
    return RedirectResponse(redirect_url("/admin/loadtest", msg) + "#start", status_code=303)


@router.post("/admin/loadtest/confirmation/withdraw")
async def withdraw(session: AdminSession, state: State, form: AdminForm):
    await state.services.loadtest.withdraw(session.admin_id)
    return RedirectResponse(redirect_url("/admin/loadtest", "lt_withdrawn"), status_code=303)


@router.post("/admin/loadtest/thresholds")
async def save_thresholds(request: Request, session: AdminSession, state: State, form: AdminForm):
    kind, errors = await state.services.loadtest.save_thresholds(session.admin_id, form)
    if kind == "denied":
        return RedirectResponse("/admin/", status_code=303)
    if kind == "rate_limited":
        return RedirectResponse(redirect_url("/admin/loadtest", "rate"), status_code=303)
    if kind == "invalid":
        return await _page(
            request, session, state, _preset_values("heavy"), errors=errors, status_code=400
        )
    return RedirectResponse(
        redirect_url("/admin/loadtest", "lt_thresholds") + "#thresholds", status_code=303
    )


@router.get("/admin/loadtest/compare")
async def compare(
    request: Request,
    session: AdminSession,
    state: State,
    a: Annotated[int, Query(ge=1, le=2_000_000_000)],
    b: Annotated[int, Query(ge=1, le=2_000_000_000)],
):
    data = await state.services.loadtest.compare(session.admin_id, a, b)
    if data is None:
        return render(
            request, "error.html", session, status_code=404, title="Not found",
            text="Both tests must exist and have measured results.",
        )  # fmt: skip
    return render(request, "loadtest_compare.html", session, active="loadtest", cmp=data, icon=ICON)


@router.get("/admin/loadtest/{rid}")
async def report(rid: RunId, request: Request, session: AdminSession, state: State):
    svc = state.services.loadtest
    run = await svc.run(session.admin_id, rid)
    if run is None:
        return render(
            request, "error.html", session, status_code=404, title="Not found",
            text="No such load test.",
        )  # fmt: skip
    windows = await svc.windows(session.admin_id, rid)
    return render(
        request,
        "loadtest_report.html",
        session,
        active="loadtest",
        run=run,
        windows=windows,
        icon=ICON,
        refresh=5 if run["status"] in ("queued", "starting", "running", "stopping") else 0,
        series={
            key: [w.get(key) for w in windows]
            for key in ("msgs_s", "p95_ms", "cpu_pct", "ram_pct", "db_latency_ms")
        },
        recent=windows[-60:] if len(windows) > 60 else windows,
    )


@router.post("/admin/loadtest/{rid}/stop")
async def stop(rid: RunId, session: AdminSession, state: State, form: AdminForm):
    res = await state.services.loadtest.stop(session.admin_id, rid)
    if res.kind == "denied":
        return RedirectResponse("/admin/", status_code=303)
    msg = {
        "ok": "lt_cancelled" if res.status == "cancelled" else "lt_stop",
        "not_active": "lt_not_active",
        "missing": "missing",
        "rate_limited": "rate",
    }[res.kind]
    return RedirectResponse(redirect_url("/admin/loadtest", msg), status_code=303)


@router.get("/admin/loadtest/{rid}/export.{kind}")
async def export(
    rid: RunId,
    kind: Annotated[str, Path(pattern="^(csv|json)$")],
    session: AdminSession,
    state: State,
):
    body = await state.services.loadtest.export(session.admin_id, rid, kind)
    if body is None:
        return Response("Not found", status_code=404, media_type="text/plain")
    media = "text/csv; charset=utf-8" if kind == "csv" else "application/json"
    return Response(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="loadtest-{rid}.{kind}"'},
    )

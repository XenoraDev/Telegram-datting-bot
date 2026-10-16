"""Server Capacity / Load Testing: admin-facing service.

Authority: only ADMIN_IDS members, checked here on every call (the web session and the Telegram
callback both pass through this service). A test is started only by consuming a one-time Telegram
confirmation that is bound to the admin and to the exact configuration; the web request never runs
a workload. The separate worker (bot.loadtest.worker) executes queued runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field

import asyncpg

from ..db import repo_loadtest as repo
from ..loadtest import report as reports
from ..loadtest.config import LADDER, TestConfig, config_hash, parse_config
from ..loadtest.evaluate import (
    Capacity,
    calculate_capacity,
    compare_runs,
    evaluate_run,
    merge_thresholds,
    next_level,
    server_signature,
    validate_thresholds,
)
from ..loadtest.safety import LoadTestSettings, SafetyReport, evaluate
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)

CONFIRM_TTL_SECONDS = 300
STALE_HEARTBEAT_SECONDS = 45.0  # a running test whose worker is silent this long is aborted
WORKER_ONLINE_SECONDS = 30.0
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _j(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def run_view(row: Mapping[str, object] | None) -> dict[str, object] | None:
    """A run row as plain data (JSON columns decoded)."""
    if row is None:
        return None
    out = dict(row)
    for key in ("config", "thresholds", "environment", "server_info", "summary", "result_reasons"):
        out[key] = _j(out.get(key))
    return out


@dataclass(frozen=True, slots=True)
class Review:
    kind: str  # ok | invalid | denied | rate_limited
    config: TestConfig | None = None
    errors: dict[str, str] = field(default_factory=dict)
    safety: SafetyReport | None = None
    busy: bool = False
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    kind: str  # ok | invalid | denied | rate_limited | blocked | busy
    token: str | None = None
    confirmation_id: int | None = None
    config: TestConfig | None = None
    errors: dict[str, str] = field(default_factory=dict)
    safety: SafetyReport | None = None


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    # ok | denied | invalid | used | expired | cancelled | blocked | busy
    kind: str
    run_id: int | None = None
    config: TestConfig | None = None


@dataclass(frozen=True, slots=True)
class StopResult:
    kind: str  # ok | denied | not_active | missing | rate_limited
    status: str | None = None


class LoadTestService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        limiter: RateLimiter,
        admin_ids: frozenset[int],
        settings: LoadTestSettings | None = None,
    ) -> None:
        self._pool = pool
        self._limiter = limiter
        self._admins = admin_ids
        self.settings = settings or LoadTestSettings()

    def is_admin(self, uid: int | None) -> bool:
        return uid is not None and uid in self._admins

    # ------------------------------------------------------------------ safety

    async def safety(self) -> SafetyReport:
        try:
            identity = await repo.database_identity(self._pool)
        except Exception:  # an unreachable / odd database is "not proven", never "assume fine"
            log.warning("load test safety: the database identity could not be read")
            identity = None
        return evaluate(self.settings, identity)

    # ------------------------------------------------------------------ configuration

    async def review(self, admin_id: int, form: Mapping[str, str]) -> Review:
        """Validate a configuration for display. A configuration that differs from an unanswered
        Telegram confirmation invalidates that confirmation."""
        if not self.is_admin(admin_id):
            return Review("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return Review("rate_limited")
        config, errors = parse_config(form, self.settings.limits)
        if config is None:
            return Review("invalid", errors=errors)
        superseded = False
        async with self._pool.acquire() as conn, conn.transaction():
            pending = await conn.fetchval(
                "SELECT config_hash FROM load_test_confirmations "
                "WHERE admin_id = $1 AND state = 'pending'",
                admin_id,
            )
            if pending is not None and pending != config.digest:
                superseded = await repo.supersede_pending(conn, admin_id) > 0
            await repo.audit(conn, admin_id, "configured", detail=_describe(config))
        return Review(
            "ok",
            config,
            safety=await self.safety(),
            busy=await repo.active_run(self._pool) is not None,
            superseded=superseded,
        )

    # ------------------------------------------------------------------ confirmation

    async def request_confirmation(
        self, admin_id: int, form: Mapping[str, str]
    ) -> ConfirmationRequest:
        if not self.is_admin(admin_id):
            return ConfirmationRequest("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return ConfirmationRequest("rate_limited")
        config, errors = parse_config(form, self.settings.limits)
        if config is None:
            return ConfirmationRequest("invalid", errors=errors)
        safety = await self.safety()
        if not safety.ok:
            await repo.audit(self._pool, admin_id, "blocked", detail=_failed_keys(safety))
            log.warning("load test blocked by the environment checks: %s", _failed_keys(safety))
            return ConfirmationRequest("blocked", config=config, safety=safety)
        if await repo.active_run(self._pool) is not None:
            return ConfirmationRequest("busy", config=config, safety=safety)
        token = secrets.token_urlsafe(24)
        async with self._pool.acquire() as conn, conn.transaction():
            await repo.supersede_pending(conn, admin_id)
            cid = await repo.create_confirmation(
                conn, token_hash(token), admin_id, config.as_json(), config.digest,
                CONFIRM_TTL_SECONDS,
            )  # fmt: skip
        return ConfirmationRequest("ok", token, cid, config, safety=safety)

    async def delivered(self, admin_id: int, cid: int, chat_id: int, message_id: int) -> None:
        await repo.set_confirmation_message(self._pool, cid, chat_id, message_id)
        await repo.audit(self._pool, admin_id, "confirmation_sent", detail=f"confirmation {cid}")

    async def delivery_failed(self, admin_id: int, cid: int) -> None:
        await repo.mark_confirmation(self._pool, cid, "failed")
        await repo.audit(self._pool, admin_id, "failed", detail=f"confirmation {cid} not delivered")

    async def confirm(self, tg_user_id: int, token: str) -> ConfirmResult:
        """The admin pressed CONFIRM in Telegram: create the (queued) run, exactly once."""
        if not self.is_admin(tg_user_id):
            return ConfirmResult("denied")
        if not _TOKEN_RE.fullmatch(token or ""):
            return ConfirmResult("invalid")
        digest = token_hash(token)
        safety = await self.safety()  # re-proved at the moment of confirming
        async with self._pool.acquire() as conn:
            try:
                async with conn.transaction():
                    row = await repo.confirmation_by_hash(conn, digest, lock=True)
                    if row is None:
                        return ConfirmResult("invalid")
                    if row["admin_id"] != tg_user_id:
                        await repo.audit(
                            conn, tg_user_id, "blocked", detail="token of another admin"
                        )
                        return ConfirmResult("denied")
                    if row["state"] != "pending":
                        return ConfirmResult(
                            "cancelled" if row["state"] in ("cancelled", "superseded") else "used"
                        )
                    if row["is_expired"]:
                        await repo.mark_confirmation(conn, row["id"], "expired")
                        await repo.audit(
                            conn,
                            tg_user_id,
                            "confirmation_expired",
                            detail=f"confirmation {row['id']}",
                        )
                        return ConfirmResult("expired")
                    config_json = _j(row["config"])
                    if (
                        not isinstance(config_json, dict)
                        or config_hash(config_json) != row["config_hash"]
                    ):
                        return ConfirmResult("invalid")
                    config = TestConfig.from_json(config_json)
                    # limits may have been lowered since the confirmation was created
                    again, _errs = parse_config(_as_form(config), self.settings.limits)
                    if again is None or again.digest != config.digest:
                        return ConfirmResult("invalid")
                    if not safety.ok:
                        await repo.audit(conn, tg_user_id, "blocked", detail=_failed_keys(safety))
                        return ConfirmResult("blocked", config=config)
                    thresholds = merge_thresholds(await repo.thresholds(conn))
                    run_id = await repo.create_run(
                        conn, tg_user_id, config.as_json(), config.digest, thresholds
                    )
                    await repo.consume_confirmation(conn, row["id"], run_id)
                    await repo.audit(conn, tg_user_id, "confirmed", run_id, _describe(config))
            except asyncpg.UniqueViolationError:
                return ConfirmResult("busy", config=None)
        log.info("load test %s queued after a Telegram confirmation", run_id)
        return ConfirmResult("ok", run_id, config)

    async def cancel_confirmation(self, tg_user_id: int, token: str) -> ConfirmResult:
        if not self.is_admin(tg_user_id):
            return ConfirmResult("denied")
        if not _TOKEN_RE.fullmatch(token or ""):
            return ConfirmResult("invalid")
        async with self._pool.acquire() as conn, conn.transaction():
            row = await repo.confirmation_by_hash(conn, token_hash(token), lock=True)
            if row is None:
                return ConfirmResult("invalid")
            if row["admin_id"] != tg_user_id:
                return ConfirmResult("denied")
            if row["state"] != "pending":
                return ConfirmResult("used")
            await repo.mark_confirmation(conn, row["id"], "cancelled")
            await repo.audit(
                conn, tg_user_id, "confirmation_cancelled", detail=f"confirmation {row['id']}"
            )
        return ConfirmResult("cancelled")

    async def confirmation_state(self, admin_id: int) -> dict[str, object] | None:
        if not self.is_admin(admin_id):
            return None
        row = await repo.latest_confirmation(self._pool, admin_id)
        if row is None:
            return None
        state = row["state"]
        if state == "pending" and row["is_expired"]:
            state = "expired"
        return {
            "id": row["id"],
            "state": state,
            "config": _j(row["config"]),
            "config_hash": row["config_hash"],
            "expires_at": row["expires_at"],
            "run_id": row["run_id"],
        }

    # ------------------------------------------------------------------ runs

    async def stop(self, admin_id: int, run_id: int) -> StopResult:
        if not self.is_admin(admin_id):
            return StopResult("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return StopResult("rate_limited")
        if await repo.get_run(self._pool, run_id) is None:
            return StopResult("missing")
        status = await repo.request_cancel(self._pool, run_id, admin_id)
        if status is None:
            return StopResult("not_active")
        await repo.audit(
            self._pool, admin_id, "cancelled" if status == "cancelled" else "stop_requested", run_id
        )
        return StopResult("ok", status)

    async def runs(self, admin_id: int, limit: int = 100) -> list[dict[str, object]]:
        if not self.is_admin(admin_id):
            return []
        return [run_view(r) for r in await repo.list_runs(self._pool, limit)]  # type: ignore[misc]

    async def run(self, admin_id: int, run_id: int) -> dict[str, object] | None:
        if not self.is_admin(admin_id):
            return None
        return run_view(await repo.get_run(self._pool, run_id))

    async def windows(self, admin_id: int, run_id: int) -> list[dict[str, object]]:
        if not self.is_admin(admin_id):
            return []
        return [dict(r) for r in await repo.list_metrics(self._pool, run_id)]

    async def live(self, admin_id: int) -> dict[str, object]:
        """What the dashboard shows: the active run, its newest window and worker health."""
        if not self.is_admin(admin_id):
            return {}
        await self.reap_stale()
        active = run_view(await repo.active_run(self._pool))
        latest = None
        if active is not None:
            row = await repo.latest_metric(self._pool, int(active["id"]))  # type: ignore[call-overload]
            latest = dict(row) if row else None
        workers = await repo.workers(self._pool)
        online = [
            w for w in workers if w["age_s"] is not None and w["age_s"] <= WORKER_ONLINE_SECONDS
        ]
        return {
            "active": active,
            "latest": latest,
            "worker_online": bool(online),
            "worker_state": online[0]["state"] if online else None,
            "worker_detail": online[0]["detail"] if online else None,
        }

    async def reap_stale(self) -> int:
        """A run whose worker stopped reporting is ended with a partial report, and its synthetic
        rows are removed. Never touches services, containers or volumes."""
        reaped = 0
        for row in await repo.stale_runs(self._pool, STALE_HEARTBEAT_SECONDS):
            run = run_view(await repo.get_run(self._pool, row["id"]))
            if run is None:
                continue
            reaped += await self._abort(run, "the worker stopped responding")
        for orphan in await repo.orphan_runs(self._pool):
            async with self._pool.acquire() as conn:
                await repo.delete_synthetic(conn, orphan["run_id"])
        return reaped

    async def _abort(self, run: dict[str, object], reason: str) -> int:
        rid = int(run["id"])  # type: ignore[call-overload]
        cfg = TestConfig.from_json(run["config"])  # type: ignore[arg-type]
        windows = [dict(r) for r in await repo.list_metrics(self._pool, rid)]
        elapsed = float(windows[-1]["elapsed_s"]) if windows else 0.0
        summary = reports.summary_from_windows(windows, cfg, stop_reason=reason, elapsed_s=elapsed)
        verdict = evaluate_run(summary, run["thresholds"])  # type: ignore[arg-type]
        done = await repo.finish_run(
            self._pool, rid, "aborted", stop_reason=reason, summary=summary,
            result=verdict.result, reasons=[r.as_json() for r in verdict.reasons], partial=True,
        )  # fmt: skip
        if done:
            await repo.audit(self._pool, int(run["requested_by"]), "aborted", rid, reason)  # type: ignore[call-overload]
            log.warning("load test %s aborted: %s", rid, reason)
        return int(done)

    # ------------------------------------------------------------------ reports

    async def thresholds(self, admin_id: int) -> dict[str, float]:
        if not self.is_admin(admin_id):
            return {}
        return merge_thresholds(await repo.thresholds(self._pool))

    async def save_thresholds(
        self, admin_id: int, form: Mapping[str, str]
    ) -> tuple[str, dict[str, str]]:
        if not self.is_admin(admin_id):
            return "denied", {}
        if not self._limiter.allow("admin_action", admin_id):
            return "rate_limited", {}
        values, errors = validate_thresholds(form)
        if values is None:
            return "invalid", errors
        async with self._pool.acquire() as conn, conn.transaction():
            await repo.save_thresholds(conn, admin_id, values)
            await repo.audit(conn, admin_id, "thresholds_saved")
        return "ok", {}

    async def capacity(self, admin_id: int) -> list[tuple[str, Capacity]]:
        """Observed capacity per hardware signature, the most recently tested hardware first."""
        if not self.is_admin(admin_id):
            return []
        runs = [run_view(r) for r in await repo.finished_runs(self._pool)]
        thresholds = merge_thresholds(await repo.thresholds(self._pool))
        groups: dict[str, list[dict[str, object]]] = {}
        for run in runs:
            assert run is not None  # noqa: S101
            groups.setdefault(server_signature(run["server_info"]), []).append(run)  # type: ignore[arg-type]
        ordered = sorted(groups.items(), key=lambda kv: -max(int(r["id"]) for r in kv[1]))  # type: ignore[call-overload]
        return [(sig, calculate_capacity(rs, thresholds)) for sig, rs in ordered]

    async def next_recommended(self, admin_id: int) -> dict[str, object] | None:
        """The next ladder step after the newest finished test, only when that test PASSED."""
        if not self.is_admin(admin_id):
            return None
        rows = await repo.list_runs(self._pool, 1)
        if not rows or rows[0]["status"] != "completed" or rows[0]["result"] != "PASS":
            return None
        run = run_view(rows[0])
        cfg = run["config"]  # type: ignore[index]
        nxt = next_level(int(cfg["chats"]), LADDER)  # type: ignore[call-overload, index]
        if nxt is None:
            return None
        return {"after": run["id"], "chats": nxt, "msgs_per_chat_min": cfg["msgs_per_chat_min"]}  # type: ignore[index]

    async def compare(self, admin_id: int, a: int, b: int) -> dict[str, object] | None:
        if not self.is_admin(admin_id):
            return None
        ra, rb = await self.run(admin_id, a), await self.run(admin_id, b)
        if ra is None or rb is None or not ra.get("summary") or not rb.get("summary"):
            return None
        first, second = sorted((ra, rb), key=lambda r: int(r["id"]))  # type: ignore[call-overload]
        return {
            "before": first,
            "after": second,
            "rows": compare_runs(first["summary"], second["summary"]),  # type: ignore[arg-type]
            "same_workload": first["config"]["chats"] == second["config"]["chats"]  # type: ignore[index]
            and first["config"]["msgs_per_chat_min"] == second["config"]["msgs_per_chat_min"],  # type: ignore[index]
            "same_hardware": server_signature(first["server_info"])
            == server_signature(second["server_info"]),  # type: ignore[arg-type]
        }

    async def export(self, admin_id: int, run_id: int, kind: str) -> str | None:
        if not self.is_admin(admin_id) or kind not in ("csv", "json"):
            return None
        run = run_view(await repo.get_run(self._pool, run_id))
        if run is None or run["status"] not in repo.FINISHED:
            return None
        windows = [dict(r) for r in await repo.list_metrics(self._pool, run_id)]
        await repo.audit(self._pool, admin_id, "exported", run_id, kind)
        return reports.to_csv(windows, run_id) if kind == "csv" else reports.to_json(run, windows)

    async def audit_trail(self, admin_id: int, limit: int = 30):
        return await repo.list_audit(self._pool, limit) if self.is_admin(admin_id) else []


def _describe(cfg: TestConfig) -> str:
    return (
        f"{cfg.profile}: {cfg.chats} chats x {cfg.msgs_per_chat_min} msg/min, "
        f"{cfg.duration_s}s + {cfg.ramp_s}s ramp"
    )


def _failed_keys(report: SafetyReport) -> str:
    return "failed checks: " + ", ".join(c.key for c in report.failed())


def _as_form(cfg: TestConfig) -> dict[str, str]:
    return {
        "profile": cfg.profile,
        "chats": str(cfg.chats),
        "msgs_per_chat_min": str(cfg.msgs_per_chat_min),
        "duration_s": str(cfg.duration_s),
        "ramp_s": str(cfg.ramp_s),
    }

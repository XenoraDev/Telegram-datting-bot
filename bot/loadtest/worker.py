"""The load-test worker: a separate process that executes queued jobs.

    python -m bot.loadtest.worker

It never runs inside the bot or the web request. A job exists only after the admin confirmed it in
Telegram. Before EVERY job the environment is proved again, and an unsafe environment makes the
worker refuse (the job is marked "blocked"). It never restarts or stops any service, never touches
containers or volumes, and it exits gracefully on SIGTERM / SIGINT (a running test is stopped and
gets a partial report).
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import sys
import uuid

import asyncpg

from ..config import ConfigError, load_settings
from ..db import repo_loadtest as repo
from ..db.pool import create_pool
from ..services.loadtest import LoadTestService, run_view
from ..services.rate_limit import RateLimiter
from .config import TestConfig
from .metrics import app_commit, server_info
from .runner import DEFAULT_BUCKET_SECONDS, Workload
from .safety import BLOCKED_TEXT, LoadTestSettings, evaluate

log = logging.getLogger("bot.loadtest")

POLL_SECONDS = 2.0
IDLE_BEAT_SECONDS = 10.0


async def safety_report(control: asyncpg.Pool, settings: LoadTestSettings):
    try:
        identity = await repo.database_identity(control)
    except Exception:
        identity = None
    return evaluate(settings, identity)


async def process_run(
    run: dict[str, object],
    workload_pool: asyncpg.Pool,
    control_pool: asyncpg.Pool,
    settings: LoadTestSettings,
    *,
    stop_event: asyncio.Event | None = None,
    bucket_seconds: float = DEFAULT_BUCKET_SECONDS,
    **workload_kwargs: object,
) -> str:
    """Execute one claimed run to its final state. Returns the final status."""
    rid = int(run["id"])  # type: ignore[call-overload]
    admin = int(run["requested_by"])  # type: ignore[call-overload]
    cfg = TestConfig.from_json(run["config"])  # type: ignore[arg-type]
    log.info("load test %s: claimed (%s)", rid, cfg.profile)

    safety = await safety_report(control_pool, settings)
    if not safety.ok:
        reason = BLOCKED_TEXT + " (" + ", ".join(c.key for c in safety.failed()) + ")"
        await repo.set_run_environment(control_pool, rid, safety.as_json(), {}, app_commit())
        await repo.finish_run(
            control_pool, rid, "blocked", stop_reason=reason, summary=None, result=None,
            reasons=None, partial=False,
        )  # fmt: skip
        await repo.audit(control_pool, admin, "blocked", rid, reason)
        log.error("load test %s: %s", rid, reason)
        return "blocked"

    extra = {
        "postgres": await repo.postgres_version(control_pool),
        "python": platform.python_version(),
        "docker": os.environ.get("LOAD_TEST_DOCKER_VERSION", "").strip()[:40] or None,
    }
    await repo.set_run_environment(
        control_pool, rid, safety.as_json(), server_info(app_commit(), extra), app_commit()
    )
    if not await repo.set_run_status(control_pool, rid, "running", only_from=("starting",)):
        # an admin stop arrived while the job was starting
        await repo.finish_run(
            control_pool, rid, "cancelled", stop_reason="stopped before the workload began",
            summary=None, result=None, reasons=None, partial=True,
        )  # fmt: skip
        return "cancelled"
    await repo.audit(
        control_pool, admin, "started", rid, f"{cfg.chats} chats x {cfg.msgs_per_chat_min}"
    )
    log.info("load test %s: running (environment verified)", rid)

    thresholds = {k: float(v) for k, v in dict(run["thresholds"]).items()}  # type: ignore[call-overload]
    workload = Workload(
        workload_pool, control_pool, rid, cfg, thresholds, settings,
        bucket_seconds=bucket_seconds, stop_event=stop_event, **workload_kwargs,
    )  # type: ignore[arg-type]  # fmt: skip
    outcome = await workload.execute()

    await repo.finish_run(
        control_pool, rid, outcome.status, stop_reason=outcome.stop_reason,
        summary=outcome.summary, result=outcome.result, reasons=outcome.reasons,
        partial=outcome.partial,
    )  # fmt: skip
    action = {"completed": "completed", "cancelled": "cancelled", "aborted": "aborted"}.get(
        outcome.status, "failed"
    )
    await repo.audit(control_pool, admin, action, rid, outcome.stop_reason or outcome.result)
    log.info("load test %s: %s, result %s", rid, outcome.status, outcome.result)
    return outcome.status


async def serve(stop: asyncio.Event) -> int:
    try:
        bot_settings = load_settings(os.environ)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    settings = LoadTestSettings.from_env(os.environ)
    worker_id = f"{platform.node()[:40]}-{uuid.uuid4().hex[:8]}"
    control = await create_pool(
        bot_settings.database_url, min_size=1, max_size=3, application_name="dating-bot-loadtest"
    )
    workload = await create_pool(
        bot_settings.database_url,
        min_size=1,
        max_size=settings.pool_size,
        application_name="dating-bot-loadtest",
    )
    service = LoadTestService(control, RateLimiter(), frozenset(), settings)
    log.info("load-test worker %s started", worker_id)
    try:
        safety = await safety_report(control, settings)
        if not safety.ok:
            log.error(
                "%s Failed checks: %s", BLOCKED_TEXT, ", ".join(c.key for c in safety.failed())
            )
        beat_at = 0.0
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            if loop.time() - beat_at >= IDLE_BEAT_SECONDS:
                state = "idle" if safety.ok else "blocked"
                await repo.worker_beat(
                    control, worker_id, state, None if safety.ok else "unsafe environment"
                )
                beat_at = loop.time()
                safety = await safety_report(control, settings)
            await service.reap_stale()
            async with control.acquire() as conn:
                claimed = await repo.claim_next(conn, worker_id)
            if claimed is None:
                with_timeout = asyncio.create_task(stop.wait())
                await asyncio.wait({with_timeout}, timeout=POLL_SECONDS)
                with_timeout.cancel()
                continue
            await repo.worker_beat(control, worker_id, "busy", f"run {claimed['id']}")
            beat_at = loop.time()
            await process_run(run_view(claimed), workload, control, settings, stop_event=stop)  # type: ignore[arg-type]
    finally:
        await workload.close()
        await control.close()
        log.info("load-test worker %s stopped", worker_id)
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    async def runner() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        return await serve(stop)

    return asyncio.run(runner())


if __name__ == "__main__":
    raise SystemExit(main())

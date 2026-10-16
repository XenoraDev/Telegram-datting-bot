"""The synthetic chat workload, its metrics collector and the fail-safe.

What is simulated: User A -> relay path -> User B (and back) inside synthetic chats. For every
message the SAME code the bot runs is executed:

    repo_users.get            (UserContextMiddleware: load the sender)
    RateLimiter "message"     (ThrottleMiddleware)
    ChatRelayService.relay    (partner lookup, mute check, content rules, contact tracker,
                               per-sender rate limit, flood filter, RAM context buffer)

The ONLY step that is replaced is the last one, ``bot.send_message(partner)``: ``NullDelivery``
counts what would have been delivered. This module owns no Telegram client and never imports
aiogram, so it cannot send anything to Telegram (enforced by a test).

Synthetic users have negative ids (Telegram ids are positive), are registered in
``load_test_users`` and are removed again when the run ends. Chat history is never stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import random
import time
import urllib.request
from array import array
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import asyncpg

from ..db import repo_loadtest as repo
from ..db import repo_users
from ..services.chat_relay import ChatRelayService, ContextBuffer, Incoming
from ..services.rate_limit import RateLimiter
from . import report
from .config import TestConfig
from .evaluate import evaluate_run
from .metrics import HealthTracker, HostSample, HostSampler, latency_stats
from .safety import LoadTestSettings

log = logging.getLogger("bot.loadtest")

DEFAULT_BUCKET_SECONDS = 5.0
TICK_SECONDS = 0.005
FAILSAFE_MIN_OPS = 5  # a window with fewer operations is too small to judge an error ratio
DRAIN_SECONDS = 10.0
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


class NullDelivery:
    """Replaces ``bot.send_message(partner, text)``: nothing leaves the process."""

    def __init__(self) -> None:
        self.delivered = 0

    def deliver(self, partner_id: int, text: str) -> None:
        self.delivered += 1


def _word(n: int) -> str:
    """Unique letters-only token (digits would trip the phone-number filter)."""
    out = []
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out.append(_LETTERS[r])
    return "".join(reversed(out))


# ----------------------------------------------------------------------------- probes


@dataclass(frozen=True, slots=True)
class DbSample:
    connections: int | None
    conn_pct: float | None
    latency_ms: float | None
    errors: int = 0
    postmaster_start: object | None = None


class Probes:
    """Database / application health, measured on a control connection that is separate from the
    workload pool so that a saturated workload cannot hide (or cause) the measurement."""

    def __init__(self, control: asyncpg.Pool, settings: LoadTestSettings) -> None:
        self._control = control
        self._settings = settings
        self._max_connections: int | None = None

    async def db(self) -> DbSample:
        try:
            if self._max_connections is None:
                self._max_connections = int(await self._control.fetchval("SHOW max_connections"))
            t0 = time.perf_counter()
            await self._control.fetchval("SELECT 1")
            latency = (time.perf_counter() - t0) * 1000
            row = await self._control.fetchrow(
                "SELECT count(*) FILTER (WHERE backend_type = 'client backend') AS n, "
                "pg_postmaster_start_time() AS started FROM pg_stat_activity"
            )
        except (asyncpg.PostgresError, OSError, TimeoutError):
            return DbSample(None, None, None, errors=1)
        n = int(row["n"])
        return DbSample(
            n, round(n / (self._max_connections or 100) * 100, 2), latency, 0, row["started"]
        )

    async def bot_ok(self) -> bool | None:
        """True when the bot holds database connections. None when it holds none right now
        (idle pools close connections), i.e. not observable."""
        try:
            n = await self._control.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'dating-bot' "
                "AND datname = current_database()"
            )
        except (asyncpg.PostgresError, OSError, TimeoutError):
            return None
        return True if n else None

    async def web_ok(self) -> bool | None:
        url = self._settings.web_health_url
        if not url:
            return None

        def fetch() -> bool:
            request = urllib.request.Request(  # noqa: S310 - internal http URL, checked by safety
                url, headers={"Host": self._settings.admin_domain}
            )
            try:
                with urllib.request.urlopen(request, timeout=3.0) as resp:  # noqa: S310
                    return resp.status == 200
            except OSError:
                return False

        return await asyncio.to_thread(fetch)


# ----------------------------------------------------------------------------- outcome


@dataclass(slots=True)
class Outcome:
    status: str  # completed | cancelled | aborted | failed
    stop_reason: str | None
    summary: dict[str, object]
    result: str | None
    reasons: list[dict[str, str]]
    partial: bool


@dataclass(slots=True)
class _Window:
    ok: int = 0
    failed: int = 0
    db_errors: int = 0
    latencies: list[float] = field(default_factory=list)


class Workload:
    def __init__(
        self,
        workload_pool: asyncpg.Pool,
        control_pool: asyncpg.Pool,
        run_id: int,
        cfg: TestConfig,
        thresholds: dict[str, float],
        settings: LoadTestSettings,
        *,
        bucket_seconds: float = DEFAULT_BUCKET_SECONDS,
        host: HostSampler | None = None,
        probes: Probes | None = None,
        stop_event: asyncio.Event | None = None,
        on_window: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        max_in_flight: int | None = None,
    ) -> None:
        self._pool = workload_pool
        self._control = control_pool
        self._run_id = run_id
        self._cfg = cfg
        self._t = thresholds
        self._bucket = bucket_seconds
        self._host = host or HostSampler()
        self._probes = probes or Probes(control_pool, settings)
        self._external_stop = stop_event or asyncio.Event()
        self._on_window = on_window
        self._max_in_flight = max_in_flight or max(workload_pool.get_max_size() * 8, 32)

        limiter = RateLimiter()
        self._limiter = limiter
        self._relay = ChatRelayService(workload_pool, ContextBuffer(), limiter)
        self.delivery = NullDelivery()

        self._stop = asyncio.Event()
        self._stop_reason: str | None = None
        self._cancelled = False
        self._failsafe = False
        self._window = _Window()
        self._totals = {"ok": 0, "failed": 0, "db_errors": 0, "shed": 0}
        self._rejected: Counter[str] = Counter()
        self._latencies = array("f")
        self._windows: list[dict[str, object]] = []
        self._in_flight = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._counter = 0
        self._active_chats = 0
        self._seq = 0
        self._lag_ms = 0.0
        self._cpu_hot = 0
        self._ram_hot = 0
        self._conn_hot = 0
        self._ctl_errors = 0
        self._bot = HealthTracker("bot")
        self._web = HealthTracker("web")
        self._pm_start: object | None = None
        self._db_restarts = 0
        self._start = 0.0
        self._last_window_at = 0.0
        self._error_budget_ok = True

    # ------------------------------------------------------------------ public

    async def execute(self) -> Outcome:
        chats = self._cfg.chats
        sessions: list[tuple[int, int, int]] = []
        try:
            async with self._control.acquire() as conn:
                sessions = await repo.create_synthetic(conn, self._run_id, chats)
            log.info("load test %s: %d synthetic chats created", self._run_id, len(sessions))
            await self._run(sessions)
            status, partial = self._final_status()
        except Exception as exc:  # the harness itself broke: report what exists, claim nothing
            log.exception("load test %s: the workload failed", self._run_id)
            self._stop_reason = f"internal error ({type(exc).__name__})"
            status, partial = "failed", True
        finally:
            await self._finish_tasks()
            try:
                async with self._control.acquire() as conn:
                    removed = await repo.delete_synthetic(conn, self._run_id)
                log.info("load test %s: %d synthetic users removed", self._run_id, removed)
            except Exception:
                log.exception("load test %s: cleanup of synthetic rows failed", self._run_id)
        return self._outcome(status, partial)

    # ------------------------------------------------------------------ run loop

    def _intended(self, cfg: TestConfig, n: int) -> list[tuple[float, int]]:
        rng = random.Random(self._run_id)  # noqa: S311 - schedule jitter, not security
        interval = 60.0 / cfg.msgs_per_chat_min
        starts = []
        for i in range(n):
            joins = cfg.ramp_s * i / n if cfg.ramp_s else 0.0
            starts.append((joins + rng.random() * interval, i))
        heapq.heapify(starts)
        return starts

    async def _run(self, sessions: list[tuple[int, int, int]]) -> None:
        cfg = self._cfg
        interval = 60.0 / cfg.msgs_per_chat_min
        join_at = [
            cfg.ramp_s * i / len(sessions) if cfg.ramp_s else 0.0 for i in range(len(sessions))
        ]
        heap = self._intended(cfg, len(sessions))
        turn = [0] * len(sessions)
        self._start = self._last_window_at = time.monotonic()
        self._pm_start = (await self._probes.db()).postmaster_start
        collector = asyncio.create_task(self._collector(join_at), name="loadtest-collector")
        watcher = asyncio.create_task(self._watch_stop(), name="loadtest-stop")
        total = cfg.total_runtime_s
        try:
            while not self._stop.is_set():
                now = time.monotonic() - self._start
                if now >= total:
                    break
                while heap and heap[0][0] <= now:
                    due, i = heapq.heappop(heap)
                    sid, a, b = sessions[i]
                    sender = a if turn[i] % 2 == 0 else b
                    turn[i] += 1
                    if self._in_flight >= self._max_in_flight:
                        self._shed()
                    else:
                        self._spawn(sender, self._start + due)
                    # no catch-up bursts: a late chat resumes at the normal pace from now
                    heapq.heappush(heap, (max(due + interval, now + 0.001), i))
                await asyncio.sleep(TICK_SECONDS)
        finally:
            watcher.cancel()
            collector.cancel()
            for task in (watcher, collector):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._drain()
        await self._flush_window(final=True)

    async def _watch_stop(self) -> None:
        await self._external_stop.wait()
        if not self._stop.is_set():
            self._stop_reason = "stopped by the operator (process shutdown)"
            self._cancelled = True
            self._stop.set()

    def _spawn(self, sender: int, intended_at: float) -> None:
        self._in_flight += 1
        self._counter += 1
        task = asyncio.create_task(self._send(sender, intended_at, self._counter))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _shed(self) -> None:
        """The system could not take another message in time: that is a failed message."""
        self._window.failed += 1
        self._totals["failed"] += 1
        self._totals["shed"] += 1
        self._rejected["overload"] += 1

    async def _send(self, sender: int, intended_at: float, n: int) -> None:
        text = f"synthetic load test message {_word(n)}"
        try:
            await repo_users.get(self._pool, sender)
            if not self._limiter.allow("message", sender):
                result_status = "rate_limited"
            else:
                res = await self._relay.relay(sender, Incoming("text", text=text))
                result_status = res.status
                if res.status == "ok" and res.partner_id is not None:
                    self.delivery.deliver(res.partner_id, res.text)
        except (asyncpg.PostgresError, OSError, TimeoutError):
            self._window.db_errors += 1
            self._totals["db_errors"] += 1
            result_status = "db_error"
        finally:
            self._in_flight -= 1
        latency = (time.monotonic() - intended_at) * 1000
        self._window.latencies.append(latency)
        self._latencies.append(latency)
        if result_status == "ok":
            self._window.ok += 1
            self._totals["ok"] += 1
        else:
            self._window.failed += 1
            self._totals["failed"] += 1
            self._rejected[result_status] += 1

    async def _drain(self) -> None:
        if not self._tasks:
            return
        done, pending = await asyncio.wait(set(self._tasks), timeout=DRAIN_SECONDS)
        for task in pending:
            task.cancel()

    async def _finish_tasks(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------------ collector

    async def _collector(self, join_at: list[float]) -> None:
        loop_t = time.monotonic()
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            self._lag_ms = max(self._lag_ms, (now - loop_t - 0.1) * 1000)
            loop_t = now
            self._active_chats = sum(1 for j in join_at if j <= now - self._start)
            if now - self._last_window_at >= self._bucket:
                await self._flush_window()

    def _target_rate(self, elapsed: float) -> float:
        cfg = self._cfg
        if cfg.ramp_s and elapsed < cfg.ramp_s:
            active = min(cfg.chats, elapsed / cfg.ramp_s * cfg.chats)
            return active * cfg.msgs_per_chat_min / 60.0
        return cfg.msgs_per_s

    async def _flush_window(self, final: bool = False) -> None:
        now = time.monotonic()
        dt = now - self._last_window_at
        if dt <= 0 or (final and dt < 0.05 and self._windows):
            return
        self._last_window_at = now
        win, self._window = self._window, _Window()
        lag, self._lag_ms = self._lag_ms, 0.0
        elapsed = now - self._start
        stats = latency_stats(win.latencies)
        sys: HostSample = self._host.sample()
        db = await self._probes.db()
        bot_ok, web_ok = await self._probes.bot_ok(), await self._probes.web_ok()
        self._bot.update(bot_ok)
        self._web.update(web_ok)
        if db.postmaster_start is not None and self._pm_start not in (None, db.postmaster_start):
            self._db_restarts += 1
            self._pm_start = db.postmaster_start
        self._seq += 1
        row: dict[str, object] = {
            "seq": self._seq,
            "elapsed_s": round(elapsed, 2),
            "dt": dt,
            "active_chats": self._active_chats,
            "target_msgs_s": round(self._target_rate(elapsed - dt / 2), 2),
            "msgs_s": round(win.ok / dt, 2),
            "sent_ok": win.ok,
            "sent_failed": win.failed,
            "db_errors": win.db_errors + db.errors,
            "p50_ms": stats["p50"],
            "p95_ms": stats["p95"],
            "p99_ms": stats["p99"],
            "max_ms": stats["max"],
            "cpu_pct": sys.cpu_pct,
            "ram_pct": sys.ram_pct,
            "swap_pct": sys.swap_pct,
            "worker_cpu_pct": sys.worker_cpu_pct,
            "loop_lag_ms": round(lag, 1),
            "in_flight": self._in_flight,
            "db_connections": db.connections,
            "db_conn_pct": db.conn_pct,
            "db_latency_ms": db.latency_ms,
            "bot_ok": bot_ok,
            "web_ok": web_ok,
        }
        self._windows.append(row)
        try:
            await repo.insert_metric(self._control, self._run_id, row)
            beat = await repo.heartbeat(self._control, self._run_id)
            self._ctl_errors = 0
            if beat is not None and beat[0] and not self._stop.is_set():
                self._cancelled = True
                self._stop_reason = "stopped by the admin"
                self._stop.set()
        except (asyncpg.PostgresError, OSError, TimeoutError):
            self._ctl_errors += 1
        if self._on_window is not None:
            await self._on_window(row)
        if not final:
            self._failsafe_check(row, win)

    # ------------------------------------------------------------------ fail-safe

    def _trip(self, reason: str) -> None:
        if not self._stop.is_set():
            self._failsafe = True
            self._stop_reason = reason
            log.warning("load test %s: fail-safe stop: %s", self._run_id, reason)
            self._stop.set()

    def _failsafe_check(self, row: dict[str, object], win: _Window) -> None:
        t = self._t
        ram, cpu = row.get("ram_pct"), row.get("cpu_pct")
        self._ram_hot = (
            self._ram_hot + 1 if isinstance(ram, float) and ram >= t["failsafe_ram_pct"] else 0
        )
        if self._ram_hot >= 2:
            self._trip(f"memory critical ({ram:.0f}% used)")
        windows_needed = max(1, round(t["failsafe_cpu_seconds"] / self._bucket))
        self._cpu_hot = (
            self._cpu_hot + 1 if isinstance(cpu, float) and cpu >= t["failsafe_cpu_pct"] else 0
        )
        if self._cpu_hot >= windows_needed:
            self._trip(f"CPU critical ({cpu:.0f}% for {t['failsafe_cpu_seconds']:g}s)")
        pct = row.get("db_conn_pct")
        self._conn_hot = (
            self._conn_hot + 1 if isinstance(pct, float) and pct >= t["failsafe_db_conn_pct"] else 0
        )
        if self._conn_hot >= 2:
            self._trip(f"database connections exhausted ({pct:.0f}% of the server limit)")
        attempts = win.ok + win.failed
        if (
            attempts >= FAILSAFE_MIN_OPS
            and win.db_errors / attempts * 100 >= t["failsafe_db_error_pct"]
        ):
            self._trip(f"database errors ({win.db_errors} of {attempts} operations)")
        if self._db_restarts:
            self._trip("the database server restarted")
        for tracker in (self._bot, self._web):
            if tracker.consecutive >= t["failsafe_health_fails"]:
                self._trip(f"application health check failed ({tracker.name})")
        if (row.get("loop_lag_ms") or 0) > 10_000:
            self._trip("the load generator stalled")
        if self._ctl_errors >= 3:
            self._trip("the worker lost its database connection")

    # ------------------------------------------------------------------ result

    def _final_status(self) -> tuple[str, bool]:
        if self._failsafe:
            return "aborted", True
        if self._cancelled:
            return "cancelled", True
        return "completed", False

    def _outcome(self, status: str, partial: bool) -> Outcome:
        elapsed = time.monotonic() - self._start if self._start else 0.0
        restarts = self._bot.restarts + self._web.restarts + self._db_restarts
        detail = [
            f"{name} restarted {n}x"
            for name, n in (
                ("bot", self._bot.restarts),
                ("web", self._web.restarts),
                ("database", self._db_restarts),
            )
            if n
        ]
        totals = dict(self._totals) | {"rejected": dict(self._rejected)}
        summary = report.build_summary(
            self._windows,
            self._latencies,
            totals,
            self._cfg,
            restarts=restarts,
            restart_detail=detail,
            health_failures=self._bot.failures + self._web.failures,
            failsafe=self._failsafe,
            stop_reason=self._stop_reason,
            elapsed_s=elapsed,
        )
        summary["bot_observed"] = self._bot.observed
        summary["web_observed"] = self._web.observed
        summary["delivered_simulated"] = self.delivery.delivered
        if status == "failed":
            return Outcome(status, self._stop_reason, summary, None, [], True)
        verdict = evaluate_run(summary, self._t)
        return Outcome(
            status,
            self._stop_reason,
            summary,
            verdict.result,
            [r.as_json() for r in verdict.reasons],
            partial,
        )

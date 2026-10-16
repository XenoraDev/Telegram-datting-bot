"""The synthetic workload against a real PostgreSQL (short, real-time runs)."""

import ast
import asyncio
import dataclasses
import json
import re
from pathlib import Path

import asyncpg
import pytest

from bot.db import repo_chat
from bot.db import repo_loadtest as repo
from bot.loadtest import runner
from bot.loadtest.config import TestConfig
from bot.loadtest.evaluate import DEFAULT_THRESHOLDS
from bot.loadtest.metrics import HostSample
from bot.loadtest.runner import Workload
from bot.loadtest.safety import LoadTestSettings
from bot.loadtest.worker import process_run
from bot.services.loadtest import run_view
from tests.factories import make_user
from tests.loadtest.conftest import ADMIN, env_for, mark_database

CFG = TestConfig("custom", chats=6, msgs_per_chat_min=120, duration_s=2, ramp_s=0)  # 12 msg/s


@pytest.fixture
async def pools(dsn, app_pool):
    work = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    control = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    yield work, control
    await work.close()
    await control.close()


@pytest.fixture
async def settings(app_pool, dsn):
    await mark_database(app_pool, dsn)
    return LoadTestSettings.from_env(env_for(dsn))


async def new_run(pool, cfg=CFG, thresholds=None):
    rid = await repo.create_run(
        pool, ADMIN, cfg.as_json(), cfg.digest, thresholds or DEFAULT_THRESHOLDS
    )
    async with pool.acquire() as conn:
        claimed = await repo.claim_next(conn, "test-worker")
    assert claimed["id"] == rid
    return run_view(claimed)


class Sampler:
    """Deterministic host numbers."""

    def __init__(self, **kw):
        self.kw = {"cpu_pct": 20.0, "ram_pct": 30.0, "swap_pct": 0.0, "worker_cpu_pct": 10.0} | kw

    def sample(self):
        return HostSample(**self.kw)


def workload(pools, settings, rid, cfg=CFG, **kw):
    work, control = pools
    kw.setdefault("host", Sampler())
    return Workload(
        work, control, rid, cfg, dict(DEFAULT_THRESHOLDS), settings, bucket_seconds=0.3, **kw
    )


SNAPSHOT_SQL = {
    "users": "SELECT * FROM users ORDER BY id",
    "profiles": "SELECT * FROM profiles ORDER BY user_id",
    "blocks": "SELECT * FROM blocks ORDER BY blocker_id, blocked_id",
}
HISTORY_SQL = (
    "SELECT * FROM load_test_metrics",
    "SELECT * FROM load_test_runs",
    "SELECT * FROM load_test_audit",
)


async def snapshot(pool):
    return {t: [list(r.values()) for r in await pool.fetch(q)] for t, q in SNAPSHOT_SQL.items()}


async def synthetic_counts(pool):
    return (
        await pool.fetchval("SELECT count(*) FROM users WHERE id < 0"),
        await pool.fetchval("SELECT count(*) FROM chat_sessions WHERE user_a < 0 OR user_b < 0"),
        await pool.fetchval("SELECT count(*) FROM load_test_users"),
    )


# ------------------------------------------------------------------ the happy path


async def test_a_short_run_exercises_the_real_relay_and_cleans_up(app_pool, pools, settings):
    real_a, real_b = await make_user(app_pool, 101), await make_user(app_pool, 102)
    await app_pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (101, 102)")
    before = await snapshot(app_pool)
    run = await new_run(app_pool)
    seen = []

    async def on_window(row):
        seen.append(await synthetic_counts(app_pool))

    out = await workload(pools, settings, run["id"], on_window=on_window).execute()

    assert out.status == "completed" and not out.partial
    s = out.summary
    assert s["ok"] > 5 and s["failed"] == 0 and s["error_rate_pct"] == 0.0
    assert s["delivered_simulated"] == s["ok"]  # the delivery step only counts
    assert s["p95_ms"] is not None and s["p99_ms"] >= s["p95_ms"] >= s["p50_ms"]
    assert out.result in ("PASS", "WARNING")
    # aggregated windows, never one row per message
    n_windows = await app_pool.fetchval("SELECT count(*) FROM load_test_metrics")
    assert 2 <= n_windows <= 20 and n_windows < s["ok"] / 2
    # while it ran the synthetic rows existed (12 users / 6 chats); afterwards nothing remains
    assert any(c == (12, 6, 12) for c in seen), seen
    assert await synthetic_counts(app_pool) == (0, 0, 0)
    # real data is byte-for-byte what it was
    assert await snapshot(app_pool) == before
    assert real_a and real_b


async def test_synthetic_rows_are_isolated_from_real_data(app_pool, pools, settings):
    run = await new_run(app_pool)
    checked = []

    async def on_window(row):
        users = [r["id"] for r in await app_pool.fetch("SELECT id FROM users WHERE id < 0")]
        registry = {
            r["user_id"] for r in await app_pool.fetch("SELECT user_id FROM load_test_users")
        }
        sessions = await app_pool.fetch("SELECT user_a, user_b FROM chat_sessions")
        profiles = await app_pool.fetchval("SELECT count(*) FROM profiles")
        queue = await app_pool.fetchval("SELECT count(*) FROM chat_queue")
        checked.append(
            (
                set(users) == registry and len(users) == 12,
                all(a < 0 and b < 0 for a, b in sessions),
                profiles,
                queue,
            )
        )

    await workload(pools, settings, run["id"], on_window=on_window).execute()
    assert checked and all(c == (True, True, 0, 0) for c in checked), (
        checked
    )  # no profiles: never in browse


async def test_chat_history_is_never_stored(app_pool, pools, settings):
    run = await new_run(app_pool)
    await workload(pools, settings, run["id"]).execute()
    rows = [list(map(str, r.values())) for q in HISTORY_SQL for r in await app_pool.fetch(q)]
    dump = json.dumps(rows)
    assert "synthetic load test message" not in dump


async def test_the_workload_follows_the_target_rate(app_pool, pools, settings):
    run = await new_run(app_pool)
    out = await workload(pools, settings, run["id"]).execute()
    ok = out.summary["ok"]
    assert 0.7 * 24 <= ok <= 1.2 * 24 + 6  # 12 msg/s for 2 s
    assert out.summary["throughput_pct"] > 70


async def test_the_ramp_up_adds_chats_gradually(app_pool, pools, settings):
    cfg = dataclasses.replace(CFG, ramp_s=2, duration_s=1)
    run = await new_run(app_pool, cfg)
    await workload(pools, settings, run["id"], cfg).execute()
    rows = await repo.list_metrics(app_pool, run["id"])
    actives = [r["active_chats"] for r in rows]
    assert actives == sorted(actives) and actives[0] < cfg.chats <= actives[-1]


# ------------------------------------------------------------------ stopping and failing safe


async def test_the_admin_can_stop_a_running_test_and_gets_a_partial_report(
    app_pool, pools, settings
):
    cfg = dataclasses.replace(CFG, duration_s=30)
    run = await new_run(app_pool, cfg)
    await app_pool.execute("UPDATE load_test_runs SET status = 'running'")

    async def stop_soon():
        await asyncio.sleep(0.8)
        await repo.request_cancel(app_pool, run["id"], ADMIN)

    stopper = asyncio.create_task(stop_soon())
    started = asyncio.get_event_loop().time()
    out = await workload(pools, settings, run["id"], cfg).execute()
    await stopper
    assert asyncio.get_event_loop().time() - started < 6  # long before the 30 s were over
    assert out.status == "cancelled" and out.partial and out.stop_reason == "stopped by the admin"
    assert out.summary["ok"] > 0 and out.result in ("PASS", "WARNING", "FAIL")
    assert await synthetic_counts(app_pool) == (0, 0, 0)


async def test_the_process_stop_event_cancels_gracefully(app_pool, pools, settings):
    cfg = dataclasses.replace(CFG, duration_s=30)
    run = await new_run(app_pool, cfg)
    stop = asyncio.Event()
    asyncio.get_event_loop().call_later(0.7, stop.set)
    out = await workload(pools, settings, run["id"], cfg, stop_event=stop).execute()
    assert out.status == "cancelled" and out.partial
    assert await synthetic_counts(app_pool) == (0, 0, 0)


@pytest.mark.parametrize(
    "sample,needle",
    [
        ({"ram_pct": 99.0}, "memory critical"),
        ({"cpu_pct": 99.5}, "CPU critical"),
    ],
)
async def test_the_failsafe_stops_the_test_and_writes_a_partial_report(
    app_pool, pools, settings, sample, needle
):
    cfg = dataclasses.replace(CFG, duration_s=30)
    thresholds = dict(DEFAULT_THRESHOLDS) | {"failsafe_cpu_seconds": 5.0}
    run = await new_run(app_pool, cfg, thresholds)
    work, control = pools
    w = Workload(
        work,
        control,
        run["id"],
        cfg,
        thresholds,
        settings,
        bucket_seconds=0.3,
        host=Sampler(**sample),
    )
    out = await w.execute()
    assert out.status == "aborted" and out.partial and needle in out.stop_reason
    assert out.result == "FAIL" and any(r["code"] == "failsafe" for r in out.reasons)
    assert out.summary["failsafe"] is True
    assert await synthetic_counts(app_pool) == (0, 0, 0)


async def test_database_errors_trip_the_failsafe(app_pool, pools, settings, monkeypatch):
    async def broken(db, uid):
        raise asyncpg.PostgresConnectionError("boom")

    monkeypatch.setattr(repo_chat, "get_partner", broken)
    cfg = dataclasses.replace(CFG, duration_s=30)
    run = await new_run(app_pool, cfg)
    out = await workload(pools, settings, run["id"], cfg).execute()
    assert out.status == "aborted" and "database errors" in out.stop_reason
    assert out.summary["db_errors"] > 0 and out.summary["error_rate_pct"] > 50
    assert out.result == "FAIL"


async def test_an_overloaded_system_sheds_load_and_the_run_fails(
    app_pool, pools, settings, monkeypatch
):
    from bot.services.chat_relay import ChatRelayService

    real = ChatRelayService.relay

    async def slow(self, sender, msg):
        await asyncio.sleep(0.4)
        return await real(self, sender, msg)

    monkeypatch.setattr(ChatRelayService, "relay", slow)
    run = await new_run(app_pool)
    out = await workload(pools, settings, run["id"], max_in_flight=3).execute()
    assert out.summary["shed"] > 0 and out.summary["rejected"]["overload"] == out.summary["shed"]
    assert out.summary["throughput_pct"] < 90 and out.result == "FAIL"


async def test_a_workload_crash_still_produces_a_report(app_pool, pools, settings, monkeypatch):
    async def explode(*a, **k):
        raise RuntimeError("harness bug with secret-ish detail")

    monkeypatch.setattr(repo, "create_synthetic", explode)
    run = await new_run(app_pool)
    out = await workload(pools, settings, run["id"]).execute()
    assert out.status == "failed" and out.partial and out.result is None
    assert out.stop_reason == "internal error (RuntimeError)"  # no message detail leaks out


async def test_restarts_of_monitored_services_are_counted(app_pool, pools, settings):
    class Flaky(runner.Probes):
        def __init__(self, control, settings):
            super().__init__(control, settings)
            self.calls = 0

        async def web_ok(self):
            self.calls += 1
            return self.calls not in (3, 4)  # two failed probes, then healthy again

    work, control = pools
    cfg = dataclasses.replace(CFG, duration_s=3)
    run = await new_run(app_pool, cfg)
    w = Workload(
        work,
        control,
        run["id"],
        cfg,
        dict(DEFAULT_THRESHOLDS),
        settings,
        bucket_seconds=0.3,
        host=Sampler(),
        probes=Flaky(control, settings),
    )
    out = await w.execute()
    assert out.summary["restarts"] == 1 and out.result == "FAIL"
    assert "web restarted 1x" in out.summary["restart_detail"]


# ------------------------------------------------------------------ the worker's job handling


async def test_process_run_end_to_end(app_pool, pools, settings):
    cfg = dataclasses.replace(CFG, duration_s=1)
    run = await new_run(app_pool, cfg)
    work, control = pools
    status = await process_run(run, work, control, settings, bucket_seconds=0.3, host=Sampler())
    row = await repo.get_run(app_pool, run["id"])
    assert status == "completed" and row["status"] == "completed" and row["finished_at"]
    assert row["result"] in ("PASS", "WARNING") and json.loads(row["summary"])["ok"] > 0
    info = json.loads(row["server_info"])
    assert info["cpu_count"] and "postgres" in info and "python" in info
    env = json.loads(row["environment"])
    assert env and all(c["ok"] for c in env)
    acts = [
        r["action"] for r in await app_pool.fetch("SELECT action FROM load_test_audit ORDER BY id")
    ]
    assert acts == ["started", "completed"]
    assert await synthetic_counts(app_pool) == (0, 0, 0)


async def test_the_worker_refuses_an_unsafe_environment(app_pool, pools, dsn, caplog):
    unsafe = LoadTestSettings.from_env(env_for(dsn, APP_ENV="production"))
    await mark_database(app_pool, dsn)
    run = await new_run(app_pool)
    work, control = pools
    status = await process_run(run, work, control, unsafe, host=Sampler())
    row = await repo.get_run(app_pool, run["id"])
    assert status == "blocked" and row["status"] == "blocked"
    assert "LOAD TEST BLOCKED" in row["stop_reason"] and row["finished_at"]
    assert await synthetic_counts(app_pool) == (0, 0, 0)  # nothing was even created
    assert await app_pool.fetchval("SELECT count(*) FROM load_test_metrics") == 0
    assert [r["action"] for r in await app_pool.fetch("SELECT action FROM load_test_audit")] == [
        "blocked"
    ]


async def test_a_job_stopped_while_starting_never_runs(app_pool, pools, settings):
    run = await new_run(app_pool)
    await repo.request_cancel(app_pool, run["id"], ADMIN)  # status becomes 'stopping'
    work, control = pools
    status = await process_run(run, work, control, settings, host=Sampler())
    assert status == "cancelled"
    assert await synthetic_counts(app_pool) == (0, 0, 0)


async def test_leftovers_of_a_crashed_worker_are_cleaned_up(lt, app_pool):
    rid = await repo.create_run(app_pool, ADMIN, CFG.as_json(), CFG.digest, DEFAULT_THRESHOLDS)
    async with app_pool.acquire() as conn:
        await repo.create_synthetic(conn, rid, 3)
    await make_user(app_pool, 777)
    assert await synthetic_counts(app_pool) == (6, 3, 6)
    await app_pool.execute(
        "UPDATE load_test_runs SET status='running', heartbeat_at = now() - interval '9 minutes'"
    )
    assert await lt.reap_stale() == 1
    assert await synthetic_counts(app_pool) == (0, 0, 0)
    assert await app_pool.fetchval("SELECT count(*) FROM users WHERE id = 777") == 1


# ----------------------------------------------------------- no Telegram, nothing destructive


def test_the_workload_code_cannot_reach_telegram():
    forbidden_calls = {"Bot", "send_message", "send_sticker", "send_photo", "copy_message"}
    for path in Path("bot/loadtest").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not name.startswith("aiogram"), (path, name)
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                assert called not in forbidden_calls, (path, called)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "api.telegram.org" not in node.value, path


async def test_no_telegram_call_is_possible_during_a_run(app_pool, pools, settings, monkeypatch):
    from aiogram.client.session.base import BaseSession

    async def forbidden(*a, **k):
        raise AssertionError("a Telegram API call was attempted")

    monkeypatch.setattr(BaseSession, "make_request", forbidden)
    run = await new_run(app_pool)
    out = await workload(pools, settings, run["id"]).execute()
    assert out.status == "completed" and out.summary["ok"] > 0


def test_the_module_contains_no_destructive_sql():
    sources = (
        [Path("bot/db/repo_loadtest.py")]
        + list(Path("bot/loadtest").glob("*.py"))
        + [Path("bot/services/loadtest.py")]
    )
    for path in sources:
        text = path.read_text()
        assert not re.search(r"\b(DROP|TRUNCATE|ALTER|GRANT|REVOKE|VACUUM)\b", text), path
        for match in re.finditer(r"DELETE FROM (\w+)", text):
            assert path.name == "repo_loadtest.py", path
            assert match.group(1) in {"chat_sessions", "restrictions", "users"}


async def test_every_statement_of_a_full_run_is_non_destructive(
    app_pool, pools, settings, monkeypatch
):
    executed = []
    for name in ("execute", "executemany", "fetch", "fetchrow", "fetchval"):
        original = getattr(asyncpg.Connection, name)

        def spy(self, query, *args, _orig=original, **kw):
            executed.append(query)
            return _orig(self, query, *args, **kw)

        monkeypatch.setattr(asyncpg.Connection, name, spy)
    cfg = dataclasses.replace(CFG, duration_s=1)
    run = await new_run(app_pool, cfg)
    work, control = pools
    await process_run(run, work, control, settings, bucket_seconds=0.3, host=Sampler())
    assert executed
    for q in executed:
        assert not re.search(r"\b(DROP|TRUNCATE|ALTER|CREATE|GRANT)\b", q, re.I), q
        if re.search(r"\bDELETE\b", q, re.I):
            assert "< 0" in q, q  # only ever synthetic (negative) ids
        if re.search(r"\bUPDATE\b", q, re.I):
            assert "load_test_" in q, q  # only the module's own tables


async def test_secrets_never_reach_the_stored_report(app_pool, pools, settings, dsn):
    cfg = dataclasses.replace(CFG, duration_s=1)
    run = await new_run(app_pool, cfg)
    work, control = pools
    await process_run(run, work, control, settings, bucket_seconds=0.3, host=Sampler())
    row = await repo.get_run(app_pool, run["id"])
    dump = json.dumps({k: str(v) for k, v in dict(row).items()})
    for secret in ("TEST-token-not-real", "7777777:", "pgtest@", "postgresql://", "SECRET"):
        assert secret not in dump, secret

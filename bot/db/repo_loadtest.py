"""SQL for the Server Capacity / Load Testing module (tables from migration 011).

Rules kept here: parameterized SQL only; the only DELETE statements remove the synthetic rows of
ONE run, always restricted to negative ids that the run itself registered. Nothing here touches
real users, payments, subscriptions or chats.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import asyncpg

from ..loadtest.safety import DbIdentity
from .repo_users import Db

ACTIVE = ("queued", "starting", "running", "stopping")
FINISHED = ("completed", "cancelled", "aborted", "blocked", "failed")
USER_ID_BLOCK = 10_000  # synthetic ids of run N lie in (-(N+1)*10000, -N*10000)


# ----------------------------------------------------------------------------- environment


async def database_identity(db: Db) -> DbIdentity:
    row = await db.fetchrow(
        """SELECT current_database() AS name,
                  coalesce(shobj_description(d.oid, 'pg_database'), '') AS comment,
                  (SELECT count(*) FROM users u
                    WHERE u.id < 0
                      AND NOT EXISTS (SELECT 1 FROM load_test_users l WHERE l.user_id = u.id)
                  ) AS foreign_negative
           FROM pg_database d WHERE d.datname = current_database()"""
    )
    return DbIdentity(row["name"], row["comment"], int(row["foreign_negative"]))


async def postgres_version(db: Db) -> str:
    return str(await db.fetchval("SHOW server_version"))[:40]


# ----------------------------------------------------------------------------- audit


async def audit(
    db: Db, admin_id: int, action: str, run_id: int | None = None, detail: str | None = None
) -> None:
    await db.execute(
        "INSERT INTO load_test_audit (admin_id, action, run_id, detail) VALUES ($1, $2, $3, $4)",
        admin_id,
        action,
        run_id,
        detail[:300] if detail else None,
    )


async def list_audit(db: Db, limit: int = 30) -> list[asyncpg.Record]:
    return await db.fetch(
        "SELECT id, admin_id, action, run_id, detail, created_at FROM load_test_audit "
        "ORDER BY id DESC LIMIT $1",
        limit,
    )


# ----------------------------------------------------------------------------- thresholds


async def thresholds(db: Db) -> dict[str, float]:
    return {
        r["key"]: float(r["value"])
        for r in await db.fetch("SELECT key, value FROM load_test_settings")
    }


async def save_thresholds(
    conn: asyncpg.Connection, admin_id: int, values: Mapping[str, float]
) -> None:
    await conn.executemany(
        """INSERT INTO load_test_settings (key, value, updated_by) VALUES ($1, $2, $3)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
               updated_by = EXCLUDED.updated_by, updated_at = now()""",
        [(k, float(v), admin_id) for k, v in values.items()],
    )


# ----------------------------------------------------------------------------- confirmations


async def supersede_pending(db: Db, admin_id: int) -> int:
    """Any earlier unanswered confirmation of this admin stops being valid."""
    status = await db.execute(
        "UPDATE load_test_confirmations SET state = 'superseded', decided_at = now() "
        "WHERE admin_id = $1 AND state = 'pending'",
        admin_id,
    )
    return int(status.split()[-1])


async def create_confirmation(
    db: Db,
    token_hash: str,
    admin_id: int,
    config: Mapping[str, object],
    digest: str,
    ttl_seconds: int,
) -> int:
    return await db.fetchval(
        """INSERT INTO load_test_confirmations
               (token_hash, admin_id, config, config_hash, expires_at)
           VALUES ($1, $2, $3::jsonb, $4, now() + make_interval(secs => $5))
           RETURNING id""",
        token_hash,
        admin_id,
        json.dumps(dict(config)),
        digest,
        float(ttl_seconds),
    )


async def set_confirmation_message(db: Db, cid: int, chat_id: int, message_id: int) -> None:
    await db.execute(
        "UPDATE load_test_confirmations SET tg_chat_id = $2, tg_message_id = $3 WHERE id = $1",
        cid,
        chat_id,
        message_id,
    )


async def mark_confirmation(db: Db, cid: int, state: str) -> None:
    await db.execute(
        "UPDATE load_test_confirmations SET state = $2, decided_at = now() "
        "WHERE id = $1 AND state = 'pending'",
        cid,
        state,
    )


_CONF_BY_HASH = (
    "SELECT *, expires_at <= now() AS is_expired FROM load_test_confirmations WHERE token_hash = $1"
)


async def confirmation_by_hash(db: Db, token_hash: str, *, lock: bool = False):
    return await db.fetchrow(_CONF_BY_HASH + (" FOR UPDATE" if lock else ""), token_hash)


async def latest_confirmation(db: Db, admin_id: int):
    return await db.fetchrow(
        "SELECT id, state, config, config_hash, created_at, expires_at, run_id, "
        "       expires_at <= now() AS is_expired "
        "FROM load_test_confirmations WHERE admin_id = $1 ORDER BY id DESC LIMIT 1",
        admin_id,
    )


async def consume_confirmation(db: Db, cid: int, run_id: int) -> bool:
    status = await db.execute(
        "UPDATE load_test_confirmations SET state = 'consumed', decided_at = now(), run_id = $2 "
        "WHERE id = $1 AND state = 'pending' AND expires_at > now()",
        cid,
        run_id,
    )
    return status == "UPDATE 1"


# ----------------------------------------------------------------------------- runs

_RUN_COLS = (
    "id, status, config, config_hash, thresholds, requested_by, environment, server_info, "
    "commit_hash, worker_id, created_at, started_at, finished_at, heartbeat_at, "
    "cancel_requested_by, cancel_requested_at, stop_reason, summary, result, result_reasons, "
    "partial"
)
_SELECT_RUN = "SELECT " + _RUN_COLS + " FROM load_test_runs "  # noqa: S608


async def active_run(db: Db):
    return await db.fetchrow(_SELECT_RUN + "WHERE status = ANY($1) LIMIT 1", list(ACTIVE))


async def create_run(
    db: Db,
    admin_id: int,
    config: Mapping[str, object],
    digest: str,
    thresholds_: Mapping[str, float],
) -> int:
    """Raises asyncpg.UniqueViolationError when another test is active (partial unique index)."""
    return await db.fetchval(
        "INSERT INTO load_test_runs (config, config_hash, thresholds, requested_by) "
        "VALUES ($1::jsonb, $2, $3::jsonb, $4) RETURNING id",
        json.dumps(dict(config)),
        digest,
        json.dumps(dict(thresholds_)),
        admin_id,
    )


async def get_run(db: Db, run_id: int):
    return await db.fetchrow(_SELECT_RUN + "WHERE id = $1", run_id)


async def list_runs(db: Db, limit: int = 100):
    return await db.fetch(_SELECT_RUN + "ORDER BY id DESC LIMIT $1", limit)


async def finished_runs(db: Db):
    return await db.fetch(_SELECT_RUN + "WHERE status = ANY($1) ORDER BY id", list(FINISHED))


_CLAIM = (
    "UPDATE load_test_runs SET status = 'starting', worker_id = $2, started_at = now(), "  # noqa: S608
    "heartbeat_at = now() WHERE id = $1 RETURNING " + _RUN_COLS
)


async def claim_next(conn: asyncpg.Connection, worker_id: str):
    """A worker takes the oldest queued job (queued -> starting). None when there is none."""
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT id FROM load_test_runs WHERE status = 'queued' "
            "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        if row is None:
            return None
        return await conn.fetchrow(_CLAIM, row["id"], worker_id)


async def set_run_status(
    db: Db, run_id: int, status: str, *, only_from: Sequence[str] = ACTIVE
) -> bool:
    out = await db.execute(
        "UPDATE load_test_runs SET status = $2 WHERE id = $1 AND status = ANY($3)",
        run_id,
        status,
        list(only_from),
    )
    return out == "UPDATE 1"


async def set_run_environment(
    db: Db, run_id: int, env: object, info: object, commit: str | None
) -> None:
    await db.execute(
        "UPDATE load_test_runs SET environment = $2::jsonb, server_info = $3::jsonb, "
        "commit_hash = $4 WHERE id = $1",
        run_id,
        json.dumps(env),
        json.dumps(info),
        commit,
    )


async def heartbeat(db: Db, run_id: int) -> tuple[bool, str] | None:
    """Touch the run; returns (cancel_requested, status) or None if the run vanished."""
    row = await db.fetchrow(
        "UPDATE load_test_runs SET heartbeat_at = now() WHERE id = $1 "
        "RETURNING cancel_requested_at IS NOT NULL AS cancel, status",
        run_id,
    )
    return (row["cancel"], row["status"]) if row else None


async def heartbeat_age(db: Db, run_id: int) -> float | None:
    """Seconds since the worker last reported for this run."""
    return await db.fetchval(
        "SELECT extract(epoch FROM now() - heartbeat_at)::float8 FROM load_test_runs WHERE id = $1",
        run_id,
    )


async def request_cancel(db: Db, run_id: int, admin_id: int) -> str | None:
    """Ask a run to stop. A job no worker has claimed yet is cancelled on the spot.
    Returns the new status, or None when the run is not active."""
    row = await db.fetchrow(
        """UPDATE load_test_runs
              SET cancel_requested_by = $2, cancel_requested_at = now(),
                  status = CASE WHEN status = 'queued' THEN 'cancelled'
                                WHEN status = 'running' THEN 'stopping' ELSE status END,
                  finished_at = CASE WHEN status = 'queued' THEN now() END,
                  partial = CASE WHEN status = 'queued' THEN true ELSE partial END,
                  stop_reason = CASE WHEN status = 'queued' THEN 'cancelled before it started'
                                     ELSE stop_reason END
            WHERE id = $1 AND status = ANY($3)
        RETURNING status""",
        run_id,
        admin_id,
        list(ACTIVE),
    )
    return row["status"] if row else None


async def finish_run(
    db: Db,
    run_id: int,
    status: str,
    *,
    stop_reason: str | None,
    summary: Mapping[str, object] | None,
    result: str | None,
    reasons: Sequence[Mapping[str, str]] | None,
    partial: bool,
) -> bool:
    out = await db.execute(
        """UPDATE load_test_runs
              SET status = $2, finished_at = now(), stop_reason = $3, summary = $4::jsonb,
                  result = $5, result_reasons = $6::jsonb, partial = $7
            WHERE id = $1 AND status = ANY($8)""",
        run_id,
        status,
        stop_reason[:300] if stop_reason else None,
        json.dumps(summary) if summary is not None else None,
        result,
        json.dumps(list(reasons)) if reasons is not None else None,
        partial,
        list(ACTIVE),
    )
    return out == "UPDATE 1"


async def stale_runs(db: Db, seconds: float):
    """Running jobs whose worker stopped reporting (crashed or killed)."""
    return await db.fetch(
        "SELECT id FROM load_test_runs WHERE status IN ('starting', 'running', 'stopping') "
        "AND heartbeat_at < now() - make_interval(secs => $1)",
        seconds,
    )


async def expire_unclaimed(db: Db, seconds: float) -> list[int]:
    """Queued jobs that no worker picked up in time are cancelled, so a test can never start
    unexpectedly long after the admin confirmed it."""
    rows = await db.fetch(
        "UPDATE load_test_runs SET status = 'cancelled', finished_at = now(), partial = true, "
        "stop_reason = 'no worker picked the job up in time' "
        "WHERE status = 'queued' AND created_at < now() - make_interval(secs => $1) RETURNING id",
        seconds,
    )
    return [r["id"] for r in rows]


# ----------------------------------------------------------------------------- workers


async def worker_beat(db: Db, worker_id: str, state: str, detail: str | None = None) -> None:
    await db.execute(
        """INSERT INTO load_test_workers (worker_id, state, detail) VALUES ($1, $2, $3)
           ON CONFLICT (worker_id) DO UPDATE SET heartbeat_at = now(), state = EXCLUDED.state,
               detail = EXCLUDED.detail""",
        worker_id,
        state,
        detail[:200] if detail else None,
    )


async def workers(db: Db):
    return await db.fetch(
        "SELECT worker_id, state, detail, started_at, heartbeat_at, "
        "       extract(epoch FROM now() - heartbeat_at) AS age_s "
        "FROM load_test_workers ORDER BY heartbeat_at DESC LIMIT 5"
    )


# ----------------------------------------------------------------------------- metrics

_METRIC_COLS = (
    "seq", "ts", "elapsed_s", "active_chats", "target_msgs_s", "msgs_s", "sent_ok", "sent_failed",
    "db_errors", "p50_ms", "p95_ms", "p99_ms", "max_ms", "cpu_pct", "ram_pct", "swap_pct",
    "worker_cpu_pct", "loop_lag_ms", "in_flight", "db_connections", "db_conn_pct",
    "db_latency_ms", "bot_ok", "web_ok",
)  # fmt: skip


_METRIC_FIELDS = [c for c in _METRIC_COLS if c != "ts"]
_METRIC_SELECT = "SELECT " + ", ".join(_METRIC_COLS) + " FROM load_test_metrics "  # noqa: S608
_METRIC_INSERT = (
    "INSERT INTO load_test_metrics (run_id, ts, "  # noqa: S608
    + ", ".join(_METRIC_FIELDS)
    + ") VALUES ($1, now(), "
    + ", ".join(f"${i + 2}" for i in range(len(_METRIC_FIELDS)))
    + ") ON CONFLICT DO NOTHING"
)


async def insert_metric(db: Db, run_id: int, row: Mapping[str, object]) -> None:
    data = {"db_errors": 0} | dict(row)
    await db.execute(_METRIC_INSERT, run_id, *[data.get(c) for c in _METRIC_FIELDS])


async def list_metrics(db: Db, run_id: int, limit: int = 5000):
    return await db.fetch(_METRIC_SELECT + "WHERE run_id = $1 ORDER BY seq LIMIT $2", run_id, limit)


async def latest_metric(db: Db, run_id: int):
    return await db.fetchrow(_METRIC_SELECT + "WHERE run_id = $1 ORDER BY seq DESC LIMIT 1", run_id)


# ----------------------------------------------------------------------------- synthetic data


def synthetic_ids(run_id: int, chats: int) -> list[tuple[int, int]]:
    """The (user_a, user_b) pairs of a run: negative ids inside the run's own block."""
    if chats * 2 >= USER_ID_BLOCK:
        raise ValueError("too many synthetic users for one id block")
    base = -run_id * USER_ID_BLOCK
    return [(base - (2 * i + 1), base - (2 * i + 2)) for i in range(chats)]


async def create_synthetic(
    conn: asyncpg.Connection, run_id: int, chats: int
) -> list[tuple[int, int, int]]:
    """Create the synthetic users and their active chat sessions. Returns (session, a, b)."""
    pairs = synthetic_ids(run_id, chats)
    ids = [u for pair in pairs for u in pair]
    if any(u >= 0 for u in ids):
        raise ValueError("synthetic ids must be negative")
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO users (id, username, status, status_reason, age_confirmed_at) "
            "SELECT i, NULL, 'active', 'load_test_synthetic', now() FROM unnest($1::bigint[]) AS i",
            ids,
        )
        await conn.execute(
            "INSERT INTO load_test_users (user_id, run_id) "
            "SELECT i, $2 FROM unnest($1::bigint[]) AS i",
            ids,
            run_id,
        )
        sessions = await conn.fetch(
            "INSERT INTO chat_sessions (user_a, user_b) "
            "SELECT a, b FROM unnest($1::bigint[], $2::bigint[]) AS t(a, b) "
            "RETURNING id, user_a, user_b",
            [a for a, _ in pairs],
            [b for _, b in pairs],
        )
        await conn.execute(
            "INSERT INTO chat_session_members (session_id, user_id) "
            "SELECT s, u FROM unnest($1::bigint[], $2::bigint[]) AS t(s, u)",
            [r["id"] for r in sessions] * 2,
            [r["user_a"] for r in sessions] + [r["user_b"] for r in sessions],
        )
    return [(r["id"], r["user_a"], r["user_b"]) for r in sessions]


async def delete_synthetic(conn: asyncpg.Connection, run_id: int) -> int:
    """Remove exactly the rows this run registered (negative ids only). Returns users removed."""
    async with conn.transaction():
        ids = [
            r["user_id"]
            for r in await conn.fetch(
                "SELECT user_id FROM load_test_users WHERE run_id = $1 AND user_id < 0", run_id
            )
        ]
        if not ids:
            return 0
        await conn.execute(
            "DELETE FROM chat_sessions WHERE user_a = ANY($1) AND user_b = ANY($1) "
            "AND user_a < 0 AND user_b < 0",
            ids,
        )
        await conn.execute("DELETE FROM restrictions WHERE user_id = ANY($1) AND user_id < 0", ids)
        out = await conn.execute("DELETE FROM users WHERE id = ANY($1) AND id < 0", ids)
    return int(out.split()[-1])


async def orphan_runs(db: Db):
    """Finished runs that still own synthetic users (a worker died before cleaning up)."""
    return await db.fetch(
        "SELECT DISTINCT l.run_id FROM load_test_users l JOIN load_test_runs r ON r.id = l.run_id "
        "WHERE r.status = ANY($1)",
        list(FINISHED),
    )

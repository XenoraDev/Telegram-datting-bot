"""Migration 014 (queue statistics): additive only, safe on a live database."""

import hashlib
import shutil
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate
from tests.db.test_p13_migration import APPLIED as APPLIED_001_012

APPLIED = {
    **APPLIED_001_012,
    "013_expiry_reminders.sql": "e1062cd88d02312c427f43153814e9e519870d4f6096f0a1c547a80c27d89d97",
}


def live(tmp_path: Path, upto: int = 13) -> Path:
    old = tmp_path / "live"
    old.mkdir()
    for f in sorted(Path(DEFAULT_MIGRATIONS_DIR).glob("*.sql")):
        if int(f.name[:3]) <= upto:
            shutil.copy(f, old / f.name)
    return old


async def snapshot(pool):
    cols = await pool.fetch(
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "ORDER BY table_name, ordinal_position"
    )
    cons = await pool.fetch(
        "SELECT conrelid::regclass::text AS t, conname, pg_get_constraintdef(oid) AS d "
        "FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
    )
    return [dict(r) for r in cols], [dict(r) for r in cons]


def test_applied_migrations_001_to_013_are_byte_identical():
    for name, digest in APPLIED.items():
        assert hashlib.sha256(Path(DEFAULT_MIGRATIONS_DIR, name).read_bytes()).hexdigest() == digest


async def test_014_adds_one_table_and_changes_nothing_else(pool, tmp_path):
    await migrate(pool, live(tmp_path))
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    before_cols, before_cons = await snapshot(pool)
    rows = {
        t: [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        for t in ("users", "admin_actions", "plans", "subscriptions", "expiry_reminders")
    }

    assert await migrate(pool) == ["014_queue_stats"]

    after_cols, after_cons = await snapshot(pool)
    old = {c["table_name"] for c in before_cols}
    assert [c for c in after_cols if c["table_name"] in old] == before_cols
    assert [c for c in after_cons if c["t"] in old] == before_cons
    assert {c["table_name"] for c in after_cols} - old == {"queue_stats"}
    for t, expected in rows.items():
        got = [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        assert got == expected, t
    assert await migrate(pool) == []


async def test_the_file_is_purely_additive():
    sql = (Path(DEFAULT_MIGRATIONS_DIR) / "014_queue_stats.sql").read_text().upper()
    for forbidden in ("ALTER TABLE", "DROP ", "DELETE FROM", "TRUNCATE", "UPDATE "):
        assert forbidden not in sql, forbidden


async def test_the_table_holds_only_counters_no_user_and_no_text(app_pool):
    cols = {
        r["column_name"]: r["data_type"]
        for r in await app_pool.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'queue_stats'"
        )
    }
    assert set(cols) == {
        "day", "hour", "instant", "waited", "wait_sum_seconds", "expired", "cancelled"
    }  # fmt: skip
    assert set(cols.values()) <= {"date", "smallint", "integer", "bigint"}  # numbers and a date


async def test_the_counters_reject_junk_and_one_row_per_hour(app_pool):
    ins = "INSERT INTO queue_stats (day, hour) VALUES ('2030-01-01', $1)"
    await app_pool.execute(ins, 5)
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_pool.execute(ins, 5)
    for hour in (-1, 24):
        with pytest.raises(asyncpg.CheckViolationError):
            await app_pool.execute(ins, hour)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("UPDATE queue_stats SET expired = -1")

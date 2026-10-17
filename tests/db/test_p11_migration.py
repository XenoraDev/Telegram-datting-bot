"""Migration 011 (load testing): additive only, on fresh and on existing databases."""

import shutil
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate

NEW_TABLES = {
    "load_test_runs",
    "load_test_metrics",
    "load_test_confirmations",
    "load_test_settings",
    "load_test_users",
    "load_test_audit",
    "load_test_workers",
}


def live(tmp_path: Path, upto: int = 10, name: str = "live") -> Path:
    old = tmp_path / name
    old.mkdir()
    for f in sorted(Path(DEFAULT_MIGRATIONS_DIR).glob("*.sql")):
        if int(f.name[:3]) <= upto:
            shutil.copy(f, old / f.name)
    return old


async def schema(pool):
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


async def test_011_changes_no_existing_table_or_constraint_and_no_row(pool, tmp_path):
    await migrate(pool, live(tmp_path))
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    await pool.execute(
        "INSERT INTO profiles (user_id, name, age, gender, looking_for, city, bio) "
        "VALUES (1, 'A', 30, 'f', 'any', 'Pune', 'b')"
    )
    await pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (9, 'ban')")
    before_cols, before_cons = await schema(pool)
    rows_before = {
        t: [dict(r) for r in await pool.fetch(q)]
        for t, q in (
            ("users", "SELECT * FROM users ORDER BY id"),
            ("profiles", "SELECT * FROM profiles ORDER BY user_id"),
            ("plans", "SELECT * FROM plans ORDER BY id"),
            ("admin_actions", "SELECT * FROM admin_actions ORDER BY id"),
            ("tier_settings", "SELECT * FROM tier_settings ORDER BY 1, 2"),
        )
    }

    upto11 = live(tmp_path, 11, "upto11")  # 012 is a later change, checked in its own test
    assert await migrate(pool, upto11) == ["011_load_testing"]

    after_cols, after_cons = await schema(pool)
    old = {c["table_name"] for c in before_cols}
    assert [c for c in after_cols if c["table_name"] in old] == before_cols  # untouched
    assert [c for c in after_cons if c["t"] in old] == before_cons
    assert {c["table_name"] for c in after_cols} - old == NEW_TABLES
    for t, expected in rows_before.items():
        q = f"SELECT * FROM {t}"  # noqa: S608
        order = {"profiles": "user_id", "tier_settings": "1, 2"}.get(t, "id")
        assert [dict(r) for r in await pool.fetch(f"{q} ORDER BY {order}")] == expected, t
    assert await migrate(pool, upto11) == []


async def test_a_fresh_database_is_migrated_in_full(app_pool):
    versions = [
        r["version"]
        for r in await app_pool.fetch("SELECT version FROM schema_migrations ORDER BY 1")
    ]
    assert "011_load_testing" in versions and len(versions) == 14


async def test_the_migration_file_never_alters_or_drops_anything():
    sql = (Path(DEFAULT_MIGRATIONS_DIR) / "011_load_testing.sql").read_text().upper()
    sql = sql.replace(
        "BEFORE UPDATE OR DELETE ON", ""
    )  # the append-only guard of the new audit table
    for forbidden in ("ALTER TABLE", "DROP ", "DELETE FROM", "TRUNCATE", "UPDATE "):
        assert forbidden not in sql, forbidden


async def test_one_active_run_is_a_database_guarantee(app_pool):
    ins = (
        "INSERT INTO load_test_runs (status, config, config_hash, thresholds, requested_by) "
        "VALUES ($1, '{}'::jsonb, $2, '{}'::jsonb, 1)"
    )
    await app_pool.execute(ins, "queued", "a" * 64)
    for status in ("queued", "starting", "running", "stopping"):
        with pytest.raises(asyncpg.UniqueViolationError):
            await app_pool.execute(ins, status, "b" * 64)
    await app_pool.execute("UPDATE load_test_runs SET status = 'completed', finished_at = now()")
    await app_pool.execute(ins, "queued", "b" * 64)  # a finished run does not block the next


async def test_constraints_reject_junk(app_pool):
    with pytest.raises(asyncpg.CheckViolationError):  # finished without a finish time
        await app_pool.execute(
            "INSERT INTO load_test_runs (status, config, config_hash, thresholds, requested_by) "
            "VALUES ('completed', '{}', $1, '{}', 1)", "a" * 64,
        )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(
            "INSERT INTO load_test_runs (status, config, config_hash, thresholds, requested_by) "
            "VALUES ('queued', '{}', 'nothex', '{}', 1)"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(
            "INSERT INTO load_test_runs (status, config, config_hash, thresholds, requested_by) "
            "VALUES ('exploding', '{}', $1, '{}', 1)", "a" * 64,
        )  # fmt: skip
    await app_pool.execute("INSERT INTO users (id, status) VALUES (5, 'onboarding')")
    rid = await app_pool.fetchval(
        "INSERT INTO load_test_runs (config, config_hash, thresholds, requested_by) "
        "VALUES ('{}', $1, '{}', 1) RETURNING id", "a" * 64,
    )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):  # a real (positive) id is not a synthetic user
        await app_pool.execute("INSERT INTO load_test_users (user_id, run_id) VALUES (5, $1)", rid)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(
            "INSERT INTO load_test_confirmations (token_hash, admin_id, config, config_hash, "
            "expires_at) VALUES ('zz', 1, '{}', $1, now() + interval '1 minute')", "a" * 64,
        )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(
            "INSERT INTO load_test_audit (admin_id, action) VALUES (1, 'drop_database')"
        )


COLUMNS = "SELECT column_name FROM information_schema.columns WHERE table_name = $1"


async def test_the_audit_table_stores_only_what_the_service_writes(app_pool):
    rows = await app_pool.fetch(COLUMNS, "load_test_audit")
    assert {r["column_name"] for r in rows} == {
        "id", "admin_id", "action", "run_id", "detail", "created_at"
    }  # fmt: skip
    risky = ("password", "secret", "url", "dsn", "bot_token")
    for table in ("load_test_runs", "load_test_confirmations", "load_test_settings"):
        names = {r["column_name"] for r in await app_pool.fetch(COLUMNS, table)}
        assert not {n for n in names if any(w in n for w in risky)}, table
        assert not {n for n in names if "token" in n and n != "token_hash"}, table

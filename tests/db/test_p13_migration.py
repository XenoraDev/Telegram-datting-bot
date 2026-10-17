"""Migration 013 (plan-expiry reminders): additive only, safe on a live database."""

import hashlib
import shutil
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate
from tests.db.test_p12_migration import APPLIED as APPLIED_001_011

APPLIED = {
    **APPLIED_001_011,
    "012_support_tickets.sql": "6c3eb04dae4f2ee986977e059c8fa54e8b7afe43b17ae1755e0dd91246cd0869",
}


def live(tmp_path: Path, upto: int = 12) -> Path:
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


def test_applied_migrations_001_to_012_are_byte_identical():
    for name, digest in APPLIED.items():
        assert hashlib.sha256(Path(DEFAULT_MIGRATIONS_DIR, name).read_bytes()).hexdigest() == digest


async def test_013_adds_one_table_and_changes_nothing_else(pool, tmp_path):
    await migrate(pool, live(tmp_path))
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    await pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (9, 'ticket_reply')")
    before_cols, before_cons = await snapshot(pool)
    rows = {
        t: [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        for t in ("users", "admin_actions", "plans", "subscriptions", "support_tickets")
    }

    assert await migrate(pool) == ["013_expiry_reminders"]

    after_cols, after_cons = await snapshot(pool)
    old = {c["table_name"] for c in before_cols}
    assert [c for c in after_cols if c["table_name"] in old] == before_cols
    assert [c for c in after_cons if c["t"] in old] == before_cons  # no constraint changed
    assert {c["table_name"] for c in after_cols} - old == {"expiry_reminders"}
    for t, expected in rows.items():
        got = [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        assert got == expected, t
    assert await migrate(pool) == []


async def test_the_migration_file_is_purely_additive():
    sql = (Path(DEFAULT_MIGRATIONS_DIR) / "013_expiry_reminders.sql").read_text().upper()
    for forbidden in ("ALTER TABLE", "DROP ", "DELETE FROM", "TRUNCATE", "UPDATE "):
        assert forbidden not in sql, forbidden


async def test_one_row_per_user_and_end_date_and_it_goes_with_the_user(app_pool):
    await app_pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    ins = "INSERT INTO expiry_reminders (user_id, period_end) VALUES (1, now() + interval '2 days')"
    await app_pool.execute(ins.replace("now() + interval '2 days'", "'2030-01-01'"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_pool.execute(ins.replace("now() + interval '2 days'", "'2030-01-01'"))
    await app_pool.execute(ins)  # another end date is another reminder
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_pool.execute(
            "INSERT INTO expiry_reminders (user_id, period_end) VALUES (404, now())"
        )
    await app_pool.execute("DELETE FROM expiry_reminders WHERE user_id = 1")
    assert await app_pool.fetchval("SELECT count(*) FROM expiry_reminders") == 0

"""Migration 018 (structured audit fields): additive only, every audit row survives."""

import hashlib
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate
from tests.db.test_p14_migration import live, snapshot
from tests.db.test_p17_migration import APPLIED as APPLIED_001_016

APPLIED = {
    **APPLIED_001_016,
    "017_sub_audit_actions.sql": "3af63c061ca241d7c86a0b2b05764c289efc138fa12a4f95fa1f56763fe45726",
}


def test_applied_migrations_001_to_017_are_byte_identical():
    for name, digest in APPLIED.items():
        assert hashlib.sha256(Path(DEFAULT_MIGRATIONS_DIR, name).read_bytes()).hexdigest() == digest


async def test_018_only_adds_three_nullable_columns_and_keeps_every_audit_row(pool, tmp_path):
    await migrate(pool, live(tmp_path, 17))
    await pool.execute(
        "INSERT INTO admin_actions (admin_id, action, detail) VALUES (1, 'ban', 'x')"
    )
    await pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (1, 'sub_cancel')")
    before_cols, before_cons = await snapshot(pool)
    rows = [dict(r) for r in await pool.fetch("SELECT * FROM admin_actions ORDER BY id")]

    upto = live(tmp_path, 18, "live18")
    assert await migrate(pool, upto) == ["018_audit_sub_columns"]

    after_cols, after_cons = await snapshot(pool)
    old = {(c["table_name"], c["column_name"]) for c in before_cols}
    new = [c for c in after_cols if (c["table_name"], c["column_name"]) not in old]
    assert {(c["table_name"], c["column_name"]) for c in new} == {
        ("admin_actions", "subscription_id"),
        ("admin_actions", "payment_id"),
        ("admin_actions", "reason"),
    }
    assert all(c["is_nullable"] == "YES" for c in new)
    assert [c for c in after_cols if (c["table_name"], c["column_name"]) in old] == before_cols
    assert [c for c in after_cons if c["conname"] != "admin_actions_reason_check"] == before_cons
    got = [dict(r) for r in await pool.fetch("SELECT * FROM admin_actions ORDER BY id")]
    assert [{k: v for k, v in r.items() if k in o} for r, o in zip(got, rows, strict=True)] == rows
    assert all(r["subscription_id"] is None and r["payment_id"] is None for r in got)
    assert await migrate(pool, upto) == []


async def test_the_reason_is_bounded(app_pool):
    ins = "INSERT INTO admin_actions (admin_id, action, reason) VALUES (1, 'sub_cancel', $1)"
    await app_pool.execute(ins, "x" * 300)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(ins, "x" * 301)


def test_the_file_is_additive_only():
    sql = (Path(DEFAULT_MIGRATIONS_DIR) / "018_audit_sub_columns.sql").read_text().upper()
    for forbidden in ("DROP ", "DELETE FROM", "TRUNCATE", "UPDATE ", "INSERT "):
        assert forbidden not in sql, forbidden

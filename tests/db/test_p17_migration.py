"""Migration 017 (subscription-management audit actions): additive only."""

import hashlib
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate
from tests.db.test_p14_migration import live, snapshot
from tests.db.test_p15_migration import APPLIED as APPLIED_001_014

APPLIED = {
    **APPLIED_001_014,
    "015_auto_approve.sql": "ad44831799d50d43a52f5df17708f9e55c4c42aae1c2c382a71a7d8667045712",
    "016_chat_activity.sql": "a28252126e95f85bab0faf92c3f131aca5f9992316d25d7e0e23aeccca403a1d",
}


def test_applied_migrations_001_to_016_are_byte_identical():
    for name, digest in APPLIED.items():
        assert hashlib.sha256(Path(DEFAULT_MIGRATIONS_DIR, name).read_bytes()).hexdigest() == digest


async def test_017_only_widens_the_audit_check_and_keeps_every_audit_row(pool, tmp_path):
    await migrate(pool, live(tmp_path, 16))
    await pool.execute(
        "INSERT INTO admin_actions (admin_id, action, detail) VALUES (1, 'ban', 'x')"
    )
    await pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (1, 'payment_approve')")
    before_cols, before_cons = await snapshot(pool)
    rows = [dict(r) for r in await pool.fetch("SELECT * FROM admin_actions ORDER BY id")]

    upto = live(tmp_path, 17, "live17")
    assert await migrate(pool, upto) == ["017_sub_audit_actions"]

    after_cols, after_cons = await snapshot(pool)
    assert after_cols == before_cols  # no table or column added, changed or removed
    changed = [c for c in after_cons if c not in before_cons]
    assert [c["conname"] for c in changed] == ["admin_actions_action_check"]
    assert [dict(r) for r in await pool.fetch("SELECT * FROM admin_actions ORDER BY id")] == rows
    assert await migrate(pool, upto) == []


async def test_every_old_action_is_still_allowed_and_the_new_ones_are_too(app_pool):
    old = (
        "ban unban report_resolve report_dismiss report_ban payment_approve payment_reject "
        "payment_config plan_save ticket_reply ticket_close payment_auto_approve "
        "payment_auto_setting"
    ).split()
    for action in [*old, "sub_grant", "sub_cancel", "sub_extend", "sub_revoke"]:
        await app_pool.execute(
            "INSERT INTO admin_actions (admin_id, action) VALUES (1, $1)", action
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (1, 'nope')")


def test_the_file_is_additive_only():
    sql = (Path(DEFAULT_MIGRATIONS_DIR) / "017_sub_audit_actions.sql").read_text().upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "DELETE FROM", "TRUNCATE", "UPDATE ", "INSERT "):
        assert forbidden not in sql, forbidden

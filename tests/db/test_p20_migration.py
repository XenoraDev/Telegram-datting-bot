"""Migration 020 (subscription shortening audit): additive only, every audit row survives."""

import hashlib
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate
from tests.db.test_p14_migration import live, snapshot
from tests.db.test_p19_migration import APPLIED as APPLIED_001_018

APPLIED = {
    **APPLIED_001_018,
    "019_support_live_reply.sql": (
        "9d2343cc8f472df182c57ae4f36239143ec1bb9c364a40913a877ef6768ff790"
    ),
}


def test_applied_migrations_001_to_019_are_byte_identical():
    for name, digest in APPLIED.items():
        assert hashlib.sha256(Path(DEFAULT_MIGRATIONS_DIR, name).read_bytes()).hexdigest() == digest


async def test_020_adds_no_column_and_keeps_every_audit_row(pool, tmp_path):
    await migrate(pool, live(tmp_path, 19))
    await pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (1, 'sub_cancel')")
    await pool.execute(
        "INSERT INTO admin_actions (admin_id, action, payment_id) VALUES (1, 'sub_revoke', 5)"
    )
    before_cols, _ = await snapshot(pool)
    rows = [dict(r) for r in await pool.fetch("SELECT * FROM admin_actions ORDER BY id")]

    upto = live(tmp_path, 20, "live20")
    assert await migrate(pool, upto) == ["020_sub_shorten"]

    after_cols, _ = await snapshot(pool)
    assert after_cols == before_cols  # no column added, changed or removed
    got = [dict(r) for r in await pool.fetch("SELECT * FROM admin_actions ORDER BY id")]
    assert got == rows
    assert await migrate(pool, upto) == []


async def test_the_new_action_is_allowed_and_junk_still_is_not(app_pool):
    ins = "INSERT INTO admin_actions (admin_id, action) VALUES (1, $1)"
    await app_pool.execute(ins, "sub_shorten")
    for action in ("sub_cancel", "sub_grant", "sub_revoke", "sub_extend", "ban"):
        await app_pool.execute(ins, action)  # everything allowed before still is
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(ins, "sub_delete")


async def test_one_payment_can_be_taken_off_only_once(app_pool):
    ins = "INSERT INTO admin_actions (admin_id, action, payment_id) VALUES (1, $1, 7)"
    await app_pool.execute(ins, "sub_shorten")
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_pool.execute(ins, "sub_shorten")
    await app_pool.execute(ins, "sub_revoke")  # other actions on that payment are unaffected
    await app_pool.execute(
        "INSERT INTO admin_actions (admin_id, action) VALUES (1, 'sub_shorten')"
    )  # shortening by days has no payment: any number of them
    await app_pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (1, 'sub_shorten')")


def test_the_file_is_additive_only():
    sql = (Path(DEFAULT_MIGRATIONS_DIR) / "020_sub_shorten.sql").read_text().upper()
    for forbidden in ("DELETE FROM", "TRUNCATE", "UPDATE ", "INSERT ", "DROP TABLE", "DROP COLUMN"):
        assert forbidden not in sql, forbidden

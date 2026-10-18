"""Migration 015 (automatic payment approval): additive only, safe on a live database."""

import hashlib
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate
from tests.db.test_p14_migration import APPLIED as APPLIED_001_013
from tests.db.test_p14_migration import live, snapshot

APPLIED = {
    **APPLIED_001_013,
    "014_queue_stats.sql": "8b1bd2b34ff1078f1b619242005a46d849c1545a5780d9fe48df6871ad83db93",
}


def test_applied_migrations_001_to_014_are_byte_identical():
    for name, digest in APPLIED.items():
        assert hashlib.sha256(Path(DEFAULT_MIGRATIONS_DIR, name).read_bytes()).hexdigest() == digest


async def test_015_only_adds_columns_and_keeps_every_row(pool, tmp_path):
    await migrate(pool, live(tmp_path, 14))
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    before_cols, _ = await snapshot(pool)
    settings_before = dict(await pool.fetchrow("SELECT * FROM payment_settings"))

    upto_015 = live(tmp_path, 15, "live15")
    assert await migrate(pool, upto_015) == ["015_auto_approve"]

    after_cols, _ = await snapshot(pool)
    old = {(c["table_name"], c["column_name"]) for c in before_cols}
    new = {(c["table_name"], c["column_name"]) for c in after_cols} - old
    assert new == {
        ("payment_settings", "auto_approve"),
        ("payment_settings", "auto_approve_changed_by"),
        ("payment_settings", "auto_approve_changed_at"),
        ("payments", "approval_mode"),
    }
    settings = dict(await pool.fetchrow("SELECT * FROM payment_settings"))
    assert {k: v for k, v in settings.items() if k in settings_before} == settings_before
    assert settings["auto_approve"] is False  # OFF by default
    assert await migrate(pool, upto_015) == []


async def test_existing_payments_become_manual(pool, tmp_path):
    await migrate(pool, live(tmp_path, 14))
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    plan = await pool.fetchval(
        "INSERT INTO plans (code, name, duration_days, price_minor) "
        "VALUES ('old', 'Old', 30, 9900) RETURNING id"
    )
    await pool.execute(
        "INSERT INTO payments (reference, user_id, plan_id, amount_minor, currency, "
        "duration_days, pay_to) VALUES ('PAY-ABCDEFGHIJ', 1, $1, 9900, 'INR', 30, 'a@b')",
        plan,
    )
    await migrate(pool, live(tmp_path, 15, "live15"))
    assert await pool.fetchval("SELECT approval_mode FROM payments") == "manual"


async def test_the_new_audit_actions_are_allowed_and_old_ones_still_are(app_pool):
    ins = "INSERT INTO admin_actions (admin_id, action) VALUES (1, $1)"
    for action in ("payment_auto_approve", "payment_auto_setting", "payment_approve", "ban"):
        await app_pool.execute(ins, action)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(ins, "made_up_action")


async def test_approval_mode_only_takes_manual_or_auto(app_pool):
    await app_pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    plan = await app_pool.fetchval(
        "INSERT INTO plans (code, name, duration_days, price_minor) "
        "VALUES ('modeplan', 'Mode', 30, 9900) RETURNING id"
    )
    ins = (
        "INSERT INTO payments (reference, user_id, plan_id, amount_minor, currency, "
        "duration_days, pay_to, approval_mode) "
        "VALUES ('PAY-ABCDEFGHIJ', 1, $1, 9900, 'INR', 30, 'a@b', $2)"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(ins, plan, "robot")
    await app_pool.execute(ins, plan, "auto")

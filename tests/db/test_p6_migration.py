"""Migration 007 (manual payments) on a database that already holds P0-P5 data."""

import shutil
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate


def p5_migrations(tmp_path: Path) -> Path:
    """Only the migrations deployed before P6 (001-006)."""
    old = tmp_path / "p5"
    old.mkdir()
    for f in sorted(Path(DEFAULT_MIGRATIONS_DIR).glob("00[1-6]_*.sql")):
        shutil.copy(f, old / f.name)
    return old


async def test_007_upgrades_a_p5_database_and_keeps_every_row(pool, tmp_path):
    await migrate(pool, p5_migrations(tmp_path))
    for uid in (1, 2):
        await pool.execute(
            "INSERT INTO users (id, username, status, age_confirmed_at, language) "
            "VALUES ($1, $2, 'active', now(), 'hi')",
            uid,
            f"u{uid}",
        )
    await pool.execute("INSERT INTO plans (code, name) VALUES ('legacy', 'Legacy')")
    await pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id) SELECT 1, id FROM plans WHERE code='legacy'"
    )
    await pool.execute(
        "INSERT INTO admin_actions (admin_id, action, target_user_id) VALUES (9, 'ban', 2)"
    )
    tables = ("users", "plans", "plan_entitlements", "subscriptions", "admin_actions")
    before = {}
    for t in tables:
        before[t] = [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608

    applied = await migrate(pool)
    assert applied == ["007_manual_payments"]

    for t in tables:
        after = [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        # existing columns keep their values; plans gain the three new (empty) columns
        stripped = [
            {k: v for k, v in row.items() if k not in ("duration_days", "price_minor", "currency")}
            for row in after
        ]
        assert stripped == before[t], t
    assert await pool.fetchval("SELECT count(*) FROM plans WHERE price_minor IS NOT NULL") == 0
    assert await pool.fetchval("SELECT count(*) FROM payments") == 0
    assert await migrate(pool) == []  # idempotent


async def test_007_creates_an_inactive_empty_configuration_and_nothing_for_sale(app_pool):
    s = await app_pool.fetchrow("SELECT * FROM payment_settings")
    assert (s["is_active"], s["upi_id"], s["payee_name"], s["instructions"]) == (
        False,
        None,
        None,
        None,
    )
    assert await app_pool.fetchval("SELECT count(*) FROM payment_settings") == 1
    assert await app_pool.fetchval("SELECT price_minor FROM plans WHERE code='free'") is None


async def test_007_plan_price_and_duration_come_together(app_pool):
    ok = "INSERT INTO plans (code, name, duration_days, price_minor) VALUES ($1, 'n', $2, $3)"
    await app_pool.execute(ok, "a", 30, 9900)
    for days, price in ((30, None), (None, 9900), (0, 100), (30, 0), (30, -1)):
        with pytest.raises(asyncpg.CheckViolationError):
            await app_pool.execute(ok, "b", days, price)


async def test_007_audit_log_accepts_the_new_actions_stays_closed_and_append_only(app_pool):
    for action in ("payment_approve", "payment_reject", "payment_config", "plan_save"):
        await app_pool.execute(
            "INSERT INTO admin_actions (admin_id, action) VALUES (1, $1)", action
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(
            "INSERT INTO admin_actions (admin_id, action) VALUES (1, 'delete_everything')"
        )
    with pytest.raises(asyncpg.RaiseError):
        await app_pool.execute("DELETE FROM admin_actions")
    with pytest.raises(asyncpg.RaiseError):
        await app_pool.execute("UPDATE admin_actions SET detail = 'x'")


async def test_007_one_payment_backs_at_most_one_subscription_row(app_pool):
    await app_pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    plan = await app_pool.fetchval("SELECT id FROM plans WHERE code='free'")
    q = (
        "INSERT INTO subscriptions (user_id, plan_id, provider, provider_ref) "
        "VALUES (1, $1, 'manual_upi', 'PAY-AAAAAAAAAA')"
    )
    await app_pool.execute(q, plan)
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_pool.execute(q, plan)
    # rows without a reference (admin / promo subscriptions) are unaffected
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id) VALUES (1, $1), (1, $1)", plan
    )


async def test_007_deleting_a_payment_removes_its_evidence(app_pool):
    await app_pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    plan = await app_pool.fetchval(
        "INSERT INTO plans (code, name, duration_days, price_minor) "
        "VALUES ('p', 'P', 30, 100) RETURNING id"
    )
    pid = await app_pool.fetchval(
        "INSERT INTO payments (reference, user_id, plan_id, amount_minor, currency, "
        "duration_days, pay_to) VALUES ('PAY-AAAAAAAAAA', 1, $1, 100, 'INR', 30, 'a@b') "
        "RETURNING id",
        plan,
    )
    await app_pool.execute(
        "INSERT INTO payment_evidence (payment_id, content_type, size_bytes, sha256, data) "
        "VALUES ($1, 'image/png', 3, $2, $3)",
        pid,
        "0" * 64,
        b"abc",
    )
    with pytest.raises(asyncpg.CheckViolationError):  # declared size must match the bytes
        await app_pool.execute("UPDATE payment_evidence SET size_bytes = 99")
    await app_pool.execute("DELETE FROM payments WHERE id=$1", pid)
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0

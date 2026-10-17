"""Migration 010 (tier_settings) on a database that already holds data through migration 009."""

import shutil
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate
from bot.services.evidence import OK


def deployed(tmp_path: Path) -> Path:
    """The migrations that are live on the server today (001-009)."""
    old = tmp_path / "live"
    old.mkdir()
    for f in sorted(Path(DEFAULT_MIGRATIONS_DIR).glob("*.sql")):
        if int(f.name[:3]) <= 9:
            shutil.copy(f, old / f.name)
    return old


async def test_010_upgrades_a_live_009_database_without_touching_anything(pool, tmp_path):
    await migrate(pool, deployed(tmp_path))
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    await pool.execute(
        "INSERT INTO profiles (user_id, name, age, gender, looking_for, city, bio) "
        "VALUES (1, 'A', 30, 'f', 'any', 'Pune', 'b')"
    )
    plan = await pool.fetchval(
        "INSERT INTO plans (code, name, duration_days, price_minor, tier) "
        "VALUES ('pro', 'Pro', 30, 49900, 'pro') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO plan_entitlements "
        "VALUES ($1, 'swipe', '{\"enabled\": true, \"daily_limit\": 77}')",
        plan,
    )
    await pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "VALUES (1, $1, now() + interval '5 days')",
        plan,
    )
    await pool.execute("INSERT INTO browse_filters (user_id, gender) VALUES (1, 'f')")
    await pool.execute(
        "INSERT INTO profile_boosts VALUES (1, now(), now() + interval '1 hour', "
        "now() + interval '12 hours')"
    )
    snap = {
        t: [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1, 2")]  # noqa: S608
        for t in (
            "users",
            "profiles",
            "plans",
            "plan_entitlements",
            "subscriptions",
            "browse_filters",
            "profile_boosts",
            "admin_actions",
        )
    }

    assert await migrate(pool) == [
        "010_tier_settings",
        "011_load_testing",
        "012_support_tickets",
    ]

    for t, before in snap.items():
        after = [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1, 2")]  # noqa: S608
        assert after == before, t
    assert await pool.fetchval("SELECT count(*) FROM tier_settings") == 0
    assert await migrate(pool) == []  # idempotent re-run


async def test_tier_settings_rejects_junk(app_pool):
    ok = "INSERT INTO tier_settings (tier, feature_key, value) VALUES ($1, $2, '{}')"
    await app_pool.execute(ok, "pro", "swipe")
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_pool.execute(ok, "pro", "swipe")
    for tier, key in (("free", "swipe"), ("gold", "swipe"), ("pro", "Bad Key"), ("pro", "")):
        with pytest.raises(asyncpg.CheckViolationError):
            await app_pool.execute(ok, tier, key)


async def test_a_fresh_database_gets_every_migration_in_order(pool):
    applied = await migrate(pool)
    assert applied[0] == "001_init" and applied[-1] == "012_support_tickets"
    assert len(applied) == 12
    assert OK == "VALIDATION_OK"  # the P8 verdict names are still the contract

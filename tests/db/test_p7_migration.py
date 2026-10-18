"""Migration 008 (tiers, partner privacy, queue priority) on a database that holds P0-P6 data."""

import json
import shutil
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate
from bot.tiers import FREE, TIERS


def p6_migrations(tmp_path: Path) -> Path:
    """Only the migrations deployed before this change (001-007)."""
    old = tmp_path / "p6"
    old.mkdir()
    for f in sorted(Path(DEFAULT_MIGRATIONS_DIR).glob("00[1-7]_*.sql")):
        shutil.copy(f, old / f.name)
    return old


async def features(pool, code):
    rows = await pool.fetch(
        "SELECT pe.feature_key, pe.value FROM plan_entitlements pe "
        "JOIN plans p ON p.id = pe.plan_id WHERE p.code = $1",
        code,
    )
    return {r["feature_key"]: json.loads(r["value"]) for r in rows}


async def test_008_upgrades_a_p6_database_without_losing_or_overwriting_anything(pool, tmp_path):
    await migrate(pool, p6_migrations(tmp_path))
    for uid in (1, 2):
        await pool.execute(
            "INSERT INTO users (id, username, status, age_confirmed_at) "
            "VALUES ($1, $2, 'active', now())",
            uid,
            f"u{uid}",
        )
        await pool.execute(
            "INSERT INTO profiles (user_id, name, age, gender, looking_for, city, bio, country) "
            "VALUES ($1, $2, 30, 'f', 'any', 'Delhi', 'bio', 'IN')",
            uid,
            f"N{uid}",
        )
    await pool.execute("INSERT INTO chat_queue (user_id) VALUES (1)")
    # an operator already tuned one free limit; a P6-style paid plan; a customised paid plan
    await pool.execute(
        'UPDATE plan_entitlements SET value = \'{"enabled": true, "daily_limit": 77}\' '
        "WHERE feature_key = 'random_chat'"
    )
    for code, price in (("legacy", 9900), ("custom", 4900)):
        pid = await pool.fetchval(
            "INSERT INTO plans (code, name, duration_days, price_minor) VALUES ($1, $1, 30, $2) "
            "RETURNING id",
            code,
            price,
        )
        for key in ("random_chat", "browse", "swipe"):
            await pool.execute(
                "INSERT INTO plan_entitlements VALUES ($1, $2, '{\"enabled\": true}')", pid, key
            )
    await pool.execute(
        'UPDATE plan_entitlements SET value = \'{"enabled": true, "daily_limit": 5}\' '
        "WHERE feature_key = 'browse' AND plan_id = (SELECT id FROM plans WHERE code='custom')"
    )
    await pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "SELECT 1, id, now() + interval '9 days' FROM plans WHERE code='legacy'"
    )
    await pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (9, 'ban')")
    snap = {
        t: [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        for t in ("users", "subscriptions", "admin_actions", "payments", "payment_settings")
    }

    assert await migrate(pool) == [
        "008_tiers_privacy",
        "009_filters_boost_evidence",
        "010_tier_settings",
        "011_load_testing",
        "012_support_tickets",
        "013_expiry_reminders",
        "014_queue_stats",
        "015_auto_approve",
        "016_chat_activity",
        "017_sub_audit_actions",
        "018_audit_sub_columns",
    ]

    for t, before in snap.items():
        got = [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        # later migrations may add columns (with defaults); every value that existed stays
        assert [
            {k: v for k, v in r.items() if k in b} for r, b in zip(got, before, strict=False)
        ] == before, t
        assert len(got) == len(before), t
    # profiles / queue gain columns, every existing value stays
    p = await pool.fetchrow("SELECT * FROM profiles WHERE user_id = 1")
    assert (p["name"], p["age"], p["city"]) == ("N1", 30, "Delhi")
    assert (p["show_age"], p["show_gender"], p["show_city"]) == (True, True, True)
    assert await pool.fetchval("SELECT priority FROM chat_queue WHERE user_id = 1") == 0
    # free plan: limits added only where the value was still the original default
    free = await features(pool, "free")
    assert free["random_chat"] == {"enabled": True, "daily_limit": 77}  # operator's edit survives
    assert free["browse"] == FREE["browse"] and free["swipe"] == FREE["swipe"]
    assert free["view_partner_city"] == FREE["view_partner_city"]
    assert free["match_priority"] == FREE["match_priority"]
    # the P6-style paid plan becomes Premium; the customised one is left exactly as it was
    assert await features(pool, "legacy") == TIERS["premium"]
    assert await pool.fetchval("SELECT tier FROM plans WHERE code='legacy'") == "premium"
    custom = await features(pool, "custom")
    assert custom["browse"] == {"enabled": True, "daily_limit": 5} and len(custom) == 3
    assert await pool.fetchval("SELECT tier FROM plans WHERE code='custom'") is None
    # the live subscription row is untouched and still points at the (now tiered) plan
    assert await pool.fetchval("SELECT count(*) FROM subscriptions") == 1
    assert await migrate(pool) == []


async def test_008_on_a_fresh_database_seeds_the_documented_free_plan(app_pool):
    assert await features(app_pool, "free") == FREE
    assert await app_pool.fetchval("SELECT count(*) FROM plans WHERE price_minor IS NOT NULL") == 0


async def test_008_constraints(app_pool):
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("UPDATE plans SET tier = 'gold' WHERE code = 'free'")
    await app_pool.execute("INSERT INTO users (id, status) VALUES (1, 'onboarding')")
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("INSERT INTO chat_queue (user_id, priority) VALUES (1, 101)")

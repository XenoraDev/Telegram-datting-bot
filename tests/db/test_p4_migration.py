"""Migration 006 (language + entitlements) on a P0-P3 database."""

import shutil
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate


def p3_migrations(tmp_path: Path) -> Path:
    """Only the migrations deployed before P4 (001-005)."""
    old = tmp_path / "p3"
    old.mkdir()
    for f in sorted(Path(DEFAULT_MIGRATIONS_DIR).glob("00[12345]_*.sql")):
        shutil.copy(f, old / f.name)
    return old


async def test_006_upgrades_a_p3_database_and_keeps_every_row(pool, tmp_path):
    await migrate(pool, p3_migrations(tmp_path))
    for uid in (1, 2, 3):
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
    await pool.execute("INSERT INTO swipes (swiper_id, target_id, action) VALUES (1, 2, 'like')")
    await pool.execute("INSERT INTO swipes (swiper_id, target_id, action) VALUES (2, 1, 'like')")
    await pool.execute("INSERT INTO matches (user_a_id, user_b_id) VALUES (1, 2)")
    await pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (3, 1)")
    await pool.execute(
        "INSERT INTO reports (reporter_id, reported_id, source, reason) "
        "VALUES (1, 3, 'profile', 'spam')"
    )
    await pool.execute(
        "INSERT INTO admin_actions (admin_id, action, target_user_id) VALUES (9, 'ban', 3)"
    )
    tables = ("users", "profiles", "swipes", "matches", "blocks", "reports", "admin_actions")
    before = {t: [dict(r) for r in await pool.fetch(f"SELECT * FROM {t}")] for t in tables}  # noqa: S608

    applied = await migrate(pool)
    assert applied[0] == "006_language_entitlements"

    for t in tables:
        after = [dict(r) for r in await pool.fetch(f"SELECT * FROM {t}")]  # noqa: S608
        assert after == before[t], t
    assert set(await pool.fetchval("SELECT array_agg(language) FROM users")) == {"en"}
    for t in ("subscriptions", "entitlement_grants", "usage_counters"):
        assert await pool.fetchval(f"SELECT count(*) FROM {t}") == 0  # noqa: S608
    assert await migrate(pool) == []


async def test_006_seeds_only_the_free_plan_that_mirrors_today(app_pool):
    plans = await app_pool.fetch("SELECT code, billing_interval, is_active FROM plans")
    assert [tuple(p) for p in plans] == [("free", "none", True)]
    feats = await app_pool.fetch(
        "SELECT feature_key, value::text AS v FROM plan_entitlements ORDER BY 1"
    )
    assert [(f["feature_key"], f["v"]) for f in feats] == [
        ("browse", '{"enabled": true}'),
        ("random_chat", '{"enabled": true}'),
        ("swipe", '{"enabled": true}'),
    ]


async def test_006_constraints(app_pool):
    await app_pool.execute("INSERT INTO users (id) VALUES (1)")
    bad = [
        "INSERT INTO plans (code, name) VALUES ('Bad Code', 'x')",
        "INSERT INTO plans (code, name, billing_interval) VALUES ('p', 'x', 'weekly')",
        "INSERT INTO plan_entitlements (plan_id, feature_key, value) VALUES (1, 'a', '[]')",
        "INSERT INTO plan_entitlements (plan_id, feature_key, value) VALUES (1, 'Bad Key', '{}')",
        "INSERT INTO subscriptions (user_id, plan_id, status) VALUES (1, 1, 'gold')",
        "INSERT INTO entitlement_grants (user_id, feature_key, value, source) "
        "VALUES (1, 'a', '{}', 'hacker')",
        "INSERT INTO usage_counters (user_id, feature_key, period_start, count) "
        "VALUES (1, 'a', current_date, -1)",
        "UPDATE users SET language = 'fr' WHERE id = 1",
    ]
    for stmt in bad:
        with pytest.raises(asyncpg.CheckViolationError):
            await app_pool.execute(stmt)
    await app_pool.execute(
        "INSERT INTO usage_counters (user_id, feature_key, period_start, count) "
        "VALUES (1, 'a', current_date, 1)"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_pool.execute(
            "INSERT INTO usage_counters (user_id, feature_key, period_start, count) "
            "VALUES (1, 'a', current_date, 1)"
        )

"""Migration 005 (admin audit) on a database that already holds P0-P2 data."""

import shutil
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate


def p2_migrations(tmp_path: Path) -> Path:
    """Only the migrations deployed before P3 (001-004)."""
    old = tmp_path / "p2"
    old.mkdir()
    for f in sorted(Path(DEFAULT_MIGRATIONS_DIR).glob("00[1234]_*.sql")):
        shutil.copy(f, old / f.name)
    return old


async def test_005_upgrades_a_p2_database_and_keeps_every_row(pool, tmp_path):
    await migrate(pool, p2_migrations(tmp_path))
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
    await pool.execute("INSERT INTO swipes (swiper_id, target_id, action) VALUES (1, 2, 'like')")
    await pool.execute("INSERT INTO swipes (swiper_id, target_id, action) VALUES (2, 1, 'like')")
    await pool.execute("INSERT INTO matches (user_a_id, user_b_id) VALUES (1, 2)")
    await pool.execute(
        "INSERT INTO reports (reporter_id, reported_id, source, reason) "
        "VALUES (1, 2, 'profile', 'spam')"
    )
    tables = ("users", "profiles", "swipes", "matches", "reports", "blocks")
    before = {t: [dict(r) for r in await pool.fetch(f"SELECT * FROM {t}")] for t in tables}  # noqa: S608

    applied = await migrate(pool)
    assert applied[0].startswith("005")  # later migrations (006+) may follow

    for t in tables:
        after = [dict(r) for r in await pool.fetch(f"SELECT * FROM {t}")]  # noqa: S608
        if before[t]:  # later migrations only ADD columns: compare the ones that existed then
            after = [{k: v for k, v in row.items() if k in before[t][0]} for row in after]
        assert after == before[t], t
    assert await pool.fetchval("SELECT count(*) FROM admin_actions") == 0
    assert await migrate(pool) == []


async def test_005_constrains_audit_rows(app_pool):
    await app_pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (1, 'ban')")
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (1, 'drop_db')")
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(
            "INSERT INTO admin_actions (admin_id, action, detail) VALUES (1, 'ban', $1)", "x" * 201
        )

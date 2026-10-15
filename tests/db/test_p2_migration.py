"""Migration 004 on a database that already holds P1 data (additive, nothing is lost)."""

import shutil
from pathlib import Path

import asyncpg

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate


def p1_migrations(tmp_path: Path) -> Path:
    """A copy of the migrations directory with only the ones deployed before P2 (001-003)."""
    old = tmp_path / "p1"
    old.mkdir()
    for f in sorted(Path(DEFAULT_MIGRATIONS_DIR).glob("00[123]_*.sql")):
        shutil.copy(f, old / f.name)
    return old


async def test_004_upgrades_an_existing_p1_database_without_touching_its_data(pool, tmp_path):
    # 1. The database as it is deployed today: migrations 001-003 only.
    await migrate(pool, p1_migrations(tmp_path))

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
    sid = await pool.fetchval(
        "INSERT INTO chat_sessions (user_a, user_b, status, ended_at, ended_by) "
        "VALUES (1, 2, 'ended', now(), 1) RETURNING id"
    )
    await pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (1, 3)")
    await pool.execute(
        "INSERT INTO reports (reporter_id, reported_id, session_id, source, reason, context) "
        "VALUES (1, 2, $1, 'chat', 'spam', '[{\"text\": \"x\"}]')",
        sid,
    )
    await pool.execute("INSERT INTO restrictions (user_id, kind, until) VALUES (3, 'mute', now())")
    before = {
        t: await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")  # noqa: S608 - test constants
        for t in ("users", "profiles", "chat_sessions", "blocks", "reports", "restrictions")
    }

    # 2. Upgrade with the real migrations directory.
    applied = await migrate(pool)
    assert len(applied) == 1 and "004" in applied[0]

    # 3. Every existing row is byte-for-byte what it was.
    for table, rows in before.items():
        after = await pool.fetch(f"SELECT * FROM {table} ORDER BY 1")  # noqa: S608
        old_cols = list(rows[0].keys())
        assert [{k: r[k] for k in old_cols} for r in after] == [dict(r) for r in rows], table

    # 4. Existing profiles got distinct opaque handles; new tables are empty and usable.
    handles = [r["public_id"] for r in await pool.fetch("SELECT public_id FROM profiles")]
    assert len(handles) == 3 and len(set(handles)) == 3 and None not in handles
    assert await pool.fetchval("SELECT count(*) FROM swipes") == 0
    assert await pool.fetchval("SELECT count(*) FROM matches") == 0
    await pool.execute("INSERT INTO swipes (swiper_id, target_id, action) VALUES (1, 2, 'like')")
    assert await migrate(pool) == []  # re-running is a no-op


async def test_004_creates_the_expected_constraints(app_pool: asyncpg.Pool):
    rows = await app_pool.fetch(
        "SELECT conrelid::regclass::text AS tbl, contype::text AS contype FROM pg_constraint "
        "WHERE conrelid IN ('swipes'::regclass, 'matches'::regclass)"
    )
    kinds = {(r["tbl"], r["contype"]) for r in rows}
    assert {
        ("swipes", "u"),
        ("swipes", "c"),
        ("swipes", "f"),
        ("matches", "u"),
        ("matches", "c"),
    } <= kinds
    idx = {r["indexname"] for r in await app_pool.fetch("SELECT indexname FROM pg_indexes")}
    assert {"profiles_public_id", "one_open_profile_report", "swipes_target_like"} <= idx

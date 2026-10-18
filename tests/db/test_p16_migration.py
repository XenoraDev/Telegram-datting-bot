"""Migration 016 (chat activity time): additive only, safe on a live database."""

from bot.db.migrate import migrate
from tests.db.test_p14_migration import live, snapshot


async def test_016_adds_one_column_and_one_index_and_keeps_every_chat(pool, tmp_path):
    await migrate(pool, live(tmp_path, 15))
    for uid in (1, 2):
        await pool.execute(
            "INSERT INTO users (id, status, age_confirmed_at) VALUES ($1, 'active', now())", uid
        )
    sid = await pool.fetchval(
        "INSERT INTO chat_sessions (user_a, user_b) VALUES (1, 2) RETURNING id"
    )
    before_cols, _ = await snapshot(pool)

    upto = live(tmp_path, 16, "live16")
    assert await migrate(pool, upto) == ["016_chat_activity"]

    after_cols, _ = await snapshot(pool)
    old = {(c["table_name"], c["column_name"]) for c in before_cols}
    assert {(c["table_name"], c["column_name"]) for c in after_cols} - old == {
        ("chat_sessions", "last_activity_at")
    }
    row = await pool.fetchrow("SELECT status, last_activity_at, started_at FROM chat_sessions")
    assert row["status"] == "active" and row["last_activity_at"] is not None
    assert await pool.fetchval("SELECT count(*) FROM chat_sessions WHERE id = $1", sid) == 1
    assert await migrate(pool, upto) == []

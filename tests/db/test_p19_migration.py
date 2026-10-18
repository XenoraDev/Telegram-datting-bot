"""Migration 019 (live support reply): additive only, every ticket and message survives."""

import hashlib
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate
from tests.db.test_p14_migration import live, snapshot
from tests.db.test_p18_migration import APPLIED as APPLIED_001_017

APPLIED = {
    **APPLIED_001_017,
    "018_audit_sub_columns.sql": "9b83cf7ae060959f7373875c252ab68dcd75127eae6da13350cf56199a8e9d42",
}


def test_applied_migrations_001_to_018_are_byte_identical():
    for name, digest in APPLIED.items():
        assert hashlib.sha256(Path(DEFAULT_MIGRATIONS_DIR, name).read_bytes()).hexdigest() == digest


async def test_019_adds_one_nullable_column_and_keeps_every_ticket(pool, tmp_path):
    await migrate(pool, live(tmp_path, 18))
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    tid = await pool.fetchval(
        "INSERT INTO support_tickets (user_id, category) VALUES (1, 'other') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO support_messages (ticket_id, sender, ticket_text) "
        "VALUES ($1, 'user', 'hello')",
        tid,
    )
    before_cols, _ = await snapshot(pool)
    rows = [dict(r) for r in await pool.fetch("SELECT * FROM support_tickets ORDER BY id")]

    upto = live(tmp_path, 19, "live19")
    assert await migrate(pool, upto) == ["019_support_live_reply"]

    after_cols, _ = await snapshot(pool)
    old = {(c["table_name"], c["column_name"]) for c in before_cols}
    new = [c for c in after_cols if (c["table_name"], c["column_name"]) not in old]
    assert [(c["table_name"], c["column_name"], c["is_nullable"]) for c in new] == [
        ("support_tickets", "live_until", "YES")
    ]
    got = [dict(r) for r in await pool.fetch("SELECT * FROM support_tickets ORDER BY id")]
    assert [{k: v for k, v in r.items() if k in o} for r, o in zip(got, rows, strict=True)] == rows
    assert got[0]["live_until"] is None  # nobody is in live mode after the upgrade
    assert await pool.fetchval("SELECT count(*) FROM support_messages") == 1
    assert await migrate(pool, upto) == []


async def test_a_closed_ticket_can_never_be_live(app_pool):
    await app_pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    tid = await app_pool.fetchval(
        "INSERT INTO support_tickets (user_id, category, status, closed_by, closed_at) "
        "VALUES (1, 'other', 'closed', 'user', now()) RETURNING id"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(
            "UPDATE support_tickets SET live_until = now() + interval '5 min' WHERE id = $1", tid
        )


def test_the_file_is_additive_only():
    sql = (Path(DEFAULT_MIGRATIONS_DIR) / "019_support_live_reply.sql").read_text().upper()
    for forbidden in ("DROP ", "DELETE FROM", "TRUNCATE", "UPDATE ", "INSERT "):
        assert forbidden not in sql, forbidden

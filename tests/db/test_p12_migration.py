"""Migration 012 (support tickets): additive, safe on a live database, enforced by the schema."""

import asyncio
import shutil
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate

NEW_TABLES = {"support_tickets", "support_messages"}


def live(tmp_path: Path, upto: int = 11, name: str = "live") -> Path:
    old = tmp_path / name
    old.mkdir()
    for f in sorted(Path(DEFAULT_MIGRATIONS_DIR).glob("*.sql")):
        if int(f.name[:3]) <= upto:
            shutil.copy(f, old / f.name)
    return old


async def snapshot(pool):
    cols = await pool.fetch(
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "ORDER BY table_name, ordinal_position"
    )
    cons = await pool.fetch(
        "SELECT conrelid::regclass::text AS t, conname, pg_get_constraintdef(oid) AS d "
        "FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
    )
    return [dict(r) for r in cols], [dict(r) for r in cons]


async def test_012_only_adds_tables_and_widens_one_check_and_keeps_every_row(pool, tmp_path):
    await migrate(pool, live(tmp_path))
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    for action in ("ban", "payment_approve", "plan_save"):
        await pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (9, $1)", action)
    before_cols, before_cons = await snapshot(pool)
    rows = {
        t: [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        for t in ("users", "admin_actions", "plans", "payments")
    }

    upto12 = live(tmp_path, 12, "upto12")  # 013 is a later change, checked in its own test
    assert await migrate(pool, upto12) == ["012_support_tickets"]

    after_cols, after_cons = await snapshot(pool)
    old = {c["table_name"] for c in before_cols}
    assert [c for c in after_cols if c["table_name"] in old] == before_cols  # no column changed
    assert {c["table_name"] for c in after_cols} - old == NEW_TABLES
    # the only existing constraint that changed is the admin_actions action list ...
    changed = [c for c in after_cons if c["t"] in old and c not in before_cons]
    assert [c["conname"] for c in changed] == ["admin_actions_action_check"]
    # ... and it still allows everything it allowed before, plus the two new actions
    for action in (
        "ban", "unban", "report_resolve", "report_dismiss", "report_ban", "payment_approve",
        "payment_reject", "payment_config", "plan_save", "ticket_reply", "ticket_close",
    ):  # fmt: skip
        await pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (9, $1)", action)
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (9, 'nuke')")
    for t, expected in rows.items():
        got = [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
        if t == "admin_actions":
            got = got[: len(expected)]
        assert got == expected, t
    assert await migrate(pool, upto12) == []  # idempotent


async def test_the_audit_log_is_still_append_only(app_pool):
    await app_pool.execute(
        "INSERT INTO admin_actions (admin_id, action) VALUES (9, 'ticket_reply')"
    )
    for sql in ("UPDATE admin_actions SET detail = 'x'", "DELETE FROM admin_actions"):
        with pytest.raises(asyncpg.RaiseError):
            await app_pool.execute(sql)


async def test_a_fresh_database_gets_the_tables(app_pool):
    versions = [
        r["version"]
        for r in await app_pool.fetch("SELECT version FROM schema_migrations ORDER BY 1")
    ]
    assert "012_support_tickets" in versions and len(versions) == 20


async def test_the_migration_file_is_additive():
    sql = (Path(DEFAULT_MIGRATIONS_DIR) / "012_support_tickets.sql").read_text().upper()
    for forbidden in ("DROP TABLE", "TRUNCATE", "DELETE FROM", "UPDATE ", "DROP COLUMN"):
        assert forbidden not in sql, forbidden
    assert sql.count("ALTER TABLE") == 2  # drop + re-add of the one audit CHECK, nothing else
    assert "ALTER TABLE ADMIN_ACTIONS" in sql.replace("\n", " ")


# sha256 of migrations 001-011, which are applied in production: they must never change
APPLIED = {
    "001_init.sql": "9cfaec94e680da060f821381b4be7437a9ed3b939134c54a958a7b600cd9603f",
    "002_profiles_chat_safety.sql": "a22f62fd446bb804a68c4724a4db56f38a3e7affadc7b8768b438e95aec6b22f",  # noqa: E501
    "003_profile_country.sql": "1af3a600785c1993cc47df4c2ce6998b650243db883bc42cb12c8f48c0582d80",
    "004_browse_matches.sql": "04c076e54e71f63087ad3020912729fd70b8570d26d228ebc94b688b2c70f426",
    "005_admin_audit.sql": "f474ac4292c1a69b43381c81ce0238beb7ee7ad7272338fb4f7a37383f281748",
    "006_language_entitlements.sql": "3e051568f8460ac655f9e33b4647024f3e377955e0e2e086906f447df5375f32",  # noqa: E501
    "007_manual_payments.sql": "beb6441b78a90c5bb7e606da36bc6382115272749adb429ee2bffefc7e60d121",
    "008_tiers_privacy.sql": "4d53037d05d51649fdda581a528feea65adc023d2ad1cbe4a4fc4482a4b40ec1",
    "009_filters_boost_evidence.sql": "cf957bea4654f56a5dd0884d3ebc32ab8a711c40d3e5f297b0cc2e973d0aa332",  # noqa: E501
    "010_tier_settings.sql": "415966de7c4e21c2815584344585a60788683384845156288c44bfd6fa41f6b8",
    "011_load_testing.sql": "d60d71cb98a7d6b46102f3f79e61af114c923a249f25ccaf8f489f442da46c5c",
}


def test_applied_migrations_001_to_011_are_byte_identical():
    import hashlib

    for name, digest in APPLIED.items():
        assert hashlib.sha256(Path(DEFAULT_MIGRATIONS_DIR, name).read_bytes()).hexdigest() == digest


async def _user(pool, uid):
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES ($1, 'active', now())", uid
    )


async def test_constraints_reject_junk(app_pool):
    await _user(app_pool, 1)
    ins = "INSERT INTO support_tickets (user_id, category) VALUES (1, $1) RETURNING id"
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(ins, "refunds")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_pool.execute(
            "INSERT INTO support_tickets (user_id, category) VALUES (404, 'other')"
        )
    with pytest.raises(asyncpg.CheckViolationError):  # closed needs a closer and a time
        await app_pool.execute(
            "INSERT INTO support_tickets (user_id, category, status) VALUES (1, 'other', 'closed')"
        )
    tid = await app_pool.fetchval(ins, "other")
    msg = (
        "INSERT INTO support_messages (ticket_id, sender, admin_id, ticket_text) "
        "VALUES ($1, $2, $3, $4)"
    )
    await app_pool.execute(msg, tid, "user", None, "hello")
    await app_pool.execute(msg, tid, "admin", 9, "hi")
    for args in (
        (tid, "user", 9, "an admin id on a user message"),
        (tid, "admin", None, "an admin message without an admin"),
        (tid, "bot", None, "bad sender"),
        (tid, "user", None, ""),
        (tid, "user", None, "x" * 2001),
        (tid + 99, "user", None, "no such ticket"),
    ):
        with pytest.raises((asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError)):
            await app_pool.execute(msg, *args)


async def test_the_database_caps_unfinished_tickets_at_three_even_when_racing(app_pool):
    await _user(app_pool, 1)
    ins = "INSERT INTO support_tickets (user_id, category) VALUES (1, 'other')"
    results = await asyncio.gather(
        *[app_pool.execute(ins) for _ in range(8)], return_exceptions=True
    )
    assert sum(1 for r in results if not isinstance(r, Exception)) == 3
    assert all(
        isinstance(r, asyncpg.CheckViolationError) for r in results if isinstance(r, Exception)
    )
    await app_pool.execute(
        "UPDATE support_tickets SET status = 'closed', closed_by = 'user', closed_at = now() "
        "WHERE id = (SELECT min(id) FROM support_tickets)"
    )
    await app_pool.execute(ins)  # a closed ticket frees a slot
    await _user(app_pool, 2)
    await app_pool.execute("INSERT INTO support_tickets (user_id, category) VALUES (2, 'other')")


async def test_deleting_a_ticket_deletes_its_messages(app_pool):
    await _user(app_pool, 1)
    tid = await app_pool.fetchval(
        "INSERT INTO support_tickets (user_id, category) VALUES (1, 'other') RETURNING id"
    )
    await app_pool.execute(
        "INSERT INTO support_messages (ticket_id, sender, ticket_text) VALUES ($1, 'user', 'x')",
        tid,
    )
    await app_pool.execute("DELETE FROM support_tickets WHERE id = $1", tid)
    assert await app_pool.fetchval("SELECT count(*) FROM support_messages") == 0

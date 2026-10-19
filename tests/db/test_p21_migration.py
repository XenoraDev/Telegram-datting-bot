"""Migration 021 (traffic attribution): additive only, first touch is immutable."""

import hashlib
from pathlib import Path

import asyncpg
import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, migrate
from tests.db.test_p14_migration import live, snapshot
from tests.db.test_p20_migration import APPLIED as APPLIED_001_019

APPLIED = {
    **APPLIED_001_019,
    "020_sub_shorten.sql": "102d11bc74446ea8fe9426ff361f552ca5a59c222eb0e9a24f922f2b69a913f7",
}


def test_applied_migrations_001_to_020_are_byte_identical():
    for name, digest in APPLIED.items():
        assert hashlib.sha256(Path(DEFAULT_MIGRATIONS_DIR, name).read_bytes()).hexdigest() == digest


async def test_021_only_adds_two_tables_and_keeps_every_existing_row(pool, tmp_path):
    await migrate(pool, live(tmp_path, 20))
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    await pool.execute("INSERT INTO admin_actions (admin_id, action) VALUES (1, 'sub_shorten')")
    before_cols, _ = await snapshot(pool)
    users = [dict(r) for r in await pool.fetch("SELECT * FROM users ORDER BY id")]
    audit = [dict(r) for r in await pool.fetch("SELECT * FROM admin_actions ORDER BY id")]

    upto = live(tmp_path, 21, "live21")
    assert await migrate(pool, upto) == ["021_traffic_attribution"]

    after_cols, _ = await snapshot(pool)
    old = {(c["table_name"], c["column_name"]) for c in before_cols}
    new_tables = {
        c["table_name"] for c in after_cols if (c["table_name"], c["column_name"]) not in old
    }
    assert new_tables == {"campaigns", "user_acquisition"}
    assert [c for c in after_cols if (c["table_name"], c["column_name"]) in old] == before_cols
    assert [dict(r) for r in await pool.fetch("SELECT * FROM users ORDER BY id")] == users
    assert [dict(r) for r in await pool.fetch("SELECT * FROM admin_actions ORDER BY id")] == audit
    assert await pool.fetchval("SELECT count(*) FROM user_acquisition") == 0  # nobody is invented
    assert await migrate(pool, upto) == []


async def campaign(pool, code="abcdefgh", source="instagram", status="active"):
    return await pool.fetchval(
        "INSERT INTO campaigns (code, source, name, status, created_by) "
        "VALUES ($1, $2, 'Reel A', $3, 9) RETURNING id",
        code,
        source,
        status,
    )


async def user(pool, uid=1):
    await pool.execute("INSERT INTO users (id) VALUES ($1) ON CONFLICT DO NOTHING", uid)


async def test_campaign_codes_and_sources_are_validated_by_the_database(app_pool):
    await campaign(app_pool)
    for code in ("ABCDEFGH", "abcdefg", "abcdefghi", "abcd-fgh", "abcdef18", "'; drop--"):
        with pytest.raises(asyncpg.CheckViolationError):
            await campaign(app_pool, code=code)
    with pytest.raises(asyncpg.UniqueViolationError):
        await campaign(app_pool)
    for source in ("Instagram", "1abc", "a", "x" * 33, "in stagram", "a;b"):
        with pytest.raises(asyncpg.CheckViolationError):
            await campaign(app_pool, code="bcdefghi", source=source)
    with pytest.raises(asyncpg.CheckViolationError):
        await campaign(app_pool, code="cdefghij", status="deleted")


async def test_an_acquisition_row_has_a_consistent_shape(app_pool):
    cid = await campaign(app_pool)
    await user(app_pool, 1)
    ins = "INSERT INTO user_acquisition (user_id, kind, campaign_id, source) VALUES ($1,$2,$3,$4)"
    await app_pool.execute(ins, 1, "campaign", cid, "instagram")
    await user(app_pool, 2)
    await app_pool.execute(ins, 2, "direct", None, None)
    await user(app_pool, 3)
    await app_pool.execute(ins, 3, "invalid", cid, None)  # a paused campaign's link
    await user(app_pool, 4)
    await app_pool.execute(ins, 4, "invalid", None, None)  # malformed / unknown
    for uid, kind, camp, src in (
        (5, "campaign", None, "instagram"),  # a campaign needs its campaign ...
        (6, "campaign", cid, None),  # ... and its source
        (7, "direct", cid, None),  # direct names nothing
        (8, "invalid", cid, "instagram"),  # an invalid link is never attributed to a source
        (9, "bogus", None, None),
    ):
        await user(app_pool, uid)
        with pytest.raises(asyncpg.CheckViolationError):
            await app_pool.execute(ins, uid, kind, camp, src)


async def test_first_touch_can_not_be_changed_or_duplicated(app_pool):
    cid = await campaign(app_pool)
    other = await campaign(app_pool, code="bcdefghi", source="youtube")
    await user(app_pool, 1)
    await app_pool.execute(
        "INSERT INTO user_acquisition (user_id, kind, campaign_id, source) "
        "VALUES (1, 'campaign', $1, 'instagram')",
        cid,
    )
    for sql in (
        "UPDATE user_acquisition SET campaign_id = campaign_id + 1, source = 'youtube'",
        "UPDATE user_acquisition SET kind = 'direct', campaign_id = NULL, source = NULL",
        "UPDATE user_acquisition SET acquired_at = now() - interval '9 days'",
    ):
        with pytest.raises(asyncpg.RaiseError):
            await app_pool.execute(sql)
    with pytest.raises(asyncpg.UniqueViolationError):  # a second link can not add a second row
        await app_pool.execute(
            "INSERT INTO user_acquisition (user_id, kind, campaign_id, source) "
            "VALUES (1, 'campaign', $1, 'youtube')",
            other,
        )


async def test_the_row_goes_with_the_account_and_campaigns_survive(app_pool):
    cid = await campaign(app_pool)
    await user(app_pool, 1)
    await app_pool.execute(
        "INSERT INTO user_acquisition (user_id, kind, campaign_id, source) "
        "VALUES (1, 'campaign', $1, 'instagram')",
        cid,
    )
    await app_pool.execute("DELETE FROM user_acquisition WHERE user_id = 1")  # erasure is allowed
    assert await app_pool.fetchval("SELECT count(*) FROM campaigns") == 1


async def test_the_new_audit_actions_are_allowed_and_junk_still_is_not(app_pool):
    ins = "INSERT INTO admin_actions (admin_id, action) VALUES (1, $1)"
    for action in ("campaign_create", "campaign_status", "sub_shorten", "sub_grant", "ban"):
        await app_pool.execute(ins, action)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(ins, "campaign_delete")


def test_the_file_is_additive_only():
    sql = (Path(DEFAULT_MIGRATIONS_DIR) / "021_traffic_attribution.sql").read_text().upper()
    for forbidden in ("DELETE FROM", "TRUNCATE", "DROP TABLE", "DROP COLUMN"):
        assert forbidden not in sql, forbidden
    for statement in ("UPDATE ", "INSERT "):  # a data change, not the trigger's "BEFORE UPDATE"
        assert not any(line.startswith(statement) for line in sql.splitlines()), statement

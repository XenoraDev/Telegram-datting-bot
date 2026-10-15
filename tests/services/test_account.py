"""/deleteme and account lifecycle: what is removed, what must remain, what is blocked after."""

import pytest

from bot.db import repo_chat, repo_profiles, repo_safety, repo_users
from bot.services.chat_relay import Incoming
from tests.factories import ended_session, make_user, start_chat


async def test_delete_removes_personal_data_but_keeps_a_deleted_shell(app_pool, svc):
    await make_user(app_pool, 1)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = 1")
    res = await svc.account.delete_account(1)
    assert res.kind == "deleted"
    row = await repo_users.get(app_pool, 1)
    assert (row["status"], row["username"], row["language"], row["age_confirmed_at"]) == (
        "deleted",
        None,
        "en",
        None,
    )
    assert row["status_reason"] == "user_request"
    assert await repo_profiles.get(app_pool, 1) is None


async def test_delete_twice_and_unknown_user(app_pool, svc):
    await make_user(app_pool, 1)
    await svc.account.delete_account(1)
    assert (await svc.account.delete_account(1)).kind == "already_deleted"
    assert (await svc.account.delete_account(404)).kind == "unknown_user"


async def test_delete_leaves_the_queue_and_ends_an_active_chat_telling_the_partner(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await start_chat(svc, 1, 2)
    res = await svc.account.delete_account(1)
    assert res.partner_id == 2
    assert await repo_chat.get_partner(app_pool, 2) is None
    await make_user(app_pool, 3)
    await svc.matchmaking.search(3)
    await svc.account.delete_account(3)
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 0


async def test_delete_drops_the_ram_chat_buffer(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    sid = await start_chat(svc, 1, 2)
    await svc.relay.relay(1, Incoming("text", text="private words"))
    assert svc.buffer.snapshot_for_report(sid, 2) is not None
    await svc.account.delete_account(1)
    assert svc.buffer.snapshot_for_report(sid, 2) is None


async def test_delete_removes_own_blocks_but_keeps_blocks_against_the_user(app_pool, svc):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    await repo_safety.add_block(app_pool, 1, 2)  # 1 blocked 2  (1's own list)
    await repo_safety.add_block(app_pool, 3, 1)  # 3 blocked 1  (protects 3)
    await svc.account.delete_account(1)
    rows = await app_pool.fetch("SELECT blocker_id, blocked_id FROM blocks")
    assert [(r["blocker_id"], r["blocked_id"]) for r in rows] == [(3, 1)]


async def test_delete_keeps_reports_and_restrictions_so_it_cannot_be_used_to_dodge_them(
    app_pool, svc
):
    from datetime import timedelta

    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    sid = await ended_session(app_pool, 2, 1)
    await svc.moderation.report(2, sid, "harassment")
    await repo_safety.add_restriction(app_pool, 1, "chat_cooldown", timedelta(hours=5), "t")
    await svc.account.delete_account(1)
    assert await app_pool.fetchval("SELECT count(*) FROM reports WHERE reported_id = 1") == 1
    assert await repo_safety.active_restriction(app_pool, 1, "chat_cooldown") is not None


async def test_delete_nulls_report_context_both_as_reporter_and_as_reported(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await make_user(app_pool, 3)
    s_against = await ended_session(app_pool, 2, 1)
    s_by = await ended_session(app_pool, 1, 3)
    for sid, reporter, reported in ((s_against, 2, 1), (s_by, 1, 3)):
        await app_pool.execute(
            "INSERT INTO reports (reporter_id, reported_id, session_id, source, reason, context) "
            'VALUES ($1, $2, $3, \'chat\', \'spam\', \'[{"from":"reporter","text":"secret"}]\')',
            reporter,
            reported,
            sid,
        )
    await svc.account.delete_account(1)
    rows = await app_pool.fetch("SELECT context FROM reports")
    assert len(rows) == 2 and all(r["context"] is None for r in rows)


async def test_delete_removes_unreferenced_session_metadata_but_keeps_reported_sessions(
    app_pool, svc
):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    plain = await ended_session(app_pool, 1, 2)
    reported = await ended_session(app_pool, 1, 3)
    await app_pool.execute(
        "INSERT INTO reports (reporter_id, reported_id, session_id, source, reason) "
        "VALUES (3, 1, $1, 'chat', 'spam')",
        reported,
    )
    await svc.account.delete_account(1)
    left = {r["id"] for r in await app_pool.fetch("SELECT id FROM chat_sessions")}
    assert plain not in left and reported in left
    assert (
        await app_pool.fetchval(
            "SELECT count(*) FROM chat_session_members WHERE session_id = $1", plain
        )
        == 0
    )


async def test_deleted_user_cannot_search_or_chat(app_pool, svc):
    await make_user(app_pool, 1)
    await svc.account.delete_account(1)
    assert (await svc.matchmaking.search(1)).kind == "not_eligible"
    assert (await svc.relay.relay(1, Incoming("text", text="hi"))).status == "not_in_chat"


async def test_deleted_user_can_start_over_but_needs_a_fresh_adult_confirmation(app_pool, svc):
    await make_user(app_pool, 1)
    await svc.account.delete_account(1)
    assert (await svc.onboarding.start(1, "newname")).state == "need_age_confirmation"
    row = await repo_users.get(app_pool, 1)
    assert (row["status"], row["age_confirmed_at"], row["username"]) == (
        "onboarding",
        None,
        "newname",
    )
    assert (await svc.matchmaking.search(1)).kind == "not_eligible"


async def test_underage_user_deleting_their_data_stays_blocked(app_pool, svc):
    await svc.onboarding.start(1, "kid")
    await svc.onboarding.declare_minor(1)
    res = await svc.account.delete_account(1)
    assert (res.kind, res.kept_block) == ("deleted", True)
    row = await repo_users.get(app_pool, 1)
    assert row["status"] == "blocked_underage" and row["username"] is None
    assert await repo_users.tombstone_reason(app_pool, 1) == "underage"
    assert (await svc.onboarding.start(1, None)).state == "blocked"  # cannot come back


async def test_banned_user_deleting_their_data_stays_banned(app_pool, svc):
    await make_user(app_pool, 1)
    await app_pool.execute(
        "UPDATE users SET status = 'banned', status_reason = 'abuse' WHERE id = 1"
    )
    await app_pool.execute("INSERT INTO deletion_tombstones (user_id, reason) VALUES (1, 'banned')")
    res = await svc.account.delete_account(1)
    assert res.kept_block is True
    row = await repo_users.get(app_pool, 1)
    assert (row["status"], row["status_reason"], row["username"]) == ("banned", "abuse", None)
    assert (await svc.onboarding.start(1, None)).state == "blocked"


async def test_normal_user_leaves_no_tombstone(app_pool, svc):
    await make_user(app_pool, 1)
    await svc.account.delete_account(1)
    assert await repo_users.tombstone_reason(app_pool, 1) is None


@pytest.mark.parametrize("table", ["profiles", "chat_queue"])
async def test_no_personal_rows_remain_after_deletion(app_pool, svc, table):
    await make_user(app_pool, 1)
    await svc.matchmaking.search(1)
    await svc.account.delete_account(1)
    key = "user_id"
    assert await app_pool.fetchval(f"SELECT count(*) FROM {table} WHERE {key} = 1") == 0  # noqa: S608

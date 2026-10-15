"""Reports and blocks: authorisation, rate limits, privacy of the stored context."""

import asyncio
import json
from datetime import timedelta

import pytest

from bot.db import repo_chat, repo_safety
from bot.services.chat_relay import Incoming
from tests.factories import ended_session, make_user, start_chat


@pytest.fixture
async def pair(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    return 1, 2, await start_chat(svc, 1, 2)


async def only_report(pool):
    return await pool.fetchrow("SELECT * FROM reports")


# ---------------- reports ----------------


async def test_report_is_stored_against_the_other_participant(app_pool, svc, pair):
    a, b, sid = pair
    res = await svc.moderation.report(a, sid, "harassment")
    assert res.kind == "created"
    row = await only_report(app_pool)
    assert (row["reporter_id"], row["reported_id"], row["session_id"]) == (a, b, sid)
    assert (row["reason"], row["source"], row["status"]) == ("harassment", "chat", "open")


async def test_reporting_ends_an_active_chat_blocks_and_tells_who_to_notify(app_pool, svc, pair):
    a, b, sid = pair
    res = await svc.moderation.report(a, sid, "spam")
    assert res.partner_id == b
    assert await repo_chat.get_partner(app_pool, a) is None
    assert await repo_safety.is_blocked_either_way(app_pool, a, b)
    assert (await svc.matchmaking.search(a)).kind == "queued"
    assert (await svc.matchmaking.search(b)).kind == "queued"  # blocked pair is never re-matched


async def test_report_can_be_filed_after_the_chat_ended(app_pool, svc, pair):
    a, b, sid = pair
    await svc.matchmaking.stop(b)
    res = await svc.moderation.report(a, sid, "scam")
    assert res.kind == "created" and res.partner_id is None


async def test_non_participant_cannot_report_or_block_a_session(app_pool, svc, pair):
    _, _, sid = pair
    await make_user(app_pool, 9)
    assert (await svc.moderation.report(9, sid, "spam")).kind == "not_member"
    assert (await svc.moderation.block(9, sid)).kind == "not_member"
    assert await only_report(app_pool) is None
    assert await app_pool.fetchval("SELECT count(*) FROM blocks") == 0


async def test_unknown_session_and_invalid_reason(app_pool, svc, pair):
    a, _, sid = pair
    assert (await svc.moderation.report(a, 987654, "spam")).kind == "not_member"
    assert (await svc.moderation.report(a, sid, "because")).kind == "invalid_reason"
    assert (
        await svc.moderation.report(a, sid, "spam'; DROP TABLE reports;--")
    ).kind == "invalid_reason"
    assert await app_pool.fetchval("SELECT count(*) FROM reports") == 0


async def test_sessions_older_than_the_window_cannot_be_reported(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    old = await ended_session(app_pool, 1, 2, ended_ago="8 days")
    assert (await svc.moderation.report(1, old, "spam")).kind == "not_member"
    recent = await ended_session(app_pool, 1, 2, ended_ago="6 days")
    assert (await svc.moderation.report(1, recent, "spam")).kind == "created"


async def test_duplicate_report_for_the_same_session_is_ignored(app_pool, svc, pair):
    a, _, sid = pair
    assert (await svc.moderation.report(a, sid, "spam")).kind == "created"
    assert (await svc.moderation.report(a, sid, "scam")).kind == "duplicate"
    assert await app_pool.fetchval("SELECT count(*) FROM reports") == 1


async def test_cannot_report_yourself_via_a_forged_session(app_pool, svc):
    await make_user(app_pool, 1)
    # a session with the same user twice is impossible (CHECK): a forged id can't self-report
    with pytest.raises(Exception):  # noqa: B017 - any DB error proves the constraint
        await app_pool.execute("INSERT INTO chat_sessions (user_a, user_b) VALUES (1, 1)")


async def test_report_rate_limit_per_hour(app_pool, svc):
    await make_user(app_pool, 1)
    kinds = []
    for other in range(10, 17):
        await make_user(app_pool, other)
        sid = await ended_session(app_pool, 1, other)
        kinds.append((await svc.moderation.report(1, sid, "spam")).kind)
    assert kinds == ["created"] * 5 + ["rate_limited"] * 2


async def test_report_rate_limit_holds_under_concurrency(app_pool, svc):
    await make_user(app_pool, 1)
    sids = []
    for other in range(10, 20):
        await make_user(app_pool, other)
        sids.append(await ended_session(app_pool, 1, other))
    results = await asyncio.gather(*(svc.moderation.report(1, s, "spam") for s in sids))
    assert sorted(r.kind for r in results).count("created") == 5
    assert await app_pool.fetchval("SELECT count(*) FROM reports WHERE reporter_id = 1") == 5


async def test_daily_report_limit(app_pool, svc):
    await make_user(app_pool, 1)
    for other in range(10, 31):
        await make_user(app_pool, other)
        sid = await ended_session(app_pool, 1, other)
        await app_pool.execute(
            "INSERT INTO reports "
            "(reporter_id, reported_id, session_id, source, reason, created_at) "
            "VALUES (1, $1, $2, 'chat', 'spam', now() - interval '3 hours')",
            other,
            sid,
        )
    await make_user(app_pool, 99)
    sid = await ended_session(app_pool, 1, 99)
    assert (await svc.moderation.report(1, sid, "spam")).kind == "rate_limited"  # 21st in a day


async def test_three_distinct_reporters_trigger_a_temporary_chat_cooldown(app_pool, svc):
    await make_user(app_pool, 50)
    outcomes = []
    for reporter in (1, 2, 3):
        await make_user(app_pool, reporter)
        sid = await ended_session(app_pool, reporter, 50)
        outcomes.append(await svc.moderation.report(reporter, sid, "harassment"))
    assert [o.restricted for o in outcomes] == [False, False, True]
    assert await repo_safety.active_restriction(app_pool, 50, "chat_cooldown") is not None
    assert (await svc.matchmaking.search(50)).kind == "cooldown"


async def test_repeat_reports_by_one_person_do_not_trigger_the_cooldown(app_pool, svc):
    await make_user(app_pool, 50)
    await make_user(app_pool, 1)
    for _ in range(3):
        sid = await ended_session(app_pool, 1, 50)
        await svc.moderation.report(1, sid, "spam")
    assert await repo_safety.active_restriction(app_pool, 50, "chat_cooldown") is None


# ---------------- privacy of the context ----------------


async def test_report_stores_buffered_messages_with_roles_not_ids(app_pool, svc):
    a, b = 777001, 777002  # distinctive ids so a substring check is meaningful
    await make_user(app_pool, a)
    await make_user(app_pool, b)
    sid = await start_chat(svc, a, b)
    await svc.relay.relay(a, Incoming("text", text="hello"))
    await svc.relay.relay(b, Incoming("text", text="send me money"))
    await svc.relay.relay(b, Incoming("sticker", sticker_id="S"))
    await svc.moderation.report(a, sid, "scam")
    row = await only_report(app_pool)
    ctx = json.loads(row["context"])
    assert [(m["from"], m["text"]) for m in ctx] == [
        ("reporter", "hello"),
        ("reported", "send me money"),
        ("reported", "[sticker]"),
    ]
    assert str(a) not in row["context"] and str(b) not in row["context"]


async def test_context_is_limited_to_the_last_ten_messages(app_pool, svc, pair, clock):
    a, b, sid = pair
    for i in range(14):
        await svc.relay.relay(a if i % 2 else b, Incoming("text", text=f"msg{i}"))
        clock.advance(2)
    await svc.moderation.report(a, sid, "spam")
    ctx = json.loads((await only_report(app_pool))["context"])
    assert [m["text"] for m in ctx] == [f"msg{i}" for i in range(4, 14)]


async def test_old_messages_do_not_leak_into_a_late_report(app_pool, svc, pair, clock):
    a, _, sid = pair
    await svc.relay.relay(a, Incoming("text", text="long ago"))
    clock.advance(11 * 60)
    await svc.matchmaking.stop(a)
    await svc.moderation.report(a, sid, "spam")
    assert (await only_report(app_pool))["context"] is None


async def test_report_without_buffered_messages_has_null_context(app_pool, svc, pair):
    a, _, sid = pair
    await svc.moderation.report(a, sid, "other")
    assert (await only_report(app_pool))["context"] is None


async def test_context_expires_after_30_days_and_is_purged(app_pool, svc, pair):
    a, _, sid = pair
    await svc.relay.relay(a, Incoming("text", text="evidence"))
    await svc.moderation.report(a, sid, "spam")
    row = await only_report(app_pool)
    life = await app_pool.fetchval(
        "SELECT $1::timestamptz - $2::timestamptz", row["context_expires_at"], row["created_at"]
    )
    assert life == timedelta(days=30)

    assert await svc.moderation.purge_expired_contexts() == 0  # not expired yet
    await app_pool.execute("UPDATE reports SET context_expires_at = now() - interval '1 second'")
    assert await svc.moderation.purge_expired_contexts() == 1
    after = await only_report(app_pool)
    assert after["context"] is None
    assert after["reason"] == "spam" and after["status"] == "open"  # the report itself stays


# ---------------- blocks ----------------


async def test_block_ends_the_chat_and_prevents_rematching(app_pool, svc, pair):
    a, b, sid = pair
    res = await svc.moderation.block(a, sid)
    assert (res.kind, res.partner_id) == ("blocked", b)
    assert await repo_chat.get_partner(app_pool, a) is None
    assert await repo_safety.is_blocked_either_way(app_pool, b, a)
    await svc.matchmaking.search(a)
    assert (await svc.matchmaking.search(b)).kind == "queued"


async def test_blocking_an_old_session_does_not_end_the_current_chat(app_pool, svc):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    old = await ended_session(app_pool, 1, 2)
    await start_chat(svc, 1, 3)
    res = await svc.moderation.block(1, old)
    assert (res.kind, res.partner_id) == ("blocked", None)
    assert await repo_chat.get_partner(app_pool, 1) is not None  # chat with 3 untouched


async def test_block_is_idempotent(app_pool, svc, pair):
    a, _, sid = pair
    await svc.moderation.block(a, sid)
    assert (await svc.moderation.block(a, sid)).kind == "blocked"
    assert await app_pool.fetchval("SELECT count(*) FROM blocks") == 1


async def test_mute_for_abuse_creates_a_temporary_restriction(app_pool, svc):
    await make_user(app_pool, 1)
    assert await svc.moderation.mute_for_abuse(1) is True
    assert await repo_safety.active_restriction(app_pool, 1, "mute") is not None
    assert await svc.moderation.mute_for_abuse(1) is False  # already muted: no pile-up
    assert await svc.moderation.mute_for_abuse(404) is False  # unknown users: nothing happens

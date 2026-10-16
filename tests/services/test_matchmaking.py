"""Queue / session safety: compatibility, blocks, restrictions and race conditions."""

import asyncio
from datetime import timedelta

import asyncpg
import pytest

from bot.db import repo_chat, repo_safety
from tests.factories import make_user, start_chat


async def active_rows(pool):
    return await pool.fetch("SELECT * FROM chat_session_members WHERE active")


async def test_two_compatible_users_are_matched_with_each_others_public_info(app_pool, svc):
    await make_user(app_pool, 1, gender="m", looking="f", age=30, city="Paris")
    await make_user(app_pool, 2, gender="f", looking="m", age=28, city="Rome")
    first = await svc.matchmaking.search(1)
    assert first.kind == "queued"
    second = await svc.matchmaking.search(2)
    assert second.kind == "matched"
    assert second.partner.user_id == 1
    assert second.me.user_id == 2
    assert {r["user_id"] for r in await active_rows(app_pool)} == {1, 2}
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 0


async def test_looking_for_is_not_a_filter_for_random_chat(app_pool, svc):
    await make_user(app_pool, 1, gender="m", looking="f")
    await make_user(app_pool, 2, gender="f", looking="f")  # wants women, user 1 is a man
    await make_user(app_pool, 3, gender="m", looking="m")
    await svc.matchmaking.search(1)
    assert (await svc.matchmaking.search(2)).kind == "matched"
    await svc.matchmaking.search(3)
    assert await app_pool.fetchval("SELECT count(*) FROM chat_sessions") == 1


async def test_queue_is_first_come_first_served(app_pool, svc, clock):
    # 1 and 2 are blocked from each other, so both wait; 3 must get the one who waited longest.
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    await repo_safety.add_block(app_pool, 1, 2)
    await svc.matchmaking.search(1)
    await asyncio.sleep(0.01)
    await svc.matchmaking.search(2)
    res = await svc.matchmaking.search(3)
    assert res.partner.user_id == 1
    assert await app_pool.fetchval("SELECT user_id FROM chat_queue") == 2


async def test_searching_twice_does_not_duplicate_queue_entry(app_pool, svc):
    await make_user(app_pool, 1)
    assert (await svc.matchmaking.search(1)).kind == "queued"
    assert (await svc.matchmaking.search(1)).kind == "already_queued"
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 1


async def test_user_in_chat_cannot_search_again(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await start_chat(svc, 1, 2)
    assert (await svc.matchmaking.search(1)).kind == "already_in_chat"
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "onboarding", "confirmed": False, "with_profile": False},
        {"status": "onboarding", "confirmed": True, "with_profile": False},
        {"status": "deleted", "confirmed": False, "with_profile": False},
        {"status": "blocked_underage", "confirmed": False, "with_profile": False},
        {"status": "banned", "confirmed": True, "with_profile": True},
    ],
)
async def test_ineligible_users_cannot_search(app_pool, svc, kwargs):
    await make_user(app_pool, 1, **kwargs)
    assert (await svc.matchmaking.search(1)).kind == "not_eligible"
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 0


async def test_user_who_becomes_ineligible_while_queued_is_skipped(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await svc.matchmaking.search(1)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = 1")
    assert (await svc.matchmaking.search(2)).kind == "queued"  # did not match the banned waiter


@pytest.mark.parametrize("blocker,blocked", [(1, 2), (2, 1)])
async def test_blocked_pairs_are_never_matched_in_either_direction(app_pool, svc, blocker, blocked):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await repo_safety.add_block(app_pool, blocker, blocked)
    await svc.matchmaking.search(1)
    assert (await svc.matchmaking.search(2)).kind == "queued"


async def test_chat_cooldown_restriction_blocks_searching_and_being_matched(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await make_user(app_pool, 3)
    await repo_safety.add_restriction(app_pool, 1, "chat_cooldown", timedelta(hours=1), "t")
    res = await svc.matchmaking.search(1)
    assert res.kind == "cooldown" and res.cooldown_until is not None
    # restricted AFTER queueing: waiter must be skipped
    await svc.matchmaking.search(2)
    await repo_safety.add_restriction(app_pool, 2, "chat_cooldown", timedelta(hours=1), "t")
    assert (await svc.matchmaking.search(3)).kind == "queued"


async def test_expired_restriction_no_longer_applies(app_pool, svc):
    await make_user(app_pool, 1)
    await app_pool.execute(
        "INSERT INTO restrictions (user_id, kind, until) "
        "VALUES (1, 'chat_cooldown', now() - interval '1 second')"
    )
    assert (await svc.matchmaking.search(1)).kind == "queued"


async def test_users_who_just_talked_are_not_rematched_immediately(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await start_chat(svc, 1, 2)
    await svc.matchmaking.stop(1)
    clock.advance(60)
    assert (await svc.matchmaking.search(1)).kind == "queued"
    assert (await svc.matchmaking.search(2)).kind == "queued"  # still excluded
    # after the cool-off they can meet again
    await app_pool.execute("DELETE FROM chat_queue")
    await app_pool.execute("UPDATE chat_sessions SET ended_at = now() - interval '11 minutes'")
    clock.advance(60)
    assert (await svc.matchmaking.search(1)).kind == "queued"
    assert (await svc.matchmaking.search(2)).kind == "matched"


async def test_stop_ends_session_for_both_and_frees_both(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    sid = await start_chat(svc, 1, 2)
    res = await svc.matchmaking.stop(1)
    assert (res.kind, res.session_id, res.partner_id) == ("ended", sid, 2)
    assert await active_rows(app_pool) == []
    row = await app_pool.fetchrow("SELECT * FROM chat_sessions WHERE id = $1", sid)
    assert row["status"] == "ended" and row["ended_by"] == 1 and row["ended_at"] is not None
    assert await repo_chat.get_partner(app_pool, 2) is None


async def test_stop_while_queued_leaves_the_queue_and_when_idle_does_nothing(app_pool, svc):
    await make_user(app_pool, 1)
    await svc.matchmaking.search(1)
    assert (await svc.matchmaking.stop(1)).kind == "left_queue"
    assert (await svc.matchmaking.stop(1)).kind == "nothing"


async def test_next_ends_current_chat_and_searches_again(app_pool, svc):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    await svc.matchmaking.search(3)  # waiting
    await start_chat_pair_without_3(app_pool, svc)
    stopped, found = await svc.matchmaking.next(1)
    assert stopped.kind == "ended" and found.kind == "matched" and found.partner.user_id == 3


async def start_chat_pair_without_3(pool, svc):
    # 1 and 2 are matched manually so that 3 keeps waiting in the queue
    await pool.execute("DELETE FROM chat_queue WHERE user_id = 3")
    await start_chat(svc, 1, 2)
    await svc.matchmaking.search(3)


async def test_search_rate_limit_stops_search_spam(app_pool, svc, clock):
    await make_user(app_pool, 1)
    results = [(await svc.matchmaking.search(1)).kind for _ in range(8)]
    assert results.count("rate_limited") == 3  # burst of 5 allowed
    clock.advance(30)
    assert (await svc.matchmaking.search(1)).kind != "rate_limited"


# ---------------- races ----------------


async def test_two_searchers_racing_for_one_waiter_only_one_wins(app_pool, svc):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    await svc.matchmaking.search(1)  # waiter
    r2, r3 = await asyncio.gather(svc.matchmaking.search(2), svc.matchmaking.search(3))
    assert sorted([r2.kind, r3.kind]) == ["matched", "queued"]
    assert await app_pool.fetchval("SELECT count(*) FROM chat_sessions") == 1
    members = await active_rows(app_pool)
    assert len(members) == 2 and 1 in {m["user_id"] for m in members}
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 1  # the loser waits


@pytest.mark.parametrize("n", [20, 21])
async def test_mass_concurrent_search_never_matches_anyone_twice(app_pool, svc, n):
    for uid in range(1, n + 1):
        await make_user(app_pool, uid)
    results = await asyncio.gather(*(svc.matchmaking.search(u) for u in range(1, n + 1)))
    assert all(r.kind in ("matched", "queued") for r in results)

    per_user = await app_pool.fetch(
        "SELECT user_id, count(*) c FROM chat_session_members WHERE active GROUP BY user_id"
    )
    assert all(r["c"] == 1 for r in per_user)
    sessions = await app_pool.fetchval("SELECT count(*) FROM chat_sessions WHERE status = 'active'")
    assert sessions == n // 2
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == n % 2
    # nobody is both in a session and in the queue
    assert (
        await app_pool.fetchval(
            "SELECT count(*) FROM chat_queue q JOIN chat_session_members m "
            "ON m.user_id = q.user_id AND m.active"
        )
        == 0
    )
    # each session pairs two distinct users and each matched result pairs symmetric partners
    matched = [r for r in results if r.kind == "matched"]
    assert len(matched) == n // 2
    assert len({frozenset((r.me.user_id, r.partner.user_id)) for r in matched}) == len(matched)


async def test_same_user_searching_concurrently_gets_one_entry(app_pool, svc, clock):
    await make_user(app_pool, 1)
    results = await asyncio.gather(*(svc.matchmaking.search(1) for _ in range(5)))
    assert sorted(r.kind for r in results).count("queued") == 1
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 1


async def test_both_sides_ending_the_chat_simultaneously_ends_it_once(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await start_chat(svc, 1, 2)
    a, b = await asyncio.gather(svc.matchmaking.stop(1), svc.matchmaking.stop(2))
    assert sorted([a.kind, b.kind]) == ["ended", "nothing"]
    assert await active_rows(app_pool) == []


async def test_database_itself_refuses_a_second_active_session_for_a_user(app_pool, svc):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    await start_chat(svc, 1, 2)
    sid = await app_pool.fetchval(
        "INSERT INTO chat_sessions (user_a, user_b) VALUES (1, 3) RETURNING id"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_pool.execute(
            "INSERT INTO chat_session_members (session_id, user_id) VALUES ($1, 1), ($1, 3)", sid
        )


async def test_search_racing_with_account_deletion_never_pairs_a_deleted_user(app_pool, svc):
    for uid in (1, 2):
        await make_user(app_pool, uid)
    await svc.matchmaking.search(1)
    res = await asyncio.gather(svc.account.delete_account(1), svc.matchmaking.search(2))
    assert res[0].kind == "deleted"
    assert (
        await app_pool.fetchval(
            "SELECT count(*) FROM chat_session_members WHERE active AND user_id = 1"
        )
        == 0
    )
    if res[1].kind == "matched":  # search won the lock first: the chat was ended by the deletion
        assert (
            await app_pool.fetchval("SELECT count(*) FROM chat_sessions WHERE status = 'active'")
            == 0
        )
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue WHERE user_id = 1") == 0


async def test_stale_queue_entries_expire(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await svc.matchmaking.search(1)
    await svc.matchmaking.search(2)  # these two cannot match? they can: both any
    await app_pool.execute("DELETE FROM chat_sessions")  # reset
    await app_pool.execute(
        "INSERT INTO chat_queue (user_id, joined_at) VALUES (1, now() - interval '31 minutes') "
        "ON CONFLICT (user_id) DO UPDATE SET joined_at = EXCLUDED.joined_at"
    )
    expired = await svc.matchmaking.expire_stale_queue()
    assert expired == [1]

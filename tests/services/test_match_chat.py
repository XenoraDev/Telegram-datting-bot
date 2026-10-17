"""Opening a chat with a mutual match (MatchmakingService.chat_with_match): races and rules."""

import asyncio

from tests.factories import make_user


async def handle(pool, uid):
    return (await pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", uid)).hex


async def matched(pool, svc, a=1, b=2):
    for uid in (a, b):
        await make_user(pool, uid)
    lo, hi = min(a, b), max(a, b)
    await pool.execute("INSERT INTO matches (user_a_id, user_b_id) VALUES ($1, $2)", lo, hi)


async def test_both_sides_pressing_at_the_same_moment_make_exactly_one_chat(app_pool, svc):
    await matched(app_pool, svc)
    for _ in range(5):
        results = await asyncio.gather(
            svc.matchmaking.chat_with_match(1, await handle(app_pool, 2)),
            svc.matchmaking.chat_with_match(2, await handle(app_pool, 1)),
        )
        assert sorted(r.kind for r in results) == ["already", "started"]
        assert await app_pool.fetchval("SELECT count(*) FROM chat_sessions") == 1
        assert (await svc.matchmaking.stop(1)).kind == "ended"
        await app_pool.execute("DELETE FROM chat_sessions")


async def test_a_random_search_and_a_match_chat_cannot_both_win(app_pool, svc):
    await matched(app_pool, svc, 1, 2)
    await make_user(app_pool, 3)
    await svc.matchmaking.search(3)  # 3 waits in the queue
    pid = await handle(app_pool, 2)
    results = await asyncio.gather(
        svc.matchmaking.chat_with_match(1, pid), svc.matchmaking.search(2)
    )
    assert (
        await app_pool.fetchval(
            "SELECT count(*) FROM chat_session_members WHERE active AND user_id = 2"
        )
        == 1
    )  # 2 is in exactly one chat, whichever won
    assert {r.kind for r in results} <= {"started", "matched", "partner_busy", "already_in_chat"}


async def test_the_result_carries_only_ids_and_no_contact_data(app_pool, svc):
    await matched(app_pool, svc)
    res = await svc.matchmaking.chat_with_match(1, await handle(app_pool, 2))
    assert res.kind == "started" and res.partner.user_id == 2 and res.me.user_id == 1
    assert set(res.partner.__slots__) == {"user_id"}


async def test_bad_handles_and_an_unmatched_pair_are_unavailable(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    for handle_text in (
        "",
        "zz",
        "g" * 32,
        "0" * 32,
        await handle(app_pool, 2),
        await handle(app_pool, 1),
    ):
        assert (await svc.matchmaking.chat_with_match(1, handle_text)).kind == "unavailable"
    assert await app_pool.fetchval("SELECT count(*) FROM chat_sessions") == 0

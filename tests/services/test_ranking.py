"""The documented ranking, all criteria together, deterministic, and blind to private data."""

import asyncio

from tests.factories import give_tier, make_user

V = 9900


async def order(svc, viewer=V):
    out = []
    for _ in range(12):
        res = await svc.browse.next_card(viewer)
        if res.kind != "card":
            break
        out.append(res.card.name)
        await svc.browse.swipe(viewer, res.card.public_id, "pass")
    return out


async def boosted(pool, svc, uid):
    await svc.entitlements.grant(uid, "profile_boost", {"enabled": True})
    assert (await svc.boost.activate(uid)).kind == "activated"


async def seen_ago(pool, uid, minutes):
    await pool.execute(
        "UPDATE users SET last_seen_at = now() - make_interval(mins => $2) WHERE id = $1",
        uid,
        minutes,
    )


async def test_browse_order_is_likers_then_boost_then_tier_then_recency(app_pool, svc):
    await make_user(app_pool, V)
    for uid in range(1, 9):
        await make_user(app_pool, uid)
    # recency: higher id = less recent (1 is the freshest)
    for uid in range(1, 9):
        await seen_ago(app_pool, uid, uid)
    await app_pool.execute(
        "INSERT INTO swipes (swiper_id, target_id, action) VALUES (8, $1, 'like')", V
    )
    await boosted(app_pool, svc, 6)  # boost, free tier
    await boosted(app_pool, svc, 5)  # boost, free tier, fresher than 6
    await give_tier(app_pool, 4, "vip")
    await give_tier(app_pool, 3, "pro")
    await give_tier(app_pool, 2, "premium")
    # 8 liked the viewer; 5, 6 boosted (5 fresher); then VIP 4, Pro 3, Premium 2; then 1, 7
    assert await order(svc) == [f"Name{n}" for n in (8, 5, 6, 4, 3, 2, 1, 7)]


async def test_the_order_is_stable_between_calls_and_breaks_ties_by_id(app_pool, svc):
    await make_user(app_pool, V)
    for uid in (3, 1, 2):
        await make_user(app_pool, uid)
    await app_pool.execute("UPDATE users SET last_seen_at = '2026-01-01' WHERE id IN (1, 2, 3)")
    first = await svc.browse.next_card(V)
    second = await svc.browse.next_card(V)
    assert first.card.public_id == second.card.public_id and first.card.name == "Name1"


async def test_an_expired_boost_and_an_expired_subscription_rank_as_nothing(app_pool, svc):
    await make_user(app_pool, V)
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
        await seen_ago(app_pool, uid, uid)
    sub = await give_tier(app_pool, 3, "vip")
    await app_pool.execute("UPDATE subscriptions SET current_period_end = now() WHERE id = $1", sub)
    await boosted(app_pool, svc, 2)
    await app_pool.execute(
        "UPDATE profile_boosts SET started_at = now() - interval '3 hours', "
        "ends_at = now() - interval '2 hours', cooldown_until = now() + interval '9 hours'"
    )
    assert await order(svc) == ["Name1", "Name2", "Name3"]


async def test_ranking_never_shows_what_the_owner_hides(app_pool, svc):
    await make_user(app_pool, V)
    await make_user(app_pool, 1, age=47, city="Secretville")
    await app_pool.execute(
        "UPDATE profiles SET show_age = false, show_city = false WHERE user_id = 1"
    )
    await give_tier(app_pool, 1, "vip")
    await boosted(app_pool, svc, 1)
    res = await svc.browse.next_card(V)
    assert res.card.name == "Name1" and res.card.age is None and res.card.city is None
    assert "47" not in str((res.card.age, res.card.city)) and "Secret" not in repr(res.card)


async def test_chat_queue_order_is_starved_then_boost_then_tier_then_wait(app_pool, svc):
    """Waiters that cannot pair with each other (blocked); the newcomer picks the best one."""
    waiting = [1, 2, 3, 4]
    for uid in waiting:
        await make_user(app_pool, uid)
        await app_pool.execute("INSERT INTO chat_queue (user_id) VALUES ($1)", uid)
        await asyncio.sleep(0.01)
    for a in waiting:
        for b in waiting:
            if a < b:
                await app_pool.execute(
                    "INSERT INTO blocks (blocker_id, blocked_id) VALUES ($1, $2)", a, b
                )
    await make_user(app_pool, 50)
    await app_pool.execute("UPDATE chat_queue SET priority = 3 WHERE user_id = 3")  # VIP waiter
    await boosted(app_pool, svc, 2)  # boosted free waiter
    await app_pool.execute(
        "UPDATE chat_queue SET joined_at = now() - interval '5 minutes' WHERE user_id = 4"
    )  # waited too long: goes first
    picked = []
    for newcomer in (50, 51, 52, 53):
        if newcomer != 50:
            await make_user(app_pool, newcomer)
        out = await svc.matchmaking.search(newcomer)
        picked.append(out.partner.user_id)
    assert picked == [4, 2, 3, 1]


async def test_a_boost_cannot_pair_a_user_who_is_not_eligible(app_pool, svc):
    for uid in (1, 2, 50):
        await make_user(app_pool, uid)
        if uid != 50:
            await app_pool.execute("INSERT INTO chat_queue (user_id) VALUES ($1)", uid)
    await app_pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (1, 2)")
    await boosted(app_pool, svc, 2)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = 2")
    assert (await svc.matchmaking.search(50)).partner.user_id == 1

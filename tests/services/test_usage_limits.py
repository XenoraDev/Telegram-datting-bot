"""Daily limits are enforced server-side from entitlement data and reset with the UTC day.

The numbers come from plan_entitlements, so the tests shrink them with SQL to stay fast; the
documented values (100 / 100 / 50 ...) are asserted in test_tiers.py."""

import pytest

from tests.factories import give_tier, make_user

DAY = 86400.0


async def set_limit(pool, plan_code, feature, limit):
    import json

    await pool.execute(
        "UPDATE plan_entitlements pe SET value = $3::jsonb FROM plans p "
        "WHERE p.id = pe.plan_id AND p.code = $1 AND pe.feature_key = $2",
        plan_code,
        feature,
        json.dumps({"enabled": True, "daily_limit": limit}),
    )


async def start_and_leave(svc, clock, uid):
    """One random-chat start, then leave the queue so the next press is a fresh start."""
    clock.advance(13)  # stay under the per-minute `chat_start` rate limit
    res = await svc.matchmaking.search(uid)
    if res.kind == "queued":
        await svc.matchmaking.stop(uid)
    return res


# ------------------------------------------------------------------ random chat starts


async def test_chat_starts_are_allowed_up_to_the_limit_then_blocked_with_the_limit_reported(
    app_pool, svc, clock
):
    await make_user(app_pool, 1)
    await set_limit(app_pool, "free", "random_chat", 3)
    kinds = [(await start_and_leave(svc, clock, 1)).kind for _ in range(3)]
    assert kinds == ["queued"] * 3  # at the limit: the last allowed one still works
    blocked = await start_and_leave(svc, clock, 1)
    assert (blocked.kind, blocked.limit) == ("limit_reached", 3)
    assert await svc.entitlements.used_today(1, "random_chat") == 3  # a block costs nothing
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 0


async def test_usage_resets_on_the_next_utc_day(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await set_limit(app_pool, "free", "random_chat", 1)
    assert (await start_and_leave(svc, clock, 1)).kind == "queued"
    assert (await start_and_leave(svc, clock, 1)).kind == "limit_reached"
    clock.advance(DAY)
    assert (await start_and_leave(svc, clock, 1)).kind == "queued"


async def test_a_search_that_does_not_start_anything_is_not_charged(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2, status="onboarding")
    await set_limit(app_pool, "free", "random_chat", 5)
    assert (await start_and_leave(svc, clock, 2)).kind == "not_eligible"
    assert (await svc.matchmaking.search(1)).kind == "queued"
    clock.advance(13)
    assert (await svc.matchmaking.search(1)).kind == "already_queued"
    assert await svc.entitlements.used_today(1, "random_chat") == 1
    assert await svc.entitlements.used_today(2, "random_chat") == 0
    await app_pool.execute(
        "INSERT INTO restrictions (user_id, kind, until) "
        "VALUES (1, 'chat_cooldown', now() + interval '1 hour')"
    )
    await svc.matchmaking.stop(1)
    clock.advance(13)
    assert (await svc.matchmaking.search(1)).kind == "cooldown"
    assert await svc.entitlements.used_today(1, "random_chat") == 1


async def test_a_matched_pair_each_paid_for_their_own_start(app_pool, svc, clock):
    for uid in (1, 2):
        await make_user(app_pool, uid)
    await set_limit(app_pool, "free", "random_chat", 5)
    await svc.matchmaking.search(1)
    clock.advance(13)
    assert (await svc.matchmaking.search(2)).kind == "matched"
    assert [await svc.entitlements.used_today(u, "random_chat") for u in (1, 2)] == [1, 1]


async def test_a_paid_tier_has_a_higher_limit(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await give_tier(app_pool, 2, "premium")
    await set_limit(app_pool, "free", "random_chat", 2)
    await set_limit(app_pool, "t_premium", "random_chat", 4)
    free = [(await start_and_leave(svc, clock, 1)).kind for _ in range(3)]
    paid = [(await start_and_leave(svc, clock, 2)).kind for _ in range(5)]
    assert free == ["queued", "queued", "limit_reached"]
    assert paid == ["queued"] * 4 + ["limit_reached"]


async def test_vip_has_no_daily_limit_but_is_still_rate_limited(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await give_tier(app_pool, 1, "vip")
    kinds = []
    for _ in range(12):  # presses without waiting: the per-second guard still applies
        res = await svc.matchmaking.search(1)
        kinds.append(res.kind)
        if res.kind == "queued":
            await svc.matchmaking.stop(1)
    assert "limit_reached" not in kinds and "rate_limited" in kinds
    kinds = [(await start_and_leave(svc, clock, 1)).kind for _ in range(30)]
    assert set(kinds) == {"queued"}  # at a human pace the unlimited tier is never blocked


# ------------------------------------------------------------------ browse views


async def seed_profiles(pool, n):
    for uid in range(100, 100 + n):
        await make_user(pool, uid)


async def test_browse_views_are_limited_and_reset(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await seed_profiles(app_pool, 6)
    await set_limit(app_pool, "free", "browse", 2)
    got = []
    for _ in range(3):
        clock.advance(1)
        got.append(await svc.browse.next_card(1))
    assert [g.kind for g in got] == ["card", "card", "limit_reached"] and got[2].limit == 2
    clock.advance(DAY)
    assert (await svc.browse.next_card(1)).kind == "card"


async def test_an_empty_browse_does_not_use_up_the_allowance(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await set_limit(app_pool, "free", "browse", 1)
    for _ in range(4):
        clock.advance(1)
        assert (await svc.browse.next_card(1)).kind == "empty"
    assert await svc.entitlements.used_today(1, "browse") == 0


async def test_unlimited_browsing_is_still_rate_limited(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await give_tier(app_pool, 1, "vip")
    await seed_profiles(app_pool, 3)
    kinds = [(await svc.browse.next_card(1)).kind for _ in range(40)]
    assert "rate_limited" in kinds and "limit_reached" not in kinds


# ------------------------------------------------------------------ likes


async def test_only_likes_count_and_the_limit_is_exact(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await seed_profiles(app_pool, 5)
    await set_limit(app_pool, "free", "swipe", 2)
    pids = [
        r["public_id"].hex
        for r in await app_pool.fetch(
            "SELECT public_id FROM profiles WHERE user_id >= 100 ORDER BY user_id"
        )
    ]
    clock.advance(1)
    assert (await svc.browse.swipe(1, pids[0], "pass")).kind == "passed"  # passes are free
    assert (await svc.browse.swipe(1, pids[1], "like")).kind == "liked"
    assert (await svc.browse.swipe(1, pids[2], "like")).kind == "liked"
    blocked = await svc.browse.swipe(1, pids[3], "like")
    assert (blocked.kind, blocked.limit) == ("limit_reached", 2)
    assert await app_pool.fetchval("SELECT count(*) FROM swipes WHERE swiper_id = 1") == 3
    assert (await svc.browse.swipe(1, pids[3], "pass")).kind == "passed"  # passing still works
    clock.advance(DAY)
    assert (await svc.browse.swipe(1, pids[4], "like")).kind == "liked"


async def test_a_duplicate_like_gives_its_use_back(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await seed_profiles(app_pool, 1)
    await set_limit(app_pool, "free", "swipe", 5)
    pid = await app_pool.fetchval("SELECT public_id FROM profiles WHERE user_id = 100")
    assert (await svc.browse.swipe(1, pid.hex, "like")).kind == "liked"
    clock.advance(1)
    assert (await svc.browse.swipe(1, pid.hex, "like")).kind == "duplicate"
    assert await svc.entitlements.used_today(1, "swipe") == 1


async def test_vip_likes_are_unlimited_but_rate_limited(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await give_tier(app_pool, 1, "vip")
    await seed_profiles(app_pool, 40)
    pids = [
        r["public_id"].hex
        for r in await app_pool.fetch("SELECT public_id FROM profiles WHERE user_id >= 100")
    ]
    kinds = [(await svc.browse.swipe(1, p, "like")).kind for p in pids]
    assert "rate_limited" in kinds and "limit_reached" not in kinds


async def test_mutual_match_still_happens_when_the_second_like_is_the_last_allowed(
    app_pool, svc, clock
):
    for uid in (1, 2):
        await make_user(app_pool, uid)
    await set_limit(app_pool, "free", "swipe", 1)
    p1 = (await app_pool.fetchval("SELECT public_id FROM profiles WHERE user_id = 1")).hex
    p2 = (await app_pool.fetchval("SELECT public_id FROM profiles WHERE user_id = 2")).hex
    assert (await svc.browse.swipe(1, p2, "like")).kind == "liked"
    assert (await svc.browse.swipe(2, p1, "like")).kind == "match"


# ------------------------------------------------------------------ priority


async def test_higher_tiers_are_matched_first_then_the_longest_waiting(app_pool, svc, clock):
    for uid in (1, 2, 3, 4):
        await make_user(app_pool, uid)
    await give_tier(app_pool, 2, "vip")
    await give_tier(app_pool, 4, "pro")
    # 1 (free) and 2 (vip) wait because they blocked each other; 3 then searches
    await app_pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (1, 2)")
    assert (await svc.matchmaking.search(1)).kind == "queued"
    clock.advance(13)
    assert (await svc.matchmaking.search(2)).kind == "queued"
    clock.advance(13)
    res = await svc.matchmaking.search(3)
    assert (
        res.kind == "matched" and res.partner.user_id == 2
    )  # vip jumped the longer-waiting free user
    clock.advance(13)
    assert (await svc.matchmaking.search(4)).partner.user_id == 1


async def test_equal_priority_keeps_first_come_first_served(app_pool, svc, clock):
    for uid in (1, 2, 3, 4):
        await make_user(app_pool, uid)
    await app_pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (1, 2)")
    await svc.matchmaking.search(1)
    clock.advance(13)
    await svc.matchmaking.search(2)
    clock.advance(13)
    assert (await svc.matchmaking.search(3)).partner.user_id == 1


async def test_browse_shows_higher_tiers_earlier_among_equals(app_pool, svc, clock):
    for uid in (1, 10, 11, 12):
        await make_user(app_pool, uid)
    await give_tier(app_pool, 11, "vip")
    await give_tier(app_pool, 12, "premium")
    await app_pool.execute("UPDATE users SET last_seen_at = now() WHERE id = 10")
    await app_pool.execute(
        "UPDATE users SET last_seen_at = now() - interval '1 day' WHERE id IN (11, 12)"
    )
    names = []
    for _ in range(3):
        clock.advance(1)
        card = (await svc.browse.next_card(1)).card
        names.append(card.name)
        await svc.browse.swipe(1, card.public_id, "pass")
    assert names == ["Name11", "Name12", "Name10"]


@pytest.mark.parametrize("feature", ["random_chat", "browse", "swipe"])
async def test_a_user_cannot_use_a_disabled_feature(app_pool, svc, feature):
    await make_user(app_pool, 1)
    await app_pool.execute(
        "UPDATE plan_entitlements pe SET value = '{\"enabled\": false}' FROM plans p "
        "WHERE p.id = pe.plan_id AND p.code = 'free' AND pe.feature_key = $1",
        feature,
    )
    res = await svc.entitlements.consume(1, feature)
    assert (res.allowed, res.reason) == (False, "disabled")

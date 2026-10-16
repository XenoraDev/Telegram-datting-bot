"""Profile boost: entitlement, one-at-a-time, cooldown, expiry, ranking, safety."""

import asyncio

import pytest

from tests.factories import give_tier, make_user

U = 7000


async def expire(pool, uid, *, cooldown_over=False):
    """Move a boost into the past (as if the time had passed)."""
    await pool.execute(
        "UPDATE profile_boosts SET started_at = now() - interval '3 hours', "
        "ends_at = now() - interval '2 hours', "
        "cooldown_until = CASE WHEN $2 THEN now() - interval '1 minute' "
        "ELSE now() + interval '9 hours' END WHERE user_id = $1",
        uid,
        cooldown_over,
    )


async def first_card(svc, viewer):
    res = await svc.browse.next_card(viewer)
    return res.card.name if res.kind == "card" else None


# ---------------------------------------------------------------- entitlement


@pytest.mark.parametrize("tier", [None, "premium"])
async def test_free_and_premium_cannot_boost(app_pool, svc, tier):
    await make_user(app_pool, U)
    if tier:
        await give_tier(app_pool, U, tier)
    assert (await svc.boost.status(U)).kind == "denied"
    assert (await svc.boost.activate(U)).kind == "denied"
    assert await app_pool.fetchval("SELECT count(*) FROM profile_boosts") == 0


@pytest.mark.parametrize("tier", ["pro", "vip"])
async def test_pro_and_vip_can_boost(app_pool, svc, tier):
    await make_user(app_pool, U)
    await give_tier(app_pool, U, tier)
    assert (await svc.boost.status(U)).kind == "idle"
    res = await svc.boost.activate(U)
    assert res.kind == "activated"
    assert (await svc.boost.status(U)).kind == "active"
    assert await svc.boost.is_boosted(U)


async def test_the_decision_is_made_from_the_entitlement_not_the_plan_name(app_pool, svc):
    """A grant of `profile_boost` (no plan at all) is enough; a plan called "pro" without the
    entitlement is not."""
    await make_user(app_pool, U)
    pid = await app_pool.fetchval(
        "INSERT INTO plans (code, name) VALUES ('pro', 'Pro') RETURNING id"
    )
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "VALUES ($1, $2, now() + interval '3 days')",
        U,
        pid,
    )
    assert (await svc.boost.activate(U)).kind == "denied"
    await svc.entitlements.grant(U, "profile_boost", {"enabled": True})
    assert (await svc.boost.activate(U)).kind == "activated"


# ---------------------------------------------------------------- activation rules


async def test_duration_and_the_expiry_time_are_reported(app_pool, svc):
    await make_user(app_pool, U)
    await give_tier(app_pool, U, "pro")
    res = await svc.boost.activate(U)
    minutes = await app_pool.fetchval(
        "SELECT extract(epoch FROM ends_at - started_at) / 60 FROM profile_boosts "
        "WHERE user_id = $1",
        U,
    )
    assert round(minutes) == 60
    assert res.ends_at is not None and res.next_at > res.ends_at


async def test_an_active_boost_is_not_stacked_and_tells_when_it_ends(app_pool, svc, clock):
    await make_user(app_pool, U)
    await give_tier(app_pool, U, "pro")
    first = await svc.boost.activate(U)
    clock.advance(30)
    second = await svc.boost.activate(U)
    assert second.kind == "already_active" and second.ends_at == first.ends_at
    assert await app_pool.fetchval("SELECT count(*) FROM profile_boosts") == 1


async def test_cooldown_after_expiry_then_a_new_boost_is_possible(app_pool, svc, clock):
    await make_user(app_pool, U)
    await give_tier(app_pool, U, "pro")
    await svc.boost.activate(U)
    await expire(app_pool, U)  # boost over, cooldown still running
    assert not await svc.boost.is_boosted(U)
    assert (await svc.boost.status(U)).kind == "cooldown"
    clock.advance(30)
    refused = await svc.boost.activate(U)
    assert refused.kind == "cooldown" and refused.next_at is not None
    await expire(app_pool, U, cooldown_over=True)
    clock.advance(30)
    assert (await svc.boost.activate(U)).kind == "activated"


async def test_concurrent_activation_creates_exactly_one_boost(app_pool, svc):
    await make_user(app_pool, U)
    await give_tier(app_pool, U, "pro")
    # the limiter is a per-second guard; the database is what must hold the line
    results = await asyncio.gather(*(svc.boost._activate_unthrottled(U) for _ in range(12)))
    assert sorted(r.kind for r in results).count("activated") == 1
    assert {r.kind for r in results} <= {"activated", "already_active"}
    assert await app_pool.fetchval("SELECT count(*) FROM profile_boosts") == 1


async def test_pressing_in_a_burst_is_rate_limited(app_pool, svc):
    await make_user(app_pool, U)
    await give_tier(app_pool, U, "pro")
    kinds = [(await svc.boost.activate(U)).kind for _ in range(5)]
    assert kinds[0] == "activated" and "rate_limited" in kinds


async def test_an_ineligible_user_cannot_boost(app_pool, svc):
    await make_user(app_pool, U)
    await give_tier(app_pool, U, "vip")
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", U)
    assert (await svc.boost.activate(U)).kind == "not_eligible"
    assert (await svc.boost.status(U)).kind == "not_eligible"
    assert await app_pool.fetchval("SELECT count(*) FROM profile_boosts") == 0


async def test_one_users_boost_does_not_touch_anothers(app_pool, svc):
    await make_user(app_pool, U)
    await make_user(app_pool, U + 1)
    await give_tier(app_pool, U, "pro")
    await give_tier(app_pool, U + 1, "pro")
    await svc.boost.activate(U)
    assert (await svc.boost.status(U + 1)).kind == "idle"


async def test_deleting_the_account_removes_the_boost(app_pool, svc):
    await make_user(app_pool, U)
    await give_tier(app_pool, U, "pro")
    await svc.boost.activate(U)
    await svc.account.delete_account(U)
    assert await app_pool.fetchval("SELECT count(*) FROM profile_boosts") == 0


# ---------------------------------------------------------------- browse ranking

V = 7100


async def candidates(pool):
    await make_user(pool, V)
    for uid in (1, 2, 3):
        await make_user(pool, uid)
        await pool.execute(
            "UPDATE users SET last_seen_at = now() - make_interval(mins => $2) WHERE id = $1",
            uid,
            uid,  # user 1 is the most recently active
        )


async def boost(pool, svc, uid, tier=None):
    """Boost `uid`. Without a tier only the boost entitlement is granted, so the user keeps the
    Free ranking level and any ordering change is due to the boost alone."""
    if tier:
        await give_tier(pool, uid, tier)
    else:
        await svc.entitlements.grant(uid, "profile_boost", {"enabled": True})
    assert (await svc.boost.activate(uid)).kind == "activated"


async def test_an_active_boost_raises_a_profile_to_the_top(app_pool, svc):
    await candidates(app_pool)
    assert await first_card(svc, V) == "Name1"
    await boost(app_pool, svc, 3)
    assert await first_card(svc, V) == "Name3"


async def test_an_expired_boost_has_no_effect(app_pool, svc):
    await candidates(app_pool)
    await boost(app_pool, svc, 3)
    await expire(app_pool, 3)
    assert await first_card(svc, V) == "Name1"


async def test_an_active_boost_beats_a_higher_subscription_tier(app_pool, svc):
    """boost > subscription priority > recency."""
    await candidates(app_pool)
    await give_tier(app_pool, 1, "vip")  # priority 3, also the most recent
    await give_tier(app_pool, 2, "premium")
    assert await first_card(svc, V) == "Name1"
    await boost(app_pool, svc, 3, tier="pro")  # pro priority is 2, below VIP, but boosted
    assert await first_card(svc, V) == "Name3"


async def test_a_boost_only_reorders_the_viewer_still_sees_everyone(app_pool, svc):
    """No starvation: boosted users are shown first, never instead of the others."""
    await candidates(app_pool)
    await boost(app_pool, svc, 3)
    seen = []
    for _ in range(3):
        res = await svc.browse.next_card(V)
        seen.append(res.card.name)
        await svc.browse.swipe(V, res.card.public_id, "pass")
    assert seen[0] == "Name3" and sorted(seen) == ["Name1", "Name2", "Name3"]


async def test_people_who_liked_the_viewer_still_come_first(app_pool, svc):
    await candidates(app_pool)
    await boost(app_pool, svc, 3)
    await app_pool.execute(
        "INSERT INTO swipes (swiper_id, target_id, action) VALUES (2, $1, 'like')", V
    )
    assert await first_card(svc, V) == "Name2"


async def test_a_boost_does_not_bypass_blocks_bans_or_deletion(app_pool, svc):
    await candidates(app_pool)
    for uid in (1, 2, 3):
        await boost(app_pool, svc, uid)
    await app_pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES ($1, 1)", V)
    await app_pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (2, $1)", V)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = 3")
    assert await first_card(svc, V) is None
    await app_pool.execute("UPDATE users SET status = 'deleted' WHERE id = 3")
    assert await first_card(svc, V) is None


async def test_a_boost_does_not_bypass_privacy_or_leak_fields(app_pool, svc):
    await candidates(app_pool)
    await app_pool.execute(
        "UPDATE profiles SET age = 51, city = 'Hiddenville', show_age = false, show_city = false "
        "WHERE user_id = 3"
    )
    await boost(app_pool, svc, 3)
    res = await svc.browse.next_card(V)
    assert res.card.name == "Name3" and res.card.age is None and res.card.city is None
    assert "age=51" not in repr(res) and "Hiddenville" not in repr(res)


async def test_a_boost_does_not_defeat_advanced_filters(app_pool, svc):
    await candidates(app_pool)
    await give_tier(app_pool, V, "pro")
    await app_pool.execute("UPDATE profiles SET gender = 'f' WHERE user_id IN (1, 2)")
    await boost(app_pool, svc, 3)  # a boosted man
    await svc.filters.set_gender(V, "f")
    assert await first_card(svc, V) in ("Name1", "Name2")


# ---------------------------------------------------------------- random-chat queue ranking

N = 7200


async def waiters(pool, *uids):
    """Users already waiting who can NOT pair with each other (blocked): only a newcomer picks."""
    for uid in uids:
        await make_user(pool, uid)
        await pool.execute("INSERT INTO chat_queue (user_id) VALUES ($1)", uid)
        await asyncio.sleep(0.01)
    for a in uids:
        for b in uids:
            if a < b:
                await pool.execute(
                    "INSERT INTO blocks (blocker_id, blocked_id) VALUES ($1, $2)", a, b
                )
    await make_user(pool, N)


async def partner_of_newcomer(svc):
    out = await svc.matchmaking.search(N)
    return out.partner.user_id if out.partner else None


async def test_a_boosted_waiter_is_served_before_an_earlier_unboosted_one(app_pool, svc):
    await waiters(app_pool, 1, 2)
    await boost(app_pool, svc, 2)
    assert await partner_of_newcomer(svc) == 2


async def test_an_expired_boost_changes_nothing_in_the_queue(app_pool, svc):
    await waiters(app_pool, 1, 2)
    await boost(app_pool, svc, 2)
    await expire(app_pool, 2)
    assert await partner_of_newcomer(svc) == 1


async def test_boost_outranks_tier_priority_in_the_queue(app_pool, svc):
    await waiters(app_pool, 1, 2)
    await app_pool.execute("UPDATE chat_queue SET priority = 3 WHERE user_id = 1")  # a VIP waiter
    await boost(app_pool, svc, 2)  # a boosted Free waiter
    assert await partner_of_newcomer(svc) == 2


async def test_tier_priority_still_orders_unboosted_waiters(app_pool, svc):
    await waiters(app_pool, 1, 2)
    await app_pool.execute("UPDATE chat_queue SET priority = 2 WHERE user_id = 2")
    assert await partner_of_newcomer(svc) == 2


async def test_a_long_waiting_user_is_not_starved_by_a_boosted_one(app_pool, svc):
    await waiters(app_pool, 1, 2)
    await boost(app_pool, svc, 2)
    await app_pool.execute(
        "UPDATE chat_queue SET joined_at = now() - interval '10 minutes' WHERE user_id = 1"
    )
    assert await partner_of_newcomer(svc) == 1


async def test_a_boost_cannot_pair_a_banned_or_blocked_waiter(app_pool, svc):
    await waiters(app_pool, 1, 2)
    await boost(app_pool, svc, 2)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = 2")
    assert await partner_of_newcomer(svc) == 1

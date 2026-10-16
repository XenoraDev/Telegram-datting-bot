"""P4 entitlements: free defaults, grants, subscription state, atomic daily usage limits."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from bot.services.entitlements import (
    EntitlementError,
    EntitlementService,
    merge,
    normalize_value,
)
from tests.factories import make_user


@pytest.fixture
def ent(app_pool, clock):
    return EntitlementService(app_pool, clock)


async def add_feature(pool, key, value, plan="free"):
    """Define a (test) feature on a plan."""
    await pool.execute(
        "INSERT INTO plan_entitlements (plan_id, feature_key, value) "
        "SELECT id, $2, $3::jsonb FROM plans WHERE code = $1 "
        "ON CONFLICT (plan_id, feature_key) DO UPDATE SET value = EXCLUDED.value",
        plan,
        key,
        value,
    )


async def make_plan(pool, code, interval="month"):
    return await pool.fetchval(
        "INSERT INTO plans (code, name, billing_interval) VALUES ($1, $1, $2) RETURNING id",
        code,
        interval,
    )


# ---------------- pure rules ----------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ({}, (True, None)),
        ({"enabled": True}, (True, None)),
        ({"enabled": False}, (False, None)),
        ({"daily_limit": 5}, (True, 5)),
        ({"enabled": True, "daily_limit": 0}, (True, 0)),
        ({"daily_limit": None}, (True, None)),
    ],
)
def test_normalize_value_accepts_valid_objects(value, expected):
    assert normalize_value(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, [], "x", 5, {"daily_limit": -1}, {"daily_limit": 1.5}, {"daily_limit": "3"},
     {"daily_limit": True}, {"enabled": "yes"}, {"price": 1}, {"daily_limit": 10**7}],
)  # fmt: skip
def test_normalize_value_rejects_everything_else(value):
    with pytest.raises(EntitlementError):
        normalize_value(value)


def test_merge_is_most_generous_wins():
    assert merge("f", []).enabled is False
    assert merge("f", [("free", '{"daily_limit": 3}')]).daily_limit == 3
    both = merge("f", [("free", '{"daily_limit": 3}'), ("grant", '{"daily_limit": 10}')])
    assert (both.enabled, both.daily_limit, both.source) == (True, 10, "grant")
    unlimited = merge("f", [("free", '{"daily_limit": 3}'), ("subscription", '{"enabled": true}')])
    assert unlimited.daily_limit is None and unlimited.source == "subscription"
    assert merge("f", [("free", '{"enabled": false}')]).enabled is False
    on = merge("f", [("free", '{"enabled": false}'), ("grant", '{"enabled": true}')])
    assert on.enabled is True and on.source == "grant"
    assert merge("f", [("free", "not json"), ("free", '{"daily_limit": -3}')]).enabled is False


# ---------------- free defaults ----------------


async def test_free_plan_has_the_documented_daily_limits(app_pool, ent):
    await make_user(app_pool, 1)
    for feature, limit in (("random_chat", 100), ("browse", 100), ("swipe", 50)):
        e = await ent.get(1, feature)
        assert (e.enabled, e.daily_limit, e.source) == (True, limit, "free")
        assert await ent.can_use(1, feature)


async def test_unknown_feature_is_disabled_and_cannot_be_consumed(app_pool, ent):
    await make_user(app_pool, 1)
    e = await ent.get(1, "teleportation")
    assert (e.enabled, e.source) == (False, "none")
    assert not await ent.can_use(1, "teleportation")
    res = await ent.consume(1, "teleportation")
    assert (res.allowed, res.reason) == (False, "disabled")
    assert await app_pool.fetchval("SELECT count(*) FROM usage_counters") == 0


@pytest.mark.parametrize("bad", ["", "A", "x y", "x;DROP", "1abc", "é", "x" * 49, "a-b"])
async def test_invalid_feature_keys_are_rejected(app_pool, ent, bad):
    with pytest.raises(EntitlementError):
        await ent.get(1, bad)
    with pytest.raises(EntitlementError):
        await ent.consume(1, bad)


# ---------------- grants ----------------


async def test_grant_enables_a_feature_only_for_that_user(app_pool, ent):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await ent.grant(1, "super_like", {"enabled": True, "daily_limit": 3}, source="promo")
    e = await ent.get(1, "super_like")
    assert (e.enabled, e.daily_limit, e.source) == (True, 3, "grant")
    assert (await ent.get(2, "super_like")).enabled is False


async def test_grant_raises_a_free_limit_but_never_lowers_it(app_pool, ent):
    await make_user(app_pool, 1)
    await add_feature(app_pool, "likes", '{"daily_limit": 5}')
    await ent.grant(1, "likes", {"daily_limit": 20})
    assert (await ent.get(1, "likes")).daily_limit == 20
    await ent.grant(1, "likes", {"daily_limit": 2})
    assert (await ent.get(1, "likes")).daily_limit == 20


async def test_unlimited_grant_beats_a_numeric_limit(app_pool, ent):
    await make_user(app_pool, 1)
    await add_feature(app_pool, "likes", '{"daily_limit": 5}')
    await ent.grant(1, "likes", {"enabled": True})
    assert (await ent.get(1, "likes")).daily_limit is None


async def test_expired_and_revoked_grants_do_not_apply(app_pool, ent):
    await make_user(app_pool, 1)
    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=1)
    await ent.grant(1, "boost", {"enabled": True}, source="boost", expires_at=past)
    assert (await ent.get(1, "boost")).enabled is False
    gid = await ent.grant(1, "boost", {"enabled": True}, source="boost", expires_at=future)
    assert (await ent.get(1, "boost")).enabled is True
    assert await ent.revoke_grant(gid) is True
    assert await ent.revoke_grant(gid) is False
    assert (await ent.get(1, "boost")).enabled is False


async def test_grant_validates_its_input(app_pool, ent):
    await make_user(app_pool, 1)
    with pytest.raises(EntitlementError):
        await ent.grant(1, "boost", {"daily_limit": -1})
    with pytest.raises(EntitlementError):
        await ent.grant(1, "boost", {"enabled": True}, source="hacker")
    with pytest.raises(EntitlementError):
        await ent.grant(1, "Bad Key", {"enabled": True})
    assert await app_pool.fetchval("SELECT count(*) FROM entitlement_grants") == 0


# ---------------- subscription state ----------------


async def test_active_subscription_applies_its_plan_entitlements(app_pool, ent):
    await make_user(app_pool, 1)
    await make_plan(app_pool, "plus")
    await add_feature(app_pool, "likes", '{"daily_limit": 5}')
    await add_feature(app_pool, "likes", '{"daily_limit": 50}', plan="plus")
    await add_feature(app_pool, "see_likes", '{"enabled": true}', plan="plus")
    assert (await ent.get(1, "see_likes")).enabled is False
    sub = await ent.start_subscription(1, "plus", period_end=datetime.now(UTC) + timedelta(days=30))
    assert (await ent.get(1, "likes")).daily_limit == 50
    e = await ent.get(1, "see_likes")
    assert (e.enabled, e.source) == (True, "subscription")
    assert (
        await app_pool.fetchval("SELECT status FROM subscriptions WHERE id = $1", sub) == "active"
    )


@pytest.mark.parametrize("status", ["past_due", "canceled", "expired"])
async def test_inactive_subscription_states_give_nothing(app_pool, ent, status):
    await make_user(app_pool, 1)
    await make_plan(app_pool, "plus")
    await add_feature(app_pool, "see_likes", '{"enabled": true}', plan="plus")
    sub = await ent.start_subscription(1, "plus")
    assert (await ent.get(1, "see_likes")).enabled is True
    assert await ent.set_subscription_status(sub, status) is True
    assert (await ent.get(1, "see_likes")).enabled is False


async def test_subscription_past_its_period_end_stops_applying(app_pool, ent):
    await make_user(app_pool, 1)
    await make_plan(app_pool, "plus")
    await add_feature(app_pool, "see_likes", '{"enabled": true}', plan="plus")
    await ent.start_subscription(1, "plus", period_end=datetime.now(UTC) - timedelta(minutes=1))
    assert (await ent.get(1, "see_likes")).enabled is False


async def test_subscription_input_is_validated(app_pool, ent):
    await make_user(app_pool, 1)
    with pytest.raises(EntitlementError):
        await ent.start_subscription(1, "nonexistent")
    await app_pool.execute("UPDATE plans SET is_active = false WHERE code = 'free'")
    with pytest.raises(EntitlementError):
        await ent.start_subscription(1, "free")
    sub = await app_pool.fetchval("SELECT 1")
    with pytest.raises(EntitlementError):
        await ent.set_subscription_status(sub, "gold")


async def test_an_inactive_plan_gives_no_entitlements(app_pool, ent):
    await make_user(app_pool, 1)
    await app_pool.execute("UPDATE plans SET is_active = false WHERE code = 'free'")
    assert (await ent.get(1, "random_chat")).enabled is False


# ---------------- usage counters / daily limits ----------------


async def test_unlimited_features_are_counted_but_never_blocked(app_pool, ent):
    await make_user(app_pool, 1)
    await add_feature(app_pool, "wave", '{"enabled": true}')
    for n in (1, 2, 3):
        res = await ent.consume(1, "wave")
        assert (res.allowed, res.used, res.remaining) == (True, n, None)
    assert await ent.used_today(1, "wave") == 3


async def test_daily_limit_is_enforced_and_reports_remaining(app_pool, ent):
    await make_user(app_pool, 1)
    await add_feature(app_pool, "likes", '{"daily_limit": 3}')
    results = [await ent.consume(1, "likes") for _ in range(5)]
    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert [r.remaining for r in results[:3]] == [2, 1, 0]
    assert results[3].reason == "limit_reached" and results[3].used == 3
    assert await ent.used_today(1, "likes") == 3  # denied calls are not counted
    assert not await ent.can_use(1, "likes")


async def test_limits_are_per_user_and_per_feature(app_pool, ent):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await add_feature(app_pool, "likes", '{"daily_limit": 1}')
    await add_feature(app_pool, "msgs", '{"daily_limit": 1}')
    assert (await ent.consume(1, "likes")).allowed
    assert not (await ent.consume(1, "likes")).allowed
    assert (await ent.consume(2, "likes")).allowed
    assert (await ent.consume(1, "msgs")).allowed


async def test_the_counter_resets_on_the_next_utc_day(app_pool, ent, clock):
    await make_user(app_pool, 1)
    await add_feature(app_pool, "likes", '{"daily_limit": 1}')
    assert (await ent.consume(1, "likes")).allowed
    assert not (await ent.consume(1, "likes")).allowed
    clock.advance(24 * 3600)
    assert await ent.can_use(1, "likes")
    assert (await ent.consume(1, "likes")).allowed
    assert await app_pool.fetchval("SELECT count(*) FROM usage_counters") == 2


async def test_zero_limit_blocks_and_oversized_amount_is_refused(app_pool, ent):
    await make_user(app_pool, 1)
    await add_feature(app_pool, "likes", '{"daily_limit": 0}')
    assert (await ent.consume(1, "likes")).allowed is False
    await add_feature(app_pool, "msgs", '{"daily_limit": 3}')
    assert (await ent.consume(1, "msgs", 4)).allowed is False
    assert (await ent.consume(1, "msgs", 3)).allowed is True
    assert (await ent.consume(1, "msgs")).allowed is False


@pytest.mark.parametrize("amount", [0, -1, 1.5, "1", None, True])
async def test_invalid_amounts_are_refused(app_pool, ent, amount):
    await make_user(app_pool, 1)
    res = await ent.consume(1, "swipe", amount)
    assert (res.allowed, res.reason) == (False, "invalid_amount")
    assert await app_pool.fetchval("SELECT count(*) FROM usage_counters") == 0


async def test_concurrent_consumption_can_never_exceed_the_limit(app_pool, ent):
    await make_user(app_pool, 1)
    await add_feature(app_pool, "likes", '{"daily_limit": 5}')
    results = await asyncio.gather(*[ent.consume(1, "likes") for _ in range(40)])
    assert sum(r.allowed for r in results) == 5
    assert await ent.used_today(1, "likes") == 5
    assert await app_pool.fetchval("SELECT count(*) FROM usage_counters") == 1


async def test_concurrent_first_use_of_the_day_creates_one_row(app_pool, ent):
    await make_user(app_pool, 1)
    await asyncio.gather(*[ent.consume(1, "swipe") for _ in range(25)])
    assert await ent.used_today(1, "swipe") == 25
    assert await app_pool.fetchval("SELECT count(*) FROM usage_counters") == 1


async def test_concurrent_mixed_amounts_stay_within_the_limit(app_pool, ent):
    await make_user(app_pool, 1)
    await add_feature(app_pool, "msgs", '{"daily_limit": 10}')
    results = await asyncio.gather(*[ent.consume(1, "msgs", 3) for _ in range(10)])
    assert sum(r.allowed for r in results) == 3
    assert await ent.used_today(1, "msgs") == 9


async def test_old_counters_are_purged_and_recent_ones_kept(app_pool, ent, clock):
    await make_user(app_pool, 1)
    await ent.consume(1, "swipe")
    clock.advance(40 * 24 * 3600)
    await ent.consume(1, "swipe")
    assert await ent.purge_old_usage() == 1
    assert await app_pool.fetchval("SELECT count(*) FROM usage_counters") == 1
    assert await ent.purge_old_usage() == 0


async def test_counters_hold_only_counts(app_pool, ent):
    await make_user(app_pool, 1)
    await ent.consume(1, "swipe")
    cols = {r["column_name"] for r in await app_pool.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'usage_counters'"
    )}  # fmt: skip
    assert cols == {"user_id", "feature_key", "period_start", "count"}


async def test_deleting_an_account_removes_its_usage_counters(app_pool, svc):
    await make_user(app_pool, 1)
    await svc.entitlements.consume(1, "swipe")
    await svc.account.delete_account(1)
    assert await app_pool.fetchval("SELECT count(*) FROM usage_counters") == 0

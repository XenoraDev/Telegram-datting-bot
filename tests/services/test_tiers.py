"""Tier entitlements: Free / Premium / Pro / VIP behaviour, levels, bulk lookup, no plan names."""

import re
from pathlib import Path

import pytest

from bot.services.entitlements import EntitlementService, merge, parse_value
from bot.tiers import FREE, TIERS
from tests.factories import give_tier, make_user

ROOT = Path(__file__).resolve().parents[2]
LIMITS = {
    None: {"random_chat": 100, "browse": 100, "swipe": 50},
    "premium": {"random_chat": 200, "browse": 200, "swipe": 200},
    "pro": {"random_chat": 500, "browse": 500, "swipe": 500},
    "vip": {"random_chat": None, "browse": None, "swipe": None},
}
UNLOCKS = {
    None: {"view_partner_age": False, "view_partner_gender": False, "view_partner_city": True},
    "premium": {"view_partner_age": True, "view_partner_gender": True, "view_partner_city": True},
    "pro": {"view_partner_age": True, "view_partner_gender": True, "view_partner_city": True},
    "vip": {"view_partner_age": True, "view_partner_gender": True, "view_partner_city": True},
}
CAPABILITIES = {
    None: (False, False, False),
    "premium": (False, False, False),
    "pro": (True, True, True),
    "vip": (True, True, True),
}
LEVEL = {None: 0, "premium": 1, "pro": 2, "vip": 3}


@pytest.fixture
def ent(app_pool, clock):
    return EntitlementService(app_pool, clock)


async def user_on(pool, uid, tier):
    await make_user(pool, uid)
    if tier:
        await give_tier(pool, uid, tier)
    return uid


@pytest.mark.parametrize("tier", [None, "premium", "pro", "vip"])
async def test_daily_limits_per_tier(app_pool, ent, tier):
    uid = await user_on(app_pool, 1, tier)
    for feature, limit in LIMITS[tier].items():
        e = await ent.get(uid, feature)
        assert (e.enabled, e.daily_limit) == (True, limit), (tier, feature)


@pytest.mark.parametrize("tier", [None, "premium", "pro", "vip"])
async def test_partner_detail_unlocks_per_tier(app_pool, ent, tier):
    uid = await user_on(app_pool, 1, tier)
    got = await ent.get_many(uid, list(UNLOCKS[tier]))
    assert {k: v.enabled for k, v in got.items()} == UNLOCKS[tier]


@pytest.mark.parametrize("tier", [None, "premium", "pro", "vip"])
async def test_capabilities_levels_and_badge_per_tier(app_pool, ent, tier):
    uid = await user_on(app_pool, 1, tier)
    got = await ent.get_many(uid, ["advanced_filters", "see_who_liked", "profile_boost"])
    assert (
        tuple(got[k].enabled for k in ("advanced_filters", "see_who_liked", "profile_boost"))
        == CAPABILITIES[tier]
    )
    assert (await ent.get(uid, "match_priority")).level == LEVEL[tier]
    badge = await ent.get(uid, "plan_badge")
    assert badge.level == LEVEL[tier] and badge.enabled == (tier is not None)


def test_every_tier_offers_at_least_what_the_one_below_does():
    order = [FREE, TIERS["premium"], TIERS["pro"], TIERS["vip"]]
    for lower, higher in zip(order, order[1:], strict=False):
        for feature, value in lower.items():
            assert feature in higher, feature
            low, high = parse_value(value), parse_value(higher[feature])
            assert high[0] >= low[0]
            assert high[1] is None or (low[1] is not None and high[1] >= low[1]), feature
            assert high[2] >= low[2], feature


def test_tier_templates_only_use_valid_entitlement_values():
    for template in (FREE, *TIERS.values()):
        for value in template.values():
            parse_value(value)


async def test_vip_is_unlimited_but_still_counted_and_never_blocked_by_the_daily_limit(
    app_pool, ent
):
    uid = await user_on(app_pool, 1, "vip")
    for n in range(1, 701):  # past even the Pro limit
        res = await ent.consume(uid, "swipe")
        assert res.allowed and res.used == n and res.remaining is None
    assert await ent.used_today(uid, "swipe") == 700


async def test_a_paid_limit_is_a_hard_ceiling_and_free_is_lower(app_pool, ent):
    free = await user_on(app_pool, 1, None)
    pro = await user_on(app_pool, 2, "pro")
    assert [(await ent.consume(free, "swipe")).allowed for _ in range(51)].count(True) == 50
    assert [(await ent.consume(pro, "swipe")).allowed for _ in range(501)].count(True) == 500


async def test_refund_gives_a_use_back_and_never_goes_below_zero(app_pool, ent):
    uid = await user_on(app_pool, 1, None)
    await ent.consume(uid, "swipe", 3)
    await ent.refund(uid, "swipe", 1)
    assert await ent.used_today(uid, "swipe") == 2
    await ent.refund(uid, "swipe", 50)
    assert await ent.used_today(uid, "swipe") == 0
    await ent.refund(uid, "swipe", 0)  # ignored


async def test_get_many_matches_get_and_handles_unknown_features(app_pool, ent):
    uid = await user_on(app_pool, 1, "pro")
    keys = ["random_chat", "match_priority", "view_partner_age", "teleportation"]
    many = await ent.get_many(uid, keys)
    for k in keys:
        assert many[k] == await ent.get(uid, k)
    assert many["teleportation"].enabled is False


def test_level_merges_by_taking_the_highest_enabled_source():
    rows = [
        ("free", {"enabled": True, "level": 0}),
        ("subscription", {"enabled": True, "level": 2}),
        ("grant", {"enabled": True, "level": 1}),
    ]
    assert merge("match_priority", rows).level == 2
    assert merge("match_priority", [("subscription", {"enabled": False, "level": 3})]).level == 0


@pytest.mark.parametrize(
    "bad", [{"level": -1}, {"level": 101}, {"level": True}, {"level": "2"}, {"lvl": 1}]
)
def test_invalid_levels_are_rejected(bad):
    with pytest.raises(Exception):  # noqa: B017 - EntitlementError, a ValueError subclass
        parse_value(bad)


async def test_a_subscription_that_has_not_started_yet_grants_nothing(app_pool, ent):
    uid = await user_on(app_pool, 1, None)
    sub = await give_tier(app_pool, uid, "vip")
    await app_pool.execute(
        "UPDATE subscriptions SET started_at = now() + interval '1 day' WHERE id=$1", sub
    )
    assert (await ent.get(uid, "view_partner_age")).enabled is False
    await app_pool.execute(
        "UPDATE subscriptions SET started_at = now() - interval '1 minute' WHERE id=$1", sub
    )
    assert (await ent.get(uid, "view_partner_age")).enabled is True


async def test_an_expired_subscription_falls_back_to_the_free_tier(app_pool, ent):
    uid = await user_on(app_pool, 1, "pro")
    assert (await ent.get(uid, "swipe")).daily_limit == 500
    await app_pool.execute(
        "UPDATE subscriptions SET current_period_end = now() - interval '1 second'"
    )
    assert (await ent.get(uid, "swipe")).daily_limit == 50
    assert (await ent.get(uid, "view_partner_age")).enabled is False


def test_no_code_branches_on_a_plan_or_tier_name():
    """Behaviour comes from entitlement data; only tier templates / the plan form know names."""
    allowed = {"tiers.py", "payments.py", "repo_payments.py", "en.py", "hi.py"}
    pattern = re.compile(r"""(==|!=|\bin\b)\s*\(?\s*["'](premium|pro|vip)["']""")
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "bot").rglob("*.py")
        if p.name not in allowed and pattern.search(p.read_text())
    ]
    assert offenders == []


def test_every_advertised_feature_is_used_by_real_code():
    """No entitlement may be a database flag only: each key of every tier must be asked for by
    code outside the tier table itself (a service, handler or repository)."""
    from pathlib import Path

    from bot.tiers import FREE, TIERS

    sources = {
        p: p.read_text()
        for p in (Path(__file__).resolve().parents[2] / "bot").rglob("*.py")
        if p.name not in ("tiers.py", "en.py", "hi.py")
    }
    keys = set(FREE) | {k for tier in TIERS.values() for k in tier}
    unused = sorted(
        k for k in keys if not any(f'"{k}"' in s or f"'{k}'" in s for s in sources.values())
    )
    assert unused == [], unused

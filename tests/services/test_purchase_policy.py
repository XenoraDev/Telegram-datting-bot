"""Who may buy which plan: a lower tier is refused while a higher paid tier is running."""

import inspect

import pytest

from bot.db import repo_payments
from bot.services import build_services
from bot.services import payments as payments_module
from tests.factories import enable_payments, give_tier, make_plan, make_user

U, ADMIN = 8300, 424242
TIERS = ("premium", "pro", "vip")


@pytest.fixture
async def shop(app_pool, clock):
    await enable_payments(app_pool)
    await make_user(app_pool, U)
    svc = build_services(app_pool, clock, clock, frozenset({ADMIN}))
    plans = {
        tier: await make_plan(app_pool, f"buy_{tier}", name=f"Buy {tier}", tier=tier)
        for tier in TIERS
    }
    return svc, plans


async def buy(shop, tier):
    svc, plans = shop
    return await svc.payments.start(U, plans[tier])


@pytest.mark.parametrize(
    "owned,wanted",
    [("vip", "premium"), ("vip", "pro"), ("pro", "premium")],
)
async def test_a_lower_tier_is_refused_while_a_higher_one_is_running(app_pool, shop, owned, wanted):
    await give_tier(app_pool, U, owned)
    res = await buy(shop, wanted)
    assert res.kind == "lower_tier" and res.order is None
    assert await app_pool.fetchval("SELECT count(*) FROM payments") == 0  # no order was opened


@pytest.mark.parametrize("tier", TIERS)
async def test_renewing_the_same_tier_is_allowed(app_pool, shop, tier):
    await give_tier(app_pool, U, tier)
    assert (await buy(shop, tier)).kind == "ok"


@pytest.mark.parametrize("owned,wanted", [("premium", "pro"), ("premium", "vip"), ("pro", "vip")])
async def test_an_upgrade_is_allowed(app_pool, shop, owned, wanted):
    await give_tier(app_pool, U, owned)
    assert (await buy(shop, wanted)).kind == "ok"


@pytest.mark.parametrize("tier", TIERS)
async def test_a_user_with_no_plan_can_buy_any_paid_plan(shop, tier):
    assert (await buy(shop, tier)).kind == "ok"


async def test_a_free_user_whose_plan_expired_is_not_blocked(app_pool, shop):
    sub = await give_tier(app_pool, U, "vip")
    await app_pool.execute(
        "UPDATE subscriptions SET started_at = now() - interval '40 days', "
        "current_period_end = now() - interval '10 days' WHERE id = $1",
        sub,
    )
    assert (await buy(shop, "premium")).kind == "ok"


async def test_a_higher_tier_that_has_not_started_yet_does_not_block(app_pool, shop):
    sub = await give_tier(app_pool, U, "vip")
    await app_pool.execute(
        "UPDATE subscriptions SET started_at = now() + interval '5 days', "
        "current_period_end = now() + interval '35 days' WHERE id = $1",
        sub,
    )
    assert (await buy(shop, "premium")).kind == "ok"


async def test_a_custom_plan_without_a_tier_counts_as_the_lowest_level(app_pool, shop):
    svc, _ = shop
    custom = await app_pool.fetchval(
        "INSERT INTO plans (code, name, is_active, duration_days, price_minor) "
        "VALUES ('custom', 'Custom', true, 30, 5000) RETURNING id"
    )
    await give_tier(app_pool, U, "premium")
    assert (await svc.payments.start(U, custom)).kind == "lower_tier"
    await app_pool.execute(
        "UPDATE subscriptions SET current_period_end = now() - interval '1 s',"
        " started_at = now() - interval '2 days'"
    )
    assert (await svc.payments.start(U, custom)).kind == "ok"


async def test_the_order_comes_from_entitlement_levels_not_plan_names(app_pool, shop):
    svc, plans = shop
    # A plan NAMED "VIP" that carries Premium entitlements is a premium-level plan.
    await app_pool.execute("UPDATE plans SET name = 'VIP Gold' WHERE id = $1", plans["premium"])
    await give_tier(app_pool, U, "pro")
    assert (await svc.payments.start(U, plans["premium"])).kind == "lower_tier"
    assert (await svc.payments.start(U, plans["vip"])).kind == "ok"


def test_there_are_no_hardcoded_plan_names_in_the_policy():
    source = inspect.getsource(payments_module.PaymentService.start) + inspect.getsource(
        repo_payments.running_levels
    )
    for name in ("premium", "pro", "vip", "'free'"):
        assert name not in source.lower().replace("premium.", "")


async def test_existing_queued_subscriptions_and_payments_are_untouched(app_pool, shop):
    await give_tier(app_pool, U, "vip")
    queued = await give_tier(app_pool, U, "premium", days=60)
    await app_pool.execute(
        "UPDATE subscriptions SET started_at = now() + interval '30 days' WHERE id = $1", queued
    )
    before = [dict(r) for r in await app_pool.fetch("SELECT * FROM subscriptions ORDER BY id")]
    assert (await buy(shop, "premium")).kind == "lower_tier"
    assert [
        dict(r) for r in await app_pool.fetch("SELECT * FROM subscriptions ORDER BY id")
    ] == before

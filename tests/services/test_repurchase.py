"""Buying again while subscribed: extend, upgrade, queue; never two overlapping subscriptions."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from bot.services import build_services
from bot.services.payments import LiveSub, Purchase, decide_purchase
from tests import payment_images as img
from tests.factories import enable_payments, make_plan, make_user

ADMIN = 9000001
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
SECOND = timedelta(seconds=30)


@pytest.fixture(autouse=True)
def orders_from_before_the_purchase_policy(monkeypatch):
    """`start()` now refuses a lower-tier plan while a higher tier runs (test_purchase_policy.py).
    These tests are about what APPROVAL does with such an order, which still exists for orders
    opened earlier (before the higher plan started, or before the policy), so the guard is off."""

    async def nothing_running(db, uid):
        return []

    monkeypatch.setattr("bot.db.repo_payments.running_levels", nothing_running)


@pytest.fixture
def psvc(app_pool, clock):
    return build_services(app_pool, clock, clock, frozenset({ADMIN}))


@pytest.fixture
async def shop(app_pool):
    """Premium 30d (Rs 99), Premium 90d, Pro 30d, VIP 30d; users 1 and 2."""
    await enable_payments(app_pool)
    plans = {
        "p30": await make_plan(app_pool, "premium_30", price_minor=9900),
        "p90": await make_plan(
            app_pool, "premium_90", name="Premium 90", days=90, price_minor=24900
        ),
        "pro": await make_plan(app_pool, "pro_30", name="Pro", price_minor=19900, tier="pro"),
        "vip": await make_plan(app_pool, "vip_30", name="VIP", price_minor=49900, tier="vip"),
    }
    for uid in (1, 2):
        await make_user(app_pool, uid)
    return plans


async def buy(psvc, uid, plan):
    """Order, upload, submit and have an admin approve; returns the approval result."""
    order = (await psvc.payments.start(uid, plan)).order
    await psvc.payments.attach_evidence(uid, order.payment_id, img.JPEG)
    assert (await psvc.payments.submit(uid, order.payment_id, None)).kind == "ok"
    res = await psvc.payments.approve(ADMIN, order.payment_id)
    assert res.kind == "ok", res
    return res


async def subs(pool, uid=1):
    return [
        dict(r)
        for r in await pool.fetch("SELECT * FROM subscriptions WHERE user_id=$1 ORDER BY id", uid)
    ]


async def effective_now(pool, uid=1):
    return await pool.fetchval(
        "SELECT count(*) FROM subscriptions WHERE user_id=$1 AND status='active' "
        "AND started_at <= now() AND current_period_end > now()",
        uid,
    )


# ------------------------------------------------------------------ the pure policy


def live(*rows):
    return [LiveSub(i + 1, plan, level, ends) for i, (plan, level, ends) in enumerate(rows)]


def priced(sid, plan, level, ends, price=9900, days=30, starts=None):
    """A live subscription of a plan with a configured price and length."""
    return LiveSub(sid, plan, level, ends, starts, price, days)


def upgrade(rows, price=29900, days=30):
    return decide_purchase(rows, 9, 3, NOW, price, days)


def test_policy_nothing_running_starts_fresh():
    assert decide_purchase([], 1, 1) == Purchase("started")


def test_policy_same_plan_extends_the_one_that_ends_last():
    rows = live((1, 1, NOW + timedelta(days=3)), (1, 1, NOW + timedelta(days=9)))
    assert decide_purchase(rows, 1, 1) == Purchase("extended", extend_id=2)


def test_policy_extending_pushes_back_what_is_queued_behind_it():
    now = NOW
    rows = [
        LiveSub(1, 9, 3, now + timedelta(days=10), now - timedelta(days=5)),
        LiveSub(2, 8, 1, now + timedelta(days=40), now + timedelta(days=10)),
        LiveSub(3, 7, 0, now + timedelta(days=70), now + timedelta(days=40)),
    ]
    p = decide_purchase(rows, 9, 3)
    assert (p.mode, p.extend_id, p.shift_ids) == ("extended", 1, (2, 3))
    # extending the LAST one in the chain moves nothing else
    assert decide_purchase(rows, 7, 0).shift_ids == ()


def test_policy_another_plan_of_the_same_tier_extends_too():
    rows = live((1, 1, NOW + timedelta(days=3)))
    assert decide_purchase(rows, 7, 1) == Purchase("extended", extend_id=1)


def test_policy_a_higher_tier_replaces_now_and_converts_the_time_by_plan_price():
    rows = [
        priced(1, 1, 1, NOW + timedelta(days=3)),
        priced(2, 1, 1, NOW + timedelta(days=9)),
    ]
    p = decide_purchase(rows, 5, 2, NOW, 19900, 30)
    # (3 + 9 days) x 99 / 199 = 5.97 -> 5 whole days; nothing is copied day for day
    assert (p.mode, p.end_ids, p.carry_days, p.start, p.end_base) == (
        "upgraded",
        (1, 2),
        5,
        None,
        None,
    )


def test_upgrade_converts_by_configured_price_99_to_299():
    rows = [priced(1, 1, 1, NOW + timedelta(days=30))]
    assert upgrade(rows).carry_days == 9  # 30 x 99/299 = 9.93 -> 9, so VIP = 30 + 9 days


def test_upgrade_ignores_what_was_actually_paid():
    # the plan is Rs 99; a Rs 1 test payment changes nothing about the conversion
    rows = [priced(1, 1, 1, NOW + timedelta(days=30), price=9900)]
    assert upgrade(rows).carry_days == 9 != 30


def test_upgrade_counts_only_the_time_that_is_left():
    rows = [priced(1, 1, 1, NOW + timedelta(days=15))]  # half used
    assert upgrade(rows).carry_days == 4  # 15 x 99/299 = 4.97 -> 4


def test_upgrade_sums_the_value_of_several_live_subscriptions_before_rounding():
    rows = [priced(1, 1, 1, NOW + timedelta(days=10)), priced(2, 2, 1, NOW + timedelta(days=20))]
    assert upgrade(rows).carry_days == 9  # 30 x 99/299 = 9.93
    rows = [priced(1, 1, 1, NOW + timedelta(days=5)), priced(2, 2, 1, NOW + timedelta(days=5))] * 3
    assert upgrade(rows).carry_days == 9  # 6 x 5 days = 30 days of value, not 6 x floor(1.65)


def test_upgrade_floors_and_never_rounds_a_fraction_up():
    almost = [priced(1, 1, 1, NOW + timedelta(days=29, hours=23))]  # 9.9 days of value
    assert upgrade(almost).carry_days == 9
    exact = [priced(1, 1, 1, NOW + timedelta(days=299), price=29900, days=299)]
    assert (
        upgrade(exact, price=29900, days=30).carry_days == 30
    )  # 299 days at Rs 100/day = Rs 29900 = exactly one Rs 299 month
    one_second_short = [priced(1, 1, 1, NOW + timedelta(days=30) - timedelta(seconds=1), 29900)]
    assert upgrade(one_second_short).carry_days == 29  # 30 x 1 -> 29.99999 floors to 29


def test_upgrade_without_prices_carries_nothing():
    end = NOW + timedelta(days=30)
    assert upgrade([priced(1, 1, 1, end, price=None, days=None)]).carry_days == 0  # custom plan
    assert upgrade([LiveSub(1, 1, 1, end)]).carry_days == 0  # no price info at all
    assert upgrade([priced(1, 1, 1, end)], price=None, days=None).carry_days == 0  # unpriced new
    assert upgrade([priced(1, 1, 1, end)], price=0).carry_days == 0
    mixed = [priced(1, 1, 1, end, price=None, days=None), priced(2, 2, 1, end)]
    assert upgrade(mixed).carry_days == 9  # only the priced one counts


def test_upgrade_counts_a_queued_subscription_in_full_and_ignores_finished_time():
    running = priced(1, 1, 1, NOW + timedelta(days=10))
    queued = priced(2, 2, 1, NOW + timedelta(days=40), starts=NOW + timedelta(days=10))
    assert upgrade([running, queued]).carry_days == 13  # 40 x 99/299 = 13.2 -> 13
    ended = priced(3, 3, 1, NOW - timedelta(days=1))
    assert upgrade([ended]).carry_days == 0


def test_same_tier_and_lower_tier_decisions_do_not_use_prices():
    rows = [priced(1, 1, 1, NOW + timedelta(days=3))]
    assert decide_purchase(rows, 1, 1, NOW, 9900, 30) == Purchase("extended", extend_id=1)
    low = [priced(5, 3, 3, NOW + timedelta(days=6), price=29900)]
    assert decide_purchase(low, 1, 1, NOW, 9900, 30) == Purchase(
        "queued", start=NOW + timedelta(days=6), end_base=NOW + timedelta(days=6)
    )


def test_policy_a_lower_tier_waits_for_the_current_time_to_run_out():
    rows = live((5, 3, NOW + timedelta(days=6)))
    assert decide_purchase(rows, 1, 1) == Purchase(
        "queued", start=NOW + timedelta(days=6), end_base=NOW + timedelta(days=6)
    )


def test_policy_a_custom_untiered_plan_behaves_like_any_other_level():
    rows = live((1, 0, NOW + timedelta(days=2)))
    assert decide_purchase(rows, 2, 0).mode == "extended"
    assert decide_purchase(rows, 2, 1).mode == "upgraded"


# ------------------------------------------------------------------ same plan again


async def test_buying_the_same_plan_again_extends_from_the_current_expiry_not_from_today(
    app_pool, psvc, shop
):
    await buy(psvc, 1, shop["p30"])
    # the example from the brief: current expiry in 4 days, buy another 30 days
    await app_pool.execute(
        "UPDATE subscriptions SET current_period_end = now() + interval '4 days'"
    )
    expiry = await app_pool.fetchval("SELECT current_period_end FROM subscriptions")
    res = await buy(psvc, 1, shop["p30"])
    rows = await subs(app_pool)
    assert len(rows) == 1 and res.mode == "extended"
    assert rows[0]["current_period_end"] == expiry + timedelta(days=30)  # NOT now() + 30d
    days = (rows[0]["current_period_end"] - datetime.now(UTC)).days
    assert days in (33, 34)
    assert await effective_now(app_pool) == 1


async def test_the_second_payment_points_at_the_same_subscription(app_pool, psvc, shop):
    await buy(psvc, 1, shop["p30"])
    await buy(psvc, 1, shop["p30"])
    ids = {
        r["subscription_id"] for r in await app_pool.fetch("SELECT subscription_id FROM payments")
    }
    assert len(ids) == 1


async def test_an_expired_subscription_starts_a_new_one_from_now(app_pool, psvc, shop):
    await buy(psvc, 1, shop["p30"])
    await app_pool.execute(
        "UPDATE subscriptions SET current_period_end = now() - interval '2 days'"
    )
    res = await buy(psvc, 1, shop["p30"])
    rows = await subs(app_pool)
    assert len(rows) == 2 and res.mode == "started"
    assert rows[1]["status"] == "active" and rows[1]["current_period_end"] > datetime.now(
        UTC
    ) + timedelta(days=29)
    assert await effective_now(app_pool) == 1  # the expired one does not count


async def test_a_longer_plan_of_the_same_tier_extends_the_running_subscription(
    app_pool, psvc, shop
):
    await buy(psvc, 1, shop["p30"])
    before = (await subs(app_pool))[0]["current_period_end"]
    res = await buy(psvc, 1, shop["p90"])
    rows = await subs(app_pool)
    assert len(rows) == 1 and res.mode == "extended" and rows[0]["plan_id"] == shop["p30"]
    assert rows[0]["current_period_end"] == before + timedelta(
        days=90
    )  # the days that were paid for


# ------------------------------------------------------------------ different tier


async def test_upgrading_takes_effect_now_and_converts_the_time_already_paid_for(
    app_pool, psvc, shop
):
    await buy(psvc, 1, shop["p30"])
    res = await buy(psvc, 1, shop["pro"])
    old, new = await subs(app_pool)
    assert res.mode == "upgraded"
    assert old["status"] == "canceled" and old["current_period_end"] <= datetime.now(UTC)
    assert new["status"] == "active" and new["plan_id"] == shop["pro"]
    # 30 days of Rs 99 are worth 14 days of Rs 199 (14.9 floored): 30 + 14, not 30 + 30
    assert abs(new["current_period_end"] - (datetime.now(UTC) + timedelta(days=44))) < SECOND
    assert await effective_now(app_pool) == 1
    assert (await psvc.entitlements.get(1, "see_who_liked")).enabled is True  # Pro right away
    assert (await psvc.entitlements.get(1, "swipe")).daily_limit == 500


async def test_buying_a_lower_tier_is_queued_behind_the_current_one(app_pool, psvc, shop):
    await buy(psvc, 1, shop["vip"])
    vip_end = (await subs(app_pool))[0]["current_period_end"]
    res = await buy(psvc, 1, shop["p30"])
    vip, queued = await subs(app_pool)
    assert res.mode == "queued" and res.starts_at == vip_end
    assert vip["status"] == "active" and vip["current_period_end"] == vip_end  # untouched
    assert queued["started_at"] == vip_end and queued["current_period_end"] == vip_end + timedelta(
        days=30
    )
    assert await effective_now(app_pool) == 1
    assert (await psvc.entitlements.get(1, "swipe")).daily_limit is None  # still VIP today
    assert (await psvc.payments.overview(1)).premium_until == queued["current_period_end"]


async def test_the_queued_plan_takes_over_when_the_higher_one_ends(app_pool, psvc, shop):
    await buy(psvc, 1, shop["pro"])
    await buy(psvc, 1, shop["p30"])
    # time passes: the Pro subscription ran out, the queued Premium has started
    await app_pool.execute(
        "UPDATE subscriptions SET current_period_end = now() - interval '1 hour' "
        "WHERE plan_id = $1",
        shop["pro"],
    )
    await app_pool.execute(
        "UPDATE subscriptions SET started_at = now() - interval '1 hour' WHERE plan_id = $1",
        shop["p30"],
    )
    assert await effective_now(app_pool) == 1
    assert (await psvc.entitlements.get(1, "swipe")).daily_limit == 200
    assert (await psvc.entitlements.get(1, "see_who_liked")).enabled is False


async def test_buying_the_queued_plan_again_extends_the_queued_one(app_pool, psvc, shop):
    await buy(psvc, 1, shop["vip"])
    await buy(psvc, 1, shop["p30"])
    res = await buy(psvc, 1, shop["p30"])
    rows = await subs(app_pool)
    assert res.mode == "extended" and len(rows) == 2
    assert rows[1]["current_period_end"] - rows[1]["started_at"] == timedelta(days=60)


async def test_a_subscription_that_has_not_started_gives_nothing(app_pool, psvc, shop):
    await buy(psvc, 1, shop["vip"])
    await buy(psvc, 1, shop["p30"])
    # remove the VIP time: the queued Premium is still in the future, so nothing is active
    await app_pool.execute(
        "UPDATE subscriptions SET status = 'expired' WHERE plan_id = $1", shop["vip"]
    )
    assert await effective_now(app_pool) == 0
    assert (await psvc.entitlements.get(1, "view_partner_age")).enabled is False


async def test_upgrade_cancels_queued_subscriptions_too(app_pool, psvc, shop):
    await buy(psvc, 1, shop["pro"])
    await buy(psvc, 1, shop["p30"])  # queued
    res = await buy(psvc, 1, shop["vip"])  # higher than everything
    rows = await subs(app_pool)
    assert res.mode == "upgraded"
    assert [r["status"] for r in rows] == ["canceled", "canceled", "active"]
    assert await effective_now(app_pool) == 1
    # 30d of Rs 199 + 30d of Rs 99 (queued, counted in full) = Rs 8940 / Rs 499 per 30d: 17 days
    assert abs(rows[2]["current_period_end"] - (datetime.now(UTC) + timedelta(days=47))) < SECOND


async def test_users_do_not_affect_each_others_subscriptions(app_pool, psvc, shop):
    await buy(psvc, 1, shop["p30"])
    await buy(psvc, 2, shop["pro"])
    await buy(psvc, 1, shop["p30"])
    assert len(await subs(app_pool, 1)) == 1 and len(await subs(app_pool, 2)) == 1


# ------------------------------------------------------------------ invariants and concurrency


@pytest.mark.parametrize(
    "sequence",
    [
        ["p30", "p30", "p90", "pro", "p30", "vip", "p30", "pro"],
        ["vip", "pro", "p30", "p30", "pro", "vip"],
        ["pro", "vip", "p30"],
        ["p30", "pro", "p30", "p30"],
    ],
)
async def test_no_sequence_of_purchases_ever_overlaps_two_subscriptions(
    app_pool, psvc, shop, sequence, clock
):
    for step in sequence:
        clock.advance(120)  # keep the payment-flow rate limits out of the way
        await buy(psvc, 1, shop[step])
        assert await effective_now(app_pool) == 1
        # also at the start of every queued subscription there is exactly one running
        starts = [r["started_at"] for r in await subs(app_pool) if r["status"] == "active"]
        for t in starts:
            overlapping = await app_pool.fetchval(
                "SELECT count(*) FROM subscriptions WHERE user_id=1 AND status='active' "
                "AND started_at <= $1 AND current_period_end > $1",
                t + timedelta(seconds=1),
            )
            assert overlapping == 1


async def test_parallel_approvals_of_one_payment_create_one_subscription_even_when_renewing(
    app_pool, psvc, shop
):
    await buy(psvc, 1, shop["p30"])
    order = (await psvc.payments.start(1, shop["p30"])).order
    await psvc.payments.attach_evidence(1, order.payment_id, img.PNG)
    await psvc.payments.submit(1, order.payment_id, None)
    before = (await subs(app_pool))[0]["current_period_end"]
    results = await asyncio.gather(
        *(psvc.payments.approve(ADMIN, order.payment_id) for _ in range(6))
    )
    assert sorted(r.kind for r in results).count("ok") == 1
    rows = await subs(app_pool)
    assert len(rows) == 1 and rows[0]["current_period_end"] == before + timedelta(
        days=30
    )  # added once


async def test_extending_the_running_plan_pushes_the_queued_one_back_so_nothing_overlaps(
    app_pool, psvc, shop
):
    await buy(psvc, 1, shop["vip"])
    await buy(psvc, 1, shop["p30"])  # queued behind VIP
    before = await subs(app_pool)
    await buy(psvc, 1, shop["vip"])  # renew VIP
    vip, queued = await subs(app_pool)
    assert vip["current_period_end"] == before[0]["current_period_end"] + timedelta(days=30)
    assert queued["started_at"] == vip["current_period_end"]  # still back-to-back
    assert queued["current_period_end"] == before[1]["current_period_end"] + timedelta(days=30)
    assert await effective_now(app_pool) == 1


async def test_the_audit_log_says_how_the_purchase_was_applied(app_pool, psvc, shop):
    await buy(psvc, 1, shop["p30"])
    await buy(psvc, 1, shop["pro"])
    details = [
        r["detail"]
        for r in await app_pool.fetch(
            "SELECT detail FROM admin_actions WHERE action='payment_approve' ORDER BY id"
        )
    ]
    assert details[0].endswith("started") and details[1].endswith("upgraded")


async def test_db_still_refuses_two_subscriptions_for_one_payment_reference(app_pool, psvc, shop):
    await buy(psvc, 1, shop["p30"])
    ref = await app_pool.fetchval("SELECT provider_ref FROM subscriptions")
    import asyncpg

    with pytest.raises(asyncpg.UniqueViolationError):
        await app_pool.execute(
            "INSERT INTO subscriptions (user_id, plan_id, provider, provider_ref) "
            "VALUES (1, $1, 'manual_upi', $2)",
            shop["p30"],
            ref,
        )


async def test_a_one_rupee_test_payment_does_not_turn_premium_into_free_vip(app_pool, psvc, shop):
    """The reported case: the Premium order was paid with a Rs 1 test amount, but the plan is Rs 99,
    so the upgrade converts at Rs 99 -> Rs 499, never 1:1 (which gave a free extra 30 days)."""
    await app_pool.execute("UPDATE plans SET price_minor = 100 WHERE id = $1", shop["p30"])
    paid = await buy(psvc, 1, shop["p30"])  # the order snapshots Rs 1
    assert await app_pool.fetchval("SELECT amount_minor FROM payments") == 100
    assert paid.mode == "started"
    await app_pool.execute("UPDATE plans SET price_minor = 9900 WHERE id = $1", shop["p30"])
    res = await buy(psvc, 1, shop["vip"])
    new = (await subs(app_pool))[-1]
    assert res.mode == "upgraded"
    # 30 x 99 / 499 = 5.95 -> 5 carried days: VIP runs 35 days, not 60
    assert abs(new["current_period_end"] - (datetime.now(UTC) + timedelta(days=35))) < SECOND

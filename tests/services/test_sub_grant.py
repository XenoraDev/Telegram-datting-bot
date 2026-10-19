"""S3: an admin grants a plan without a payment: same policy as a purchase, never a payment row."""

import asyncio
from datetime import timedelta

import pytest

from bot.services import build_services
from tests.factories import enable_payments, make_plan, make_user

ADMIN = 9000001
U, V, BANNED = 7101, 7102, 7103


@pytest.fixture
async def world(app_pool, clock):
    svc = build_services(app_pool, clock, clock, frozenset({ADMIN}))
    await enable_payments(app_pool)
    for uid in (U, V):
        await make_user(app_pool, uid)
    await make_user(app_pool, BANNED, status="banned")
    plans = {
        "prem": await make_plan(app_pool, "premium_30", price_minor=9900),
        "pro": await make_plan(app_pool, "pro_30", name="Pro", price_minor=19900, tier="pro"),
        "vip": await make_plan(app_pool, "vip_30", name="VIP", price_minor=29900, tier="vip"),
    }
    return svc, plans


async def subs(pool, uid=U):
    return [
        dict(r)
        for r in await pool.fetch("SELECT * FROM subscriptions WHERE user_id=$1 ORDER BY id", uid)
    ]


async def give(svc, plan, days=30, uid=U, reason="compensation for a lost payment", **kw):
    check = await svc.subs.preview_grant(ADMIN, uid, plan, days)
    return await svc.subs.grant(
        ADMIN,
        uid,
        plan,
        days,
        reason,
        confirmed=kw.pop("confirmed", True),
        expected=kw.pop("expected", check.expected),
        **kw,
    )


async def audits(pool):
    return [
        dict(r) for r in await pool.fetch("SELECT * FROM admin_actions WHERE action='sub_grant'")
    ]


async def near(pool, ts, days):
    now = await pool.fetchval("SELECT now()")
    return abs(ts - (now + timedelta(days=days))) < timedelta(seconds=30)


# ------------------------------------------------------------------ a fresh grant


async def test_a_grant_creates_a_subscription_marked_admin_and_no_payment(app_pool, world):
    svc, plans = world
    res = await give(svc, plans["vip"], 20)
    assert res.kind == "ok" and res.mode == "started" and res.days == 20
    (sub,) = await subs(app_pool)
    assert sub["provider"] == "admin_grant" and sub["provider_ref"].startswith("grant-")
    assert sub["status"] == "active" and await near(app_pool, sub["current_period_end"], 20)
    assert sub["plan_id"] == plans["vip"]
    for table in ("payments", "payment_evidence"):  # nothing in the money tables
        assert await app_pool.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    (a,) = await audits(app_pool)
    assert (a["admin_id"], a["target_user_id"], a["subscription_id"], a["payment_id"]) == (
        ADMIN,
        U,
        sub["id"],
        None,
    )
    assert a["reason"] == "compensation for a lost payment" and "20d" in a["detail"]


async def test_entitlements_follow_a_grant_at_once(app_pool, world):
    svc, plans = world
    assert (await svc.entitlements.get(U, "plan_badge")).enabled is False
    await give(svc, plans["vip"])
    assert (await svc.entitlements.get(U, "plan_badge")).level == 3


# ------------------------------------------------------------------ same policy as a purchase


async def test_the_same_tier_extends_what_the_user_has(app_pool, world):
    svc, plans = world
    await give(svc, plans["pro"], 30)
    before = (await subs(app_pool))[0]
    res = await give(svc, plans["pro"], 10)
    (after,) = await subs(app_pool)
    assert res.kind == "ok" and res.mode == "extended" and res.subscription_id == before["id"]
    assert after["current_period_end"] == before["current_period_end"] + timedelta(days=10)


async def test_a_higher_tier_replaces_now_and_converts_the_time_by_plan_price(app_pool, world):
    svc, plans = world
    await give(svc, plans["prem"], 30)  # Rs 99 / 30 days
    res = await give(svc, plans["vip"], 30)  # Rs 299 / 30 days
    old, new = await subs(app_pool)
    assert res.kind == "ok" and res.mode == "upgraded"
    assert old["status"] == "canceled" and new["status"] == "active"
    assert await near(app_pool, new["current_period_end"], 39)  # 30 + floor(30 x 99/299) = 9


async def test_a_lower_tier_waits_for_the_current_time(app_pool, world):
    svc, plans = world
    await give(svc, plans["vip"], 30)
    vip = (await subs(app_pool))[0]
    res = await give(svc, plans["prem"], 30)
    prem = (await subs(app_pool))[1]
    assert res.kind == "ok" and res.mode == "queued"
    assert prem["started_at"] == vip["current_period_end"]
    assert prem["current_period_end"] == prem["started_at"] + timedelta(days=30)


async def test_extending_pushes_the_queued_one_back(app_pool, world):
    svc, plans = world
    await give(svc, plans["vip"], 30)
    await give(svc, plans["prem"], 30)  # queued behind
    vip, prem = await subs(app_pool)
    await give(svc, plans["vip"], 5)
    vip2, prem2 = await subs(app_pool)
    assert vip2["current_period_end"] == vip["current_period_end"] + timedelta(days=5)
    assert prem2["started_at"] == vip2["current_period_end"]  # still back to back


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize("days", [0, -3, 366, 10**6])
async def test_days_must_be_sensible(app_pool, world, days):
    svc, plans = world
    check = await svc.subs.preview_grant(ADMIN, U, plans["vip"], days)
    assert check.problem == "bad_days"
    res = await svc.subs.grant(
        ADMIN,
        U,
        plans["vip"],
        days,
        "a long enough reason",
        confirmed=True,
        expected=check.expected,
    )
    assert res.kind == "bad_days" and await subs(app_pool) == []


async def test_only_a_purchasable_active_plan_can_be_granted(app_pool, world):
    svc, plans = world
    free = await app_pool.fetchval("SELECT id FROM plans WHERE code = 'free'")
    off = await make_plan(app_pool, "old_plan", name="Old", price_minor=100, active=False)
    for plan in (free, off, 999999):
        check = await svc.subs.preview_grant(ADMIN, U, plan, 10)
        assert check.problem == "plan_unavailable", plan
        res = await svc.subs.grant(
            ADMIN, U, plan, 10, "a long enough reason", confirmed=True, expected=check.expected
        )
        assert res.kind == "plan_unavailable"
    assert await subs(app_pool) == [] and await audits(app_pool) == []


async def test_a_banned_or_unknown_user_gets_nothing(app_pool, world):
    svc, plans = world
    assert (await give(svc, plans["vip"], uid=BANNED)).kind == "user_inactive"
    res = await svc.subs.grant(
        ADMIN, 424242, plans["vip"], 10, "an unknown account", confirmed=True, expected="x"
    )
    assert res.kind == "not_found"
    assert await subs(app_pool, BANNED) == [] and await audits(app_pool) == []


async def test_a_confirmation_and_a_written_reason_are_required(app_pool, world):
    svc, plans = world
    assert (await give(svc, plans["vip"], confirmed=False)).kind == "confirm_required"
    for reason in ("", "  ", "ab", None):
        assert (await give(svc, plans["vip"], reason=reason)).kind == "reason_required"
    assert await subs(app_pool) == [] and await audits(app_pool) == []


async def test_only_admins_can_grant(app_pool, world):
    svc, plans = world
    res = await svc.subs.grant(
        U, U, plans["vip"], 10, "i am not an admin", confirmed=True, expected=""
    )
    assert res.kind == "denied" and await svc.subs.preview_grant(U, U, plans["vip"], 10) is None
    assert await subs(app_pool) == []


async def test_an_open_ended_subscription_is_not_guessed_at(app_pool, world):
    svc, plans = world
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) VALUES ($1, $2, NULL)",
        U,
        plans["pro"],
    )
    check = await svc.subs.preview_grant(ADMIN, U, plans["vip"], 10)
    assert check.problem == "no_end"


# ------------------------------------------------------------------ idempotency / concurrency


async def test_a_double_submit_grants_once(app_pool, world):
    svc, plans = world
    check = await svc.subs.preview_grant(ADMIN, U, plans["vip"], 10)
    kw = {"confirmed": True, "expected": check.expected}
    first = await svc.subs.grant(ADMIN, U, plans["vip"], 10, "compensation as promised", **kw)
    second = await svc.subs.grant(ADMIN, U, plans["vip"], 10, "compensation as promised", **kw)
    assert (first.kind, second.kind) == ("ok", "changed")
    assert len(await subs(app_pool)) == 1 and len(await audits(app_pool)) == 1


async def test_two_admins_at_once_create_one_subscription(app_pool, world):
    svc, plans = world
    check = await svc.subs.preview_grant(ADMIN, U, plans["vip"], 10)
    kw = {"confirmed": True, "expected": check.expected}
    results = await asyncio.gather(
        *(
            svc.subs.grant(ADMIN, U, plans["vip"], 10, "compensation as promised", **kw)
            for _ in range(2)
        )
    )
    assert sorted(r.kind for r in results) == ["changed", "ok"]
    assert len(await subs(app_pool)) == 1


async def test_the_preview_changes_nothing(app_pool, world):
    svc, plans = world
    check = await svc.subs.preview_grant(ADMIN, U, plans["vip"], 15)
    assert check.problem is None and check.mode == "started"
    assert await subs(app_pool) == [] and await audits(app_pool) == []


# ------------------------------------------------------------------ with the rest of S2/S3


async def test_other_users_are_untouched(app_pool, world):
    svc, plans = world
    await give(svc, plans["pro"], uid=V)
    before = await subs(app_pool, V)
    await give(svc, plans["vip"], uid=U)
    assert await subs(app_pool, V) == before


async def test_a_granted_subscription_can_be_shortened_and_cancelled(app_pool, world):
    svc, plans = world
    res = await give(svc, plans["vip"], 30)
    sid = res.subscription_id
    check = await svc.subs.preview_shorten(ADMIN, sid, 10)
    cut = await svc.subs.shorten(
        ADMIN, sid, 10, "granted too many days", confirmed=True, expected=check.expected
    )
    assert cut.kind == "ok"
    assert await near(app_pool, (await subs(app_pool))[0]["current_period_end"], 20)
    done = await svc.subs.cancel(ADMIN, sid, "granted by mistake", confirmed=True)
    assert done.kind == "ok" and (await subs(app_pool))[0]["status"] == "canceled"


async def test_a_grant_on_a_paid_subscription_makes_its_payment_unrevocable(app_pool, world):
    from tests import payment_images as img

    svc, plans = world
    order = (await svc.payments.start(U, plans["pro"])).order
    await svc.payments.attach_evidence(U, order.payment_id, img.JPEG)
    assert (await svc.payments.submit(U, order.payment_id, None)).kind == "ok"
    assert (await svc.payments.approve(ADMIN, order.payment_id)).kind == "ok"
    assert (await svc.subs.check_revoke(ADMIN, order.payment_id)).problem is None
    await give(svc, plans["pro"], 10)  # extends the paid subscription with free days
    assert (await svc.subs.check_revoke(ADMIN, order.payment_id)).problem == "ambiguous"
    assert await app_pool.fetchval("SELECT count(*) FROM payments") == 1  # still just the one

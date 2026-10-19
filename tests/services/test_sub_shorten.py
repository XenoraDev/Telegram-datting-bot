"""S3: take time off a subscription (by days or by one payment's extension) without guessing."""

import asyncio
from datetime import timedelta

import pytest

from bot.services import build_services
from tests import payment_images as img
from tests.factories import enable_payments, make_plan, make_user

ADMIN = 9000001
U, V = 7001, 7002
SECOND = timedelta(seconds=30)


@pytest.fixture
def subs_svc(app_pool, clock):
    return build_services(app_pool, clock, clock, frozenset({ADMIN}))


ANCHOR = {}


@pytest.fixture
async def world(app_pool, subs_svc):
    ANCHOR["t"] = await app_pool.fetchval("SELECT now()")  # one clock reading for every date
    await enable_payments(app_pool)
    for uid in (U, V):
        await make_user(app_pool, uid)
    return subs_svc


async def mk(pool, uid, tier, start, end, **cols):
    """A subscription from `start` to `end`, both in days from now (None = no end date)."""
    plan = await pool.fetchval("SELECT id FROM plans WHERE code = $1", f"t_{tier}")
    if plan is None:
        plan = await make_plan(pool, f"t_{tier}", name=tier.title(), tier=tier)
    return await pool.fetchval(
        "INSERT INTO subscriptions (user_id, plan_id, started_at, current_period_end, status) "
        "VALUES ($1, $2, $6::timestamptz + make_interval(days => $3), "
        "CASE WHEN $4::int IS NULL THEN NULL "
        "ELSE $6::timestamptz + make_interval(days => $4) END, $5) "
        "RETURNING id",
        uid,
        plan,
        start,
        end,
        cols.get("status", "active"),
        ANCHOR["t"],
    )


async def row(pool, sid):
    return dict(await pool.fetchrow("SELECT * FROM subscriptions WHERE id = $1", sid))


async def seen(svc, sid, days):
    return (await svc.subs.preview_shorten(ADMIN, sid, days)).expected


async def cut(svc, sid, days, reason="wrong amount was approved", **kw):
    expected = kw.pop("expected", None)
    if expected is None:
        expected = await seen(svc, sid, days)
    return await svc.subs.shorten(
        ADMIN, sid, days, reason, confirmed=kw.pop("confirmed", True), expected=expected, **kw
    )


async def audits(pool, action="sub_shorten"):
    return [
        dict(r) for r in await pool.fetch("SELECT * FROM admin_actions WHERE action=$1", action)
    ]


# ------------------------------------------------------------------ by days


async def test_shortening_a_running_subscription_moves_only_its_end(app_pool, world):
    sid = await mk(app_pool, U, "vip", 0, 30)
    before = await row(app_pool, sid)
    res = await cut(world, sid, 10)
    after = await row(app_pool, sid)
    assert res.kind == "ok" and res.days == 10 and res.shifted == 0
    assert after["current_period_end"] == before["current_period_end"] - timedelta(days=10)
    assert {k: v for k, v in after.items() if k != "current_period_end"} == {
        k: v for k, v in before.items() if k != "current_period_end"
    }
    assert after["status"] == "active"
    (a,) = await audits(app_pool)
    assert (a["admin_id"], a["target_user_id"], a["subscription_id"], a["payment_id"]) == (
        ADMIN,
        U,
        sid,
        None,
    )
    assert a["reason"] == "wrong amount was approved" and "-10d" in a["detail"]
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 1  # nothing deleted


async def test_queued_subscriptions_move_with_it_and_never_overlap(app_pool, world):
    vip = await mk(app_pool, U, "vip", 0, 30)
    prem = await mk(app_pool, U, "premium", 30, 60)
    other = await mk(app_pool, U, "pro", 60, 70)
    old = {s: await row(app_pool, s) for s in (vip, prem, other)}
    res = await cut(world, vip, 10)
    assert res.kind == "ok" and res.shifted == 2
    new = {s: await row(app_pool, s) for s in (vip, prem, other)}
    for s in (prem, other):  # same length, ten days earlier
        assert new[s]["started_at"] == old[s]["started_at"] - timedelta(days=10)
        assert new[s]["current_period_end"] == old[s]["current_period_end"] - timedelta(days=10)
        assert (
            new[s]["current_period_end"] - new[s]["started_at"]
            == old[s]["current_period_end"] - old[s]["started_at"]
        )
    assert new[vip]["current_period_end"] == new[prem]["started_at"]  # back to back
    assert new[prem]["current_period_end"] == new[other]["started_at"]


async def test_the_preview_shows_the_chain_and_changes_nothing(app_pool, world):
    vip = await mk(app_pool, U, "vip", 0, 30)
    prem = await mk(app_pool, U, "premium", 30, 60)
    before = [await row(app_pool, s) for s in (vip, prem)]
    check = await world.subs.preview_shorten(ADMIN, vip, 10)
    assert check.problem is None and check.new_end == before[0]["current_period_end"] - timedelta(
        days=10
    )
    assert [(s.id, s.new_start) for s in check.shifts] == [
        (prem, before[1]["started_at"] - timedelta(days=10))
    ]
    assert [await row(app_pool, s) for s in (vip, prem)] == before
    assert await audits(app_pool) == []


async def test_a_subscription_is_never_shortened_to_nothing(app_pool, world):
    sid = await mk(app_pool, U, "pro", 0, 5)
    check = await world.subs.preview_shorten(ADMIN, sid, 5)
    assert check.problem == "too_much" and check.max_days == 4  # leaves at least an hour
    before = await row(app_pool, sid)
    res = await cut(world, sid, 5, expected=before["current_period_end"].isoformat())
    assert res.kind == "too_much" and res.max_days == 4
    assert await row(app_pool, sid) == before and await audits(app_pool) == []
    assert (await cut(world, sid, 4)).kind == "ok"
    left = (await row(app_pool, sid))["current_period_end"] - await app_pool.fetchval(
        "SELECT now()"
    )
    assert timedelta(hours=1) < left < timedelta(days=2)


async def test_a_queued_subscription_can_be_shortened_but_keeps_its_start(app_pool, world):
    vip = await mk(app_pool, U, "vip", 0, 30)
    prem = await mk(app_pool, U, "premium", 30, 60)
    before = await row(app_pool, prem)
    assert (await cut(world, prem, 10)).kind == "ok"
    after = await row(app_pool, prem)
    assert after["started_at"] == before["started_at"]
    assert after["current_period_end"] == before["current_period_end"] - timedelta(days=10)
    assert after["started_at"] == (await row(app_pool, vip))["current_period_end"]
    # a queued one can not be cut below its own start (+ an hour)
    assert (await world.subs.preview_shorten(ADMIN, prem, 20)).problem == "too_much"


@pytest.mark.parametrize("status", ["canceled", "expired", "past_due"])
async def test_ended_subscriptions_are_refused_and_never_revived(app_pool, world, status):
    sid = await mk(app_pool, U, "pro", -5, 20, status=status)
    before = await row(app_pool, sid)
    res = await cut(world, sid, 3, expected=before["current_period_end"].isoformat())
    assert res.kind == "not_active" and await row(app_pool, sid) == before


async def test_a_subscription_that_is_over_by_date_is_refused(app_pool, world):
    sid = await mk(app_pool, U, "pro", -40, -10)
    before = await row(app_pool, sid)
    res = await cut(world, sid, 3, expected=before["current_period_end"].isoformat())
    assert res.kind == "not_active" and await row(app_pool, sid) == before


async def test_a_subscription_without_an_end_date_is_refused(app_pool, world):
    sid = await mk(app_pool, U, "pro", 0, None)
    res = await world.subs.shorten(ADMIN, sid, 3, "needs a date first", confirmed=True, expected="")
    assert res.kind == "no_end"


@pytest.mark.parametrize("days", [0, -1, 3661, 10**9])
async def test_days_must_be_sensible(app_pool, world, days):
    sid = await mk(app_pool, U, "pro", 0, 30)
    check = await world.subs.preview_shorten(ADMIN, sid, days)
    assert check.problem == "bad_days"
    res = await world.subs.shorten(
        ADMIN, sid, days, "because of a mistake", confirmed=True, expected=check.expected
    )
    assert res.kind == "bad_days" and (await audits(app_pool)) == []


async def test_overlapping_subscriptions_are_not_guessed_at(app_pool, world):
    a = await mk(app_pool, U, "vip", 0, 30)
    await mk(app_pool, U, "premium", 10, 40)  # starts inside the first: not a state we create
    before = await row(app_pool, a)
    res = await cut(world, a, 5, expected=before["current_period_end"].isoformat())
    assert res.kind == "overlap" and await row(app_pool, a) == before


async def test_a_confirmation_and_a_written_reason_are_required(app_pool, world):
    sid = await mk(app_pool, U, "pro", 0, 30)
    before = await row(app_pool, sid)
    exp = before["current_period_end"].isoformat()
    assert (await cut(world, sid, 3, confirmed=False, expected=exp)).kind == "confirm_required"
    for reason in ("", "  ", "ab", None):
        res = await cut(world, sid, 3, reason=reason, expected=exp)
        assert res.kind == "reason_required"
    assert await row(app_pool, sid) == before and await audits(app_pool) == []


async def test_only_admins_can_shorten(app_pool, world):
    sid = await mk(app_pool, U, "pro", 0, 30)
    res = await world.subs.shorten(U, sid, 3, "i am not an admin", confirmed=True, expected="x")
    assert res.kind == "denied" and await world.subs.preview_shorten(U, sid, 3) is None
    assert (
        await world.subs.shorten(ADMIN, 999999, 3, "no such one", confirmed=True, expected="x")
    ).kind == "not_found"


async def test_a_double_submit_removes_the_time_once(app_pool, world):
    sid = await mk(app_pool, U, "vip", 0, 30)
    exp = await seen(world, sid, 10)
    first = await cut(world, sid, 10, expected=exp)
    second = await cut(world, sid, 10, expected=exp)  # the same form posted again
    assert (first.kind, second.kind) == ("ok", "changed")
    assert len(await audits(app_pool)) == 1


async def test_two_admins_at_the_same_moment_cannot_both_win(app_pool, world):
    sid = await mk(app_pool, U, "vip", 0, 30)
    queued = await mk(app_pool, U, "premium", 30, 60)
    exp = await seen(world, sid, 10)
    results = await asyncio.gather(
        *(cut(world, sid, 10, expected=exp) for _ in range(2)), return_exceptions=True
    )
    assert sorted(r.kind for r in results) == ["changed", "ok"]
    vip, prem = await row(app_pool, sid), await row(app_pool, queued)
    assert vip["current_period_end"] == prem["started_at"]  # moved once, still back to back
    assert len(await audits(app_pool)) == 1


async def test_it_does_not_touch_other_users_or_payments(app_pool, world):
    mine = await mk(app_pool, U, "vip", 0, 30)
    theirs = await mk(app_pool, V, "vip", 0, 30)
    before = await row(app_pool, theirs)
    assert (await cut(world, mine, 10)).kind == "ok"
    assert await row(app_pool, theirs) == before
    assert await app_pool.fetchval("SELECT count(*) FROM payments") == 0


async def test_entitlements_follow_the_new_dates(app_pool, world):
    vip = await mk(app_pool, U, "vip", 0, 10)
    prem = await mk(app_pool, U, "premium", 10, 40)
    assert (await world.entitlements.get(U, "plan_badge")).level == 3
    assert (await cut(world, vip, 9)).kind == "ok"  # VIP now ends in about a day
    assert (await world.entitlements.get(U, "plan_badge")).level == 3
    await app_pool.execute(  # a day later the VIP is over and the Premium (moved) has begun
        "UPDATE subscriptions SET started_at = started_at - interval '2 days', "
        "current_period_end = current_period_end - interval '2 days' WHERE id IN ($1, $2)",
        vip,
        prem,
    )
    assert (await world.entitlements.get(U, "plan_badge")).level == 1


# ------------------------------------------------------------------ by one payment's extension


async def buy(svc, uid, plan):
    order = (await svc.payments.start(uid, plan)).order
    await svc.payments.attach_evidence(uid, order.payment_id, img.JPEG)
    assert (await svc.payments.submit(uid, order.payment_id, None)).kind == "ok"
    assert (await svc.payments.approve(ADMIN, order.payment_id)).kind == "ok"
    return order.payment_id


@pytest.fixture
async def bought(app_pool, world):
    plan = await make_plan(app_pool, "premium_30", price_minor=9900)
    a = await buy(world, U, plan)  # creates the subscription
    b = await buy(world, U, plan)  # extends it by 30 days
    c = await buy(world, U, plan)  # extends it again
    sid = await app_pool.fetchval("SELECT subscription_id FROM payments WHERE id = $1", a)
    return sid, a, b, c


async def remove(svc, pid, reason="this payment was entered twice", **kw):
    check = await svc.subs.preview_remove_extension(ADMIN, pid)
    return await svc.subs.remove_extension(
        ADMIN, pid, reason, confirmed=kw.pop("confirmed", True), expected=check.expected, **kw
    )


async def payment_rows(pool):
    return [dict(r) for r in await pool.fetch("SELECT * FROM payments ORDER BY id")]


async def test_one_extension_comes_off_exactly_and_the_payment_row_is_untouched(
    app_pool, world, bought
):
    sid, a, b, c = bought
    before_sub, before_pay = await row(app_pool, sid), await payment_rows(app_pool)
    assert len({p["subscription_id"] for p in before_pay}) == 1  # all three point at it
    res = await remove(world, b)
    assert res.kind == "ok" and res.days == 30
    after = await row(app_pool, sid)
    assert after["current_period_end"] == before_sub["current_period_end"] - timedelta(days=30)
    assert after["started_at"] == before_sub["started_at"] and after["status"] == "active"
    assert await payment_rows(app_pool) == before_pay  # frozen rows: byte for byte
    (aud,) = await audits(app_pool)
    assert (aud["payment_id"], aud["subscription_id"], aud["admin_id"]) == (b, sid, ADMIN)


async def test_the_same_extension_can_only_be_removed_once(app_pool, world, bought):
    sid, a, b, c = bought
    check = await world.subs.preview_remove_extension(ADMIN, b)
    first = await world.subs.remove_extension(
        ADMIN, b, "entered twice by mistake", confirmed=True, expected=check.expected
    )
    again = await world.subs.remove_extension(
        ADMIN, b, "entered twice by mistake", confirmed=True, expected=check.expected
    )
    assert (first.kind, again.kind) == ("ok", "already_removed")
    assert (await world.subs.preview_remove_extension(ADMIN, b)).problem == "already_removed"
    assert len(await audits(app_pool)) == 1
    # another extension of the same subscription is a different payment and still removable
    assert (await remove(world, c)).kind == "ok"
    assert (await row(app_pool, sid))["current_period_end"] > await app_pool.fetchval(
        "SELECT now()"
    )


async def test_the_payment_that_created_the_subscription_is_refused(app_pool, world, bought):
    sid, a, b, c = bought
    before = await row(app_pool, sid)
    res = await world.subs.remove_extension(
        ADMIN, a, "remove the first payment", confirmed=True, expected=""
    )
    assert res.kind == "ambiguous" and await row(app_pool, sid) == before
    assert (await world.subs.preview_remove_extension(ADMIN, a)).problem == "ambiguous"
    assert await audits(app_pool) == []


async def test_unusable_payments_are_refused(app_pool, world, bought):
    plan = await app_pool.fetchval("SELECT plan_id FROM payments LIMIT 1")
    order = (await world.payments.start(V, plan)).order  # created, never approved
    res = await world.subs.remove_extension(
        ADMIN, order.payment_id, "was never approved", confirmed=True, expected=""
    )
    assert res.kind == "not_approved"
    res = await world.subs.remove_extension(
        ADMIN, 999999, "no such payment", confirmed=True, expected=""
    )
    assert res.kind == "not_found"


async def test_an_extension_of_an_ended_subscription_is_refused(app_pool, world, bought):
    sid, a, b, c = bought
    await app_pool.execute("UPDATE subscriptions SET status = 'canceled' WHERE id = $1", sid)
    assert (await world.subs.preview_remove_extension(ADMIN, b)).problem == "already_ended"


async def test_removing_an_extension_moves_what_is_queued_behind_it(app_pool, world, bought):
    sid, a, b, c = bought
    end = (await row(app_pool, sid))["current_period_end"]
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, started_at, current_period_end) "
        "SELECT user_id, plan_id, $2::timestamptz, $2::timestamptz + interval '30 days' "
        "FROM subscriptions WHERE id = $1",
        sid,
        end,
    )
    res = await remove(world, b)
    assert res.kind == "ok" and res.shifted == 1
    vip = await row(app_pool, sid)
    queued = dict(await app_pool.fetchrow("SELECT * FROM subscriptions WHERE id <> $1", sid))
    assert queued["started_at"] == vip["current_period_end"]


async def test_a_removal_is_refused_when_the_dates_changed_after_the_preview(
    app_pool, world, bought
):
    sid, a, b, c = bought
    check = await world.subs.preview_remove_extension(ADMIN, b)
    await cut(world, sid, 1)  # someone else changed the subscription meanwhile
    res = await world.subs.remove_extension(
        ADMIN, b, "entered twice by mistake", confirmed=True, expected=check.expected
    )
    assert res.kind == "changed" and len(await audits(app_pool)) == 1


async def test_revoking_the_creating_payment_is_refused_after_an_admin_changed_the_time(
    app_pool, world
):
    plan = await make_plan(app_pool, "premium_30", price_minor=9900)
    a = await buy(world, U, plan)
    sid = await app_pool.fetchval("SELECT subscription_id FROM payments WHERE id = $1", a)
    assert (await world.subs.check_revoke(ADMIN, a)).problem is None  # revocable on its own
    assert (await cut(world, sid, 5)).kind == "ok"
    assert (await world.subs.check_revoke(ADMIN, a)).problem == "ambiguous"
    res = await world.subs.revoke_payment(ADMIN, a, "chargeback on the card", confirmed=True)
    assert res.kind == "ambiguous" and (await row(app_pool, sid))["status"] == "active"

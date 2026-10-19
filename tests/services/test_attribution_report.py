"""Traffic report: exact aggregates, the funnel definitions, exclusions and privacy."""

from datetime import UTC, datetime, timedelta

import pytest

from bot.services import build_services
from bot.services.attribution import build_report, pct
from tests import payment_images as img
from tests.factories import enable_payments, ended_session, make_plan, make_user

ADMIN = 9000001
NOW = datetime.now(UTC)
WIDE = (NOW - timedelta(days=30), NOW + timedelta(days=1))


@pytest.fixture
def svc(app_pool, clock):
    return build_services(app_pool, clock, clock, frozenset({ADMIN}))


async def new_user(pool, uid, **kw):
    await make_user(pool, uid, **kw)
    return uid


async def touch(pool, uid, kind, campaign=None, source=None, when=None):
    """An acquisition row (written directly so the date can be chosen)."""
    await pool.execute(
        "INSERT INTO user_acquisition (user_id, kind, campaign_id, source, acquired_at) "
        "VALUES ($1, $2, $3, $4, coalesce($5, now()))",
        uid,
        kind,
        campaign,
        source,
        when,
    )


async def campaign(svc, source, name, *, test=False, paused=False):
    c = (await svc.attribution.create_campaign(ADMIN, source, name, is_test=test)).campaign
    if paused:
        await svc.attribution.set_status(ADMIN, c["id"], paused=True)
    return c


async def report(svc, window=WIDE, **kw):
    return await svc.attribution.report(ADMIN, *window, **kw)


def row_for(rep, cid):
    return next(c for c in rep["campaigns"] if c["id"] == cid)


async def buy(svc, uid, plan):
    order = (await svc.payments.start(uid, plan)).order
    await svc.payments.attach_evidence(uid, order.payment_id, img.JPEG)
    assert (await svc.payments.submit(uid, order.payment_id, None)).kind == "ok"
    return order.payment_id


# ------------------------------------------------------------------ the funnel steps


async def test_each_step_is_counted_once_per_acquired_account(app_pool, svc):
    a = await campaign(svc, "instagram", "Reel A")
    await enable_payments(app_pool)
    plan = await make_plan(app_pool, "premium_30", price_minor=9900)
    for uid in range(9401, 9407):
        await new_user(app_pool, uid)
        await touch(app_pool, uid, "campaign", a["id"], "instagram")
    await app_pool.execute(
        "UPDATE users SET status = 'onboarding', age_confirmed_at = NULL WHERE id = 9406"
    )  # no age yet
    await app_pool.execute("DELETE FROM profiles WHERE user_id IN (9405, 9406)")  # no profile
    await ended_session(app_pool, 9401, 9402)  # 9401 and 9402 chatted
    await app_pool.execute(
        "INSERT INTO swipes (swiper_id, target_id, action) VALUES (9401, 9403, 'like'), "
        "(9402, 9403, 'pass')"
    )
    await app_pool.execute("INSERT INTO matches (user_a_id, user_b_id) VALUES (9401, 9403)")
    approved = await buy(svc, 9401, plan)
    assert (await svc.payments.approve(ADMIN, approved)).kind == "ok"
    await buy(svc, 9402, plan)  # an order with proof, not approved
    row = row_for(await report(svc), a["id"])
    assert (row["acquired"], row["age"], row["profile"]) == (6, 5, 4)
    assert (row["chat"], row["follow"], row["matched"]) == (2, 1, 2)  # 9401+9402; 9401; 9401+9403
    assert (row["pay_started"], row["paid"], row["revenue"]) == (2, 1, 9900)
    assert row["profile_pct"] == pct(4, 6) == 66.7 and row["paid_pct"] == 16.7
    assert row["revenue_per_user"] == round(9900 / 6)


async def test_a_revoked_payment_is_not_revenue_and_not_paid(app_pool, svc):
    a = await campaign(svc, "instagram", "Reel A")
    await enable_payments(app_pool)
    plan = await make_plan(app_pool, "premium_30", price_minor=9900)
    await new_user(app_pool, 9401)
    await touch(app_pool, 9401, "campaign", a["id"], "instagram")
    pid = await buy(svc, 9401, plan)
    assert (await svc.payments.approve(ADMIN, pid)).kind == "ok"
    assert row_for(await report(svc), a["id"])["revenue"] == 9900
    res = await svc.subs.revoke_payment(ADMIN, pid, "chargeback on the card", confirmed=True)
    assert res.kind == "ok"
    row = row_for(await report(svc), a["id"])
    assert (row["paid"], row["revenue"], row["pay_started"]) == (0, 0, 1)


async def test_an_admin_grant_is_never_revenue(app_pool, svc):
    a = await campaign(svc, "instagram", "Reel A")
    await enable_payments(app_pool)
    plan = await make_plan(app_pool, "premium_30", price_minor=9900)
    await new_user(app_pool, 9401)
    await touch(app_pool, 9401, "campaign", a["id"], "instagram")
    check = await svc.subs.preview_grant(ADMIN, 9401, plan, 30)
    await svc.subs.grant(
        ADMIN, 9401, plan, 30, "a promised bonus", confirmed=True, expected=check.expected
    )
    row = row_for(await report(svc), a["id"])
    assert (row["paid"], row["revenue"], row["pay_started"]) == (0, 0, 0)


# ------------------------------------------------------------------ who is counted


async def test_sources_campaigns_direct_and_invalid_are_kept_apart(app_pool, svc):
    a = await campaign(svc, "instagram", "Reel A")
    b = await campaign(svc, "instagram", "Reel B")
    y = await campaign(svc, "youtube", "Video")
    p = await campaign(svc, "channel_a", "Old post", paused=True)
    layout = [
        (9401, "campaign", a, "instagram"),
        (9402, "campaign", a, "instagram"),
        (9403, "campaign", b, "instagram"),
        (9404, "campaign", y, "youtube"),
        (9405, "direct", None, None),
        (9406, "direct", None, None),
        (9407, "invalid", None, None),
        (9408, "invalid", p, None),  # a paused campaign's link
    ]
    for uid, kind, camp, source in layout:
        await new_user(app_pool, uid)
        await touch(app_pool, uid, kind, camp["id"] if camp else None, source)
    rep = await report(svc)
    assert rep["totals"]["acquired"] == 8
    assert {k: v["acquired"] for k, v in rep["kinds"].items()} == {
        "campaign": 4,
        "direct": 2,
        "invalid": 2,
    }
    assert {s["source"]: s["acquired"] for s in rep["sources"]} == {"instagram": 3, "youtube": 1}
    assert [s["source"] for s in rep["sources"]][0] == "instagram"  # most users first
    assert (row_for(rep, a["id"])["acquired"], row_for(rep, b["id"])["acquired"]) == (2, 1)
    # a paused link's start is shown as a dead-link count, never as that campaign's acquisition
    assert (
        row_for(rep, p["id"])["acquired"] == 0 and row_for(rep, p["id"])["paused_link_starts"] == 1
    )
    assert row_for(rep, p["id"])["status"] == "paused"


async def test_a_campaign_without_users_is_listed_with_zeros(app_pool, svc):
    c = await campaign(svc, "instagram", "Quiet")
    row = row_for(await report(svc), c["id"])
    assert row["acquired"] == 0 and row["profile_pct"] == 0.0 and row["revenue_per_user"] == 0


async def test_admin_load_test_and_test_campaign_accounts_are_left_out(app_pool, svc):
    real = await campaign(svc, "instagram", "Real")
    test = await campaign(svc, "instagram", "Test link", test=True)
    await new_user(app_pool, 9401)
    await touch(app_pool, 9401, "campaign", real["id"], "instagram")
    await new_user(app_pool, ADMIN)  # an admin's own account
    await touch(app_pool, ADMIN, "campaign", real["id"], "instagram")
    await new_user(app_pool, -5)  # a load-test user
    await touch(app_pool, -5, "campaign", real["id"], "instagram")
    await new_user(app_pool, 9402)
    await touch(app_pool, 9402, "campaign", test["id"], "instagram")
    rep = await report(svc)
    assert rep["totals"]["acquired"] == 1 and row_for(rep, real["id"])["acquired"] == 1
    assert all(c["id"] != test["id"] for c in rep["campaigns"])  # test campaigns are hidden
    full = await report(svc, include_tests=True)
    assert full["totals"]["acquired"] == 2 and row_for(full, test["id"])["acquired"] == 1


async def test_only_accounts_acquired_in_the_window_count(app_pool, svc):
    a = await campaign(svc, "instagram", "Reel A")
    for uid, days_ago in ((9401, 1), (9402, 10), (9403, 40)):
        await new_user(app_pool, uid)
        await touch(app_pool, uid, "campaign", a["id"], "instagram", NOW - timedelta(days=days_ago))
    last7 = (NOW - timedelta(days=7), NOW + timedelta(days=1))
    assert (await report(svc, last7))["totals"]["acquired"] == 1
    assert (await report(svc))["totals"]["acquired"] == 2
    empty = (NOW + timedelta(days=5), NOW + timedelta(days=6))
    rep = await report(svc, empty)
    assert rep["totals"]["acquired"] == 0 and rep["totals"]["profile_pct"] == 0.0  # no 0/0 errors


async def test_accounts_without_a_first_touch_are_not_invented_only_counted(app_pool, svc):
    a = await campaign(svc, "instagram", "Reel A")
    await new_user(app_pool, 9401)
    await touch(app_pool, 9401, "campaign", a["id"], "instagram")
    await new_user(app_pool, 9402)  # an account from before tracking
    await new_user(app_pool, 9403, status="deleted", confirmed=False)
    rep = await report(svc)
    assert rep["totals"]["acquired"] == 1 and rep["untracked"] == 1  # the deleted one is not "live"


async def test_a_deleted_account_drops_out_of_the_numbers(app_pool, svc):
    a = await campaign(svc, "instagram", "Reel A")
    await new_user(app_pool, 9401)
    await touch(app_pool, 9401, "campaign", a["id"], "instagram")
    assert (await report(svc))["totals"]["acquired"] == 1
    await svc.account.delete_account(9401)
    assert (await report(svc))["totals"]["acquired"] == 0


# ------------------------------------------------------------------ access and privacy


async def test_only_admins_get_the_report(app_pool, svc):
    assert await svc.attribution.report(9101, *WIDE) is None


async def test_the_report_holds_no_user_identifying_data(app_pool, svc):
    a = await campaign(svc, "instagram", "Reel A")
    await enable_payments(app_pool)
    plan = await make_plan(app_pool, "premium_30", price_minor=9900)
    for uid in (7771001, 7771002):
        await new_user(app_pool, uid)
        await touch(app_pool, uid, "campaign", a["id"], "instagram")
    await app_pool.execute("UPDATE users SET username = 'secret_person' WHERE id = 7771001")
    pid = await buy(svc, 7771001, plan)
    await svc.payments.approve(ADMIN, pid)
    text = repr(await report(svc))
    for leak in ("7771001", "7771002", "secret_person", "PAY-"):
        assert leak not in text


def test_build_report_rolls_campaigns_up_into_sources():
    rows = [
        {
            "kind": "campaign",
            "source": "instagram",
            "campaign_id": 1,
            "acquired": 4,
            "age": 4,
            "profile": 2,
            "chat": 1,
            "follow": 1,
            "matched": 0,
            "pay_started": 1,
            "paid": 1,
            "revenue": 9900,
        },
        {
            "kind": "campaign",
            "source": "instagram",
            "campaign_id": 2,
            "acquired": 6,
            "age": 5,
            "profile": 3,
            "chat": 2,
            "follow": 2,
            "matched": 1,
            "pay_started": 0,
            "paid": 0,
            "revenue": 0,
        },
    ]
    campaigns = [
        {
            "id": 1,
            "name": "A",
            "source": "instagram",
            "status": "active",
            "is_test": False,
            "code": "aaaaaaaa",
        },
        {
            "id": 2,
            "name": "B",
            "source": "instagram",
            "status": "active",
            "is_test": False,
            "code": "bbbbbbbb",
        },
    ]
    rep = build_report(rows, campaigns, 0, include_tests=False)
    (src,) = rep["sources"]
    assert (src["acquired"], src["profile"], src["revenue"]) == (10, 5, 9900)
    assert src["profile_pct"] == 50.0 and src["revenue_per_user"] == 990
    assert [c["id"] for c in rep["campaigns"]] == [2, 1]  # most acquired first

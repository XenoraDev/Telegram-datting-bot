"""S3 in the admin web: shorten, remove one payment's extension, grant. Two steps, CSRF, audit."""

from datetime import timedelta

import pytest

from tests import payment_images as img
from tests.factories import enable_payments, make_plan, make_user
from tests.web.conftest import ADMIN

U, V = 8001, 8002


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@pytest.fixture
async def world(admin):
    await enable_payments(admin.pool)
    for uid in (U, V):
        await make_user(admin.pool, uid)
    admin.state.bot = FakeBot()
    admin.plans = {
        "prem": await make_plan(admin.pool, "premium_30", price_minor=9900),
        "vip": await make_plan(admin.pool, "vip_30", name="VIP", price_minor=29900, tier="vip"),
    }
    return admin


def told(admin, uid=U):
    return [t for c, t in admin.state.bot.sent if c == uid]


async def sub(pool, plan, uid=U, start=0, end=30):
    anchor = await pool.fetchval("SELECT date_trunc('second', now())")  # whole seconds: no drift
    return await pool.fetchval(
        "INSERT INTO subscriptions (user_id, plan_id, started_at, current_period_end) "
        "VALUES ($1, $2, $5::timestamptz + make_interval(days => $3), "
        "$5::timestamptz + make_interval(days => $4)) RETURNING id",
        uid,
        plan,
        start,
        end,
        anchor,
    )


async def row(pool, sid):
    return dict(await pool.fetchrow("SELECT * FROM subscriptions WHERE id = $1", sid))


async def audit(pool, action):
    return [
        dict(r) for r in await pool.fetch("SELECT * FROM admin_actions WHERE action=$1", action)
    ]


def expected_in(html):
    import re

    return re.search(r'name="expected" value="([^"]*)"', html).group(1)


async def preview(admin, sid, days):
    r = await admin.client.get(f"/admin/subscriptions/{sid}/shorten?days={days}")
    assert r.status_code == 200
    return r.text


# ------------------------------------------------------------------ shorten


async def test_the_shorten_page_previews_the_chain_and_changes_nothing(world):
    vip = await sub(world.pool, world.plans["vip"], start=0, end=30)
    prem = await sub(world.pool, world.plans["prem"], start=30, end=60)
    before = [await row(world.pool, s) for s in (vip, prem)]
    html = await preview(world, vip, 10)
    assert "What will change" in html and f"#{prem}" in html and "−10 days" in html
    assert 'name="confirm"' in html and 'name="reason"' in html and "csrf_token" in html
    assert [await row(world.pool, s) for s in (vip, prem)] == before
    assert await audit(world.pool, "sub_shorten") == []


async def test_a_shorten_post_changes_dates_audits_and_tells_the_user(world):
    vip = await sub(world.pool, world.plans["vip"], start=0, end=30)
    prem = await sub(world.pool, world.plans["prem"], start=30, end=60)
    old = [await row(world.pool, s) for s in (vip, prem)]
    exp = expected_in(await preview(world, vip, 10))
    r = await world.post(
        f"/admin/subscriptions/{vip}/shorten",
        {"days": "10", "expected": exp, "reason": "approved by mistake", "confirm": "1"},
    )
    assert r.status_code == 303 and r.headers["location"].endswith(
        f"/admin/users/{U}?msg=sub_shortened"
    )
    new = [await row(world.pool, s) for s in (vip, prem)]
    assert new[0]["current_period_end"] == old[0]["current_period_end"] - timedelta(days=10)
    assert new[1]["started_at"] == new[0]["current_period_end"]
    (a,) = await audit(world.pool, "sub_shorten")
    assert (a["admin_id"], a["subscription_id"], a["reason"]) == (ADMIN, vip, "approved by mistake")
    (text,) = told(world)
    assert "now ends on" in text and "refunded" in text  # honest about money


async def test_a_double_submit_of_the_same_form_removes_the_time_once(world):
    vip = await sub(world.pool, world.plans["vip"])
    exp = expected_in(await preview(world, vip, 10))
    data = {"days": "10", "expected": exp, "reason": "approved by mistake", "confirm": "1"}
    first = await world.post(f"/admin/subscriptions/{vip}/shorten", data)
    second = await world.post(f"/admin/subscriptions/{vip}/shorten", data)
    assert first.headers["location"].endswith("sub_shortened")
    assert "sub_changed" in second.headers["location"]
    assert len(await audit(world.pool, "sub_shorten")) == 1 and len(told(world)) == 1


@pytest.mark.parametrize(
    "data, msg",
    [
        ({"days": "10", "reason": "approved by mistake"}, "sub_confirm"),
        ({"days": "10", "confirm": "1", "reason": "ab"}, "sub_reason"),
        ({"days": "999", "confirm": "1", "reason": "approved by mistake"}, "sub_too_much"),
        ({"days": "0", "confirm": "1", "reason": "approved by mistake"}, "sub_bad_days"),
        ({"days": "abc", "confirm": "1", "reason": "approved by mistake"}, "sub_bad_days"),
        ({"days": "9" * 40, "confirm": "1", "reason": "approved by mistake"}, "sub_bad_days"),
    ],
)
async def test_bad_input_changes_nothing(world, data, msg):
    vip = await sub(world.pool, world.plans["vip"])
    before = await row(world.pool, vip)
    exp = expected_in(await preview(world, vip, 5))
    r = await world.post(f"/admin/subscriptions/{vip}/shorten", {**data, "expected": exp})
    assert r.status_code == 303 and msg in r.headers["location"]
    assert await row(world.pool, vip) == before and told(world) == []
    assert await audit(world.pool, "sub_shorten") == []


async def test_shorten_pages_need_an_admin_session_csrf_and_our_origin(world):
    vip = await sub(world.pool, world.plans["vip"])
    exp = expected_in(await preview(world, vip, 5))
    data = {"days": "5", "expected": exp, "reason": "approved by mistake", "confirm": "1"}
    path = f"/admin/subscriptions/{vip}/shorten"
    assert (await world.post(path, data, with_csrf=False)).status_code == 403
    r = await world.post(path, data, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    world.client.cookies.clear()
    assert (await world.client.get(f"{path}?days=5")).status_code == 303
    assert (await world.client.post(path, data=data)).status_code == 403
    assert await audit(world.pool, "sub_shorten") == []


async def test_unknown_or_ended_subscriptions_are_handled(world):
    assert (await world.client.get("/admin/subscriptions/999999/shorten")).status_code == 404
    ended = await sub(world.pool, world.plans["vip"], start=-40, end=-10)
    html = (await world.client.get(f"/admin/subscriptions/{ended}/shorten?days=3")).text
    assert "nothing to adjust" in html and 'name="confirm"' not in html


async def test_text_in_the_query_is_escaped(world):
    vip = await sub(world.pool, world.plans["vip"])
    r = await world.client.get(f"/admin/subscriptions/{vip}/shorten?days=%3Cscript%3E")
    assert r.status_code == 200 and "<script>" not in r.text


# ------------------------------------------------------------------ remove one payment's extension


async def buy(admin, plan, uid=U):
    svc = admin.state.services
    order = (await svc.payments.start(uid, plan)).order
    await svc.payments.attach_evidence(uid, order.payment_id, img.JPEG)
    assert (await svc.payments.submit(uid, order.payment_id, None)).kind == "ok"
    assert (await svc.payments.approve(ADMIN, order.payment_id)).kind == "ok"
    return order.payment_id


async def test_the_payment_page_offers_removal_only_for_an_extension(world):
    created = await buy(world, world.plans["prem"])
    extension = await buy(world, world.plans["prem"])
    page = (await world.client.get(f"/admin/payments/{created}")).text
    assert "remove-extension" not in page  # it created the subscription: not provable
    page = (await world.client.get(f"/admin/payments/{extension}")).text
    assert f"/admin/payments/{extension}/remove-extension" in page


async def test_removing_an_extension_end_to_end(world):
    created = await buy(world, world.plans["prem"])
    extension = await buy(world, world.plans["prem"])
    sid = await world.pool.fetchval("SELECT subscription_id FROM payments WHERE id=$1", created)
    before = await row(world.pool, sid)
    payments_before = [
        dict(r) for r in await world.pool.fetch("SELECT * FROM payments ORDER BY id")
    ]
    html = (await world.client.get(f"/admin/payments/{extension}/remove-extension")).text
    assert "exactly 30 days" in html
    r = await world.post(
        f"/admin/payments/{extension}/remove-extension",
        {"expected": expected_in(html), "reason": "entered twice", "confirm": "1"},
    )
    assert r.status_code == 303 and "sub_shortened" in r.headers["location"]
    assert (await row(world.pool, sid))["current_period_end"] == before[
        "current_period_end"
    ] - timedelta(days=30)
    assert [
        dict(r) for r in await world.pool.fetch("SELECT * FROM payments ORDER BY id")
    ] == payments_before
    (a,) = await audit(world.pool, "sub_shorten")
    assert a["payment_id"] == extension and a["reason"] == "entered twice"
    again = (await world.client.get(f"/admin/payments/{extension}/remove-extension")).text
    assert "already removed" in again and 'name="confirm"' not in again
    page = (await world.client.get(f"/admin/payments/{extension}")).text
    assert "was <b>removed</b>" in page


async def test_the_creating_payment_is_explained_not_removable(world):
    created = await buy(world, world.plans["prem"])
    await buy(world, world.plans["prem"])
    html = (await world.client.get(f"/admin/payments/{created}/remove-extension")).text
    assert "not guessed" in html and 'name="confirm"' not in html
    r = await world.post(
        f"/admin/payments/{created}/remove-extension",
        {"expected": "", "reason": "remove the first one", "confirm": "1"},
    )
    assert "sub_ambiguous" in r.headers["location"]
    assert await audit(world.pool, "sub_shorten") == []


async def test_remove_extension_needs_csrf(world):
    await buy(world, world.plans["prem"])
    extension = await buy(world, world.plans["prem"])
    r = await world.post(
        f"/admin/payments/{extension}/remove-extension",
        {"expected": "x", "reason": "entered twice", "confirm": "1"},
        with_csrf=False,
    )
    assert r.status_code == 403 and await audit(world.pool, "sub_shorten") == []
    assert (await world.client.get("/admin/payments/999999/remove-extension")).status_code == 404


# ------------------------------------------------------------------ grant


async def test_the_grant_page_previews_and_changes_nothing(world):
    r = await world.client.get(f"/admin/users/{U}/grant")
    assert r.status_code == 200 and "VIP" in r.text and 'name="confirm"' not in r.text
    r = await world.client.get(f"/admin/users/{U}/grant?plan={world.plans['vip']}&days=20")
    assert "A new subscription starts now" in r.text and 'name="confirm"' in r.text
    assert await world.pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    assert (await world.client.get("/admin/users/424242/grant")).status_code == 404


async def test_a_grant_end_to_end_without_any_payment(world):
    plan = world.plans["vip"]
    html = (await world.client.get(f"/admin/users/{U}/grant?plan={plan}&days=20")).text
    r = await world.post(
        f"/admin/users/{U}/grant",
        {
            "plan": plan,
            "days": "20",
            "expected": expected_in(html),
            "reason": "compensation for a lost payment",
            "confirm": "1",
        },
    )
    assert r.status_code == 303 and r.headers["location"].endswith(
        f"/admin/users/{U}?msg=sub_granted"
    )
    (s,) = [dict(x) for x in await world.pool.fetch("SELECT * FROM subscriptions")]
    assert s["provider"] == "admin_grant" and s["plan_id"] == plan
    assert await world.pool.fetchval("SELECT count(*) FROM payments") == 0
    (a,) = await audit(world.pool, "sub_grant")
    assert a["reason"] == "compensation for a lost payment" and a["admin_id"] == ADMIN
    (text,) = told(world)
    assert "gave you" in text and "VIP" in text
    page = (await world.client.get(f"/admin/users/{U}")).text
    assert "Admin grant (no payment)" in page


async def test_a_queued_grant_tells_the_user_when_it_starts(world):
    await sub(world.pool, world.plans["vip"], start=0, end=30)
    plan = world.plans["prem"]
    html = (await world.client.get(f"/admin/users/{U}/grant?plan={plan}&days=10")).text
    await world.post(
        f"/admin/users/{U}/grant",
        {
            "plan": plan,
            "days": "10",
            "expected": expected_in(html),
            "reason": "a promised bonus",
            "confirm": "1",
        },
    )
    (text,) = told(world)
    assert "starts on" in text


async def test_grant_double_submit_and_bad_input(world):
    plan = world.plans["vip"]
    html = (await world.client.get(f"/admin/users/{U}/grant?plan={plan}&days=5")).text
    data = {
        "plan": plan,
        "days": "5",
        "expected": expected_in(html),
        "reason": "a promised bonus",
        "confirm": "1",
    }
    assert "sub_granted" in (await world.post(f"/admin/users/{U}/grant", data)).headers["location"]
    assert "sub_changed" in (await world.post(f"/admin/users/{U}/grant", data)).headers["location"]
    assert await world.pool.fetchval("SELECT count(*) FROM subscriptions") == 1
    seen_v = expected_in(
        (await world.client.get(f"/admin/users/{V}/grant?plan={plan}&days=5")).text
    )
    for bad, msg in (
        ({"confirm": ""}, "sub_confirm"),
        ({"reason": "ab"}, "sub_reason"),
        ({"days": "0"}, "sub_bad_days"),
        ({"plan": "999999"}, "sub_plan_unavailable"),
    ):
        r = await world.post(f"/admin/users/{V}/grant", {**data, "expected": seen_v, **bad})
        assert msg in r.headers["location"], bad
    assert await world.pool.fetchval("SELECT count(*) FROM subscriptions") == 1


async def test_grant_needs_a_session_csrf_and_a_real_user(world):
    plan = world.plans["vip"]
    data = {
        "plan": plan,
        "days": "5",
        "expected": "x",
        "reason": "a promised bonus",
        "confirm": "1",
    }
    assert (await world.post(f"/admin/users/{U}/grant", data, with_csrf=False)).status_code == 403
    r = await world.post(
        f"/admin/users/{U}/grant", data, headers={"origin": "https://evil.example"}
    )
    assert r.status_code == 403
    r = await world.post("/admin/users/424242/grant", data)
    assert "missing" in r.headers["location"]
    world.client.cookies.clear()
    assert (await world.client.get(f"/admin/users/{U}/grant")).status_code == 303
    assert await world.pool.fetchval("SELECT count(*) FROM subscriptions") == 0


async def test_the_user_page_shows_the_links_and_extension_history(world):
    created = await buy(world, world.plans["prem"])
    extension = await buy(world, world.plans["prem"])
    sid = await world.pool.fetchval("SELECT subscription_id FROM payments WHERE id=$1", created)
    page = (await world.client.get(f"/admin/users/{U}")).text
    assert page.count(f"/admin/subscriptions/{sid}/shorten") == 1  # one row per subscription
    assert f"/admin/payments/{extension}/remove-extension" in page and "(created it)" in page
    assert f"/admin/users/{U}/grant" in page

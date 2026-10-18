"""Admin → user detail → Subscription section (view only), and the Payment settings page layout."""

import re

import pytest

from tests.factories import give_tier, make_plan, make_user
from tests.web.conftest import ADMIN, OTHER_ADMIN

U = 4001


def section(html: str) -> str:
    """The Subscription section of the user page (from its heading to the next heading)."""
    start = html.index("<h2>Subscription</h2>")
    end = html.index("<h2>Reports about this user</h2>")
    return html[start:end]


async def page(admin, uid=U):
    r = await admin.client.get(f"/admin/users/{uid}")
    assert r.status_code == 200
    return r.text


@pytest.fixture
async def user(admin):
    await make_user(admin.pool, U)
    return U


async def test_a_user_without_a_subscription_is_shown_as_free(admin, user):
    sec = section(await page(admin))
    assert "No subscription: this user is on the Free plan." in sec
    assert "<table>" not in sec


async def test_an_active_subscription_is_listed_with_plan_tier_dates_and_status(admin, user):
    await give_tier(admin.pool, U, "vip", days=30)
    sec = section(await page(admin))
    assert "<h3>Current</h3>" in sec and "<h3>Queued" not in sec
    assert "Vip" in sec  # the plan name
    assert ">VIP<" in sec  # the tier, from the plan's badge level
    assert ">Active<" in sec and "no end date" not in sec
    ends = await admin.pool.fetchval("SELECT current_period_end FROM subscriptions")
    assert ends.strftime("%Y-%m-%d") in sec


async def test_a_queued_subscription_is_listed_apart_from_the_running_one(admin, user):
    await give_tier(admin.pool, U, "vip", days=30)
    queued = await give_tier(admin.pool, U, "premium", days=60)
    await admin.pool.execute(
        "UPDATE subscriptions SET started_at = now() + interval '30 days' WHERE id = $1", queued
    )
    sec = section(await page(admin))
    assert sec.index("<h3>Current</h3>") < sec.index("<h3>Queued (starts later)</h3>")
    current, later = sec.split("<h3>Queued (starts later)</h3>")
    assert "Vip" in current and "Premium" not in current
    assert "Premium" in later and ">Queued<" in later and "Vip" not in later


async def test_ended_and_canceled_subscriptions_are_history(admin, user):
    old = await give_tier(admin.pool, U, "pro", days=30)
    await admin.pool.execute(
        "UPDATE subscriptions SET started_at = now() - interval '40 days', "
        "current_period_end = now() - interval '10 days', status = 'expired' WHERE id = $1",
        old,
    )
    gone = await give_tier(admin.pool, U, "premium", days=30)
    await admin.pool.execute("UPDATE subscriptions SET status = 'canceled' WHERE id = $1", gone)
    sec = section(await page(admin))
    assert "<h3>Past</h3>" in sec and "<h3>Current</h3>" not in sec
    assert ">Ended<" in sec and "(expired)" in sec and "(canceled)" in sec


async def test_the_payment_that_created_a_subscription_is_linked(admin, user):
    plan = await make_plan(admin.pool, "pay_prem")
    sub = await admin.pool.fetchval(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end, provider, provider_ref) "
        "VALUES ($1, $2, now() + interval '30 days', 'manual_upi', 'PAY-ABCDEFGHIJ') RETURNING id",
        U,
        plan,
    )
    await admin.pool.execute(
        "INSERT INTO payments (reference, user_id, plan_id, amount_minor, currency, duration_days,"
        " pay_to, status, utr, submitted_at, reviewed_by, reviewed_at, subscription_id) "
        "VALUES ('PAY-ABCDEFGHIJ', $1, $2, 9900, 'INR', 30, 'a@b', 'approved', 'SECRETUTR12345', "
        "now(), 1, now(), $3)",
        U,
        plan,
        sub,
    )
    pid = await admin.pool.fetchval("SELECT id FROM payments")
    sec = section(await page(admin))
    assert f'href="/admin/payments/{pid}"' in sec and "PAY-ABCDEFGHIJ" in sec
    assert "Paid (manual UPI)" in sec
    # nothing more about the payment than its id and reference
    for secret in ("SECRETUTR12345", "99.00", "a@b", "9900"):
        assert secret not in sec


async def test_a_subscription_without_a_payment_shows_a_dash(admin, user):
    await give_tier(admin.pool, U, "pro")
    sec = section(await page(admin))
    assert "<td>—</td>" in sec and "/admin/payments/" not in sec


async def test_a_subscription_with_no_end_date_says_so(admin, user):
    sub = await give_tier(admin.pool, U, "premium")
    await admin.pool.execute(
        "UPDATE subscriptions SET current_period_end = NULL WHERE id = $1", sub
    )
    sec = section(await page(admin))
    assert "no end date" in sec and "<h3>Current</h3>" in sec


async def test_the_section_has_no_action_buttons_yet(admin, user):
    await give_tier(admin.pool, U, "vip")
    sec = section(await page(admin))
    assert "<form" not in sec and "<button" not in sec
    assert "View only" in sec


async def test_only_this_users_subscriptions_are_shown(admin, user):
    await make_user(admin.pool, U + 1)
    await give_tier(admin.pool, U + 1, "vip")
    assert "No subscription" in section(await page(admin, U))


async def test_the_page_needs_an_admin_session(web):
    await make_user(web.pool, U)
    r = await web.client.get(f"/admin/users/{U}")
    assert r.status_code in (303, 401, 403)
    assert "Subscription" not in r.text


async def test_a_non_admin_session_cannot_open_the_page(admin, user):
    admin.state.sessions._admin_ids = frozenset({OTHER_ADMIN + 5})
    r = await admin.client.get(f"/admin/users/{U}")
    assert r.status_code in (303, 401, 403) and "Subscription" not in r.text


async def test_the_service_returns_nothing_to_a_non_admin(admin, user):
    assert await admin.state.services.admin.user_detail(OTHER_ADMIN + 5, U) is None


async def test_an_unknown_user_is_a_404(admin):
    assert (await admin.client.get("/admin/users/99999")).status_code == 404


# ---------------------------------------------------------------- audit foundation


async def test_the_new_audit_actions_are_accepted_and_old_ones_still_are(admin):
    for action in ("sub_grant", "sub_cancel", "sub_extend", "sub_revoke", "ban", "payment_approve"):
        await admin.pool.execute(
            "INSERT INTO admin_actions (admin_id, action) VALUES ($1, $2)", ADMIN, action
        )
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        await admin.pool.execute(
            "INSERT INTO admin_actions (admin_id, action) VALUES ($1, 'made_up')", ADMIN
        )


async def test_the_audit_page_can_filter_on_the_new_actions(admin):
    from bot.services.admin import AUDIT_ACTIONS

    assert {"sub_grant", "sub_cancel", "sub_extend", "sub_revoke"} <= set(AUDIT_ACTIONS)
    await admin.pool.execute(
        "INSERT INTO admin_actions (admin_id, action, target_user_id, detail) "
        "VALUES ($1, 'sub_grant', 1, 'test entry')",
        ADMIN,
    )
    html = (await admin.client.get("/admin/audit?action=sub_grant")).text
    assert "sub_grant" in html and "test entry" in html


# ---------------------------------------------------------------- Payment settings layout (UI bug)


def parts(html: str) -> tuple[str, str]:
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    body = html[html.index("<body") :]
    return title, body


async def test_the_title_is_plain_text_and_unchanged(admin):
    title, _ = parts((await admin.client.get("/admin/payment-settings")).text)
    assert title == "Payment settings · Bot admin"
    assert "<" not in title


async def test_the_automatic_approval_card_is_in_the_body_not_the_title(admin):
    html = (await admin.client.get("/admin/payment-settings")).text
    title, body = parts(html)
    assert "Automatic payment approval" not in title
    assert html.count("Automatic payment approval") == 1
    assert "<h2>Automatic payment approval</h2>" in body
    assert 'action="/admin/payment-settings/auto-approve"' in body
    assert 'name="confirm"' in body and "Turn ON" in body and "Manual approval" in body


async def test_the_card_follows_the_state_and_sits_after_the_save_form(admin):
    from tests.factories import enable_payments

    await enable_payments(admin.pool)
    await admin.post("/admin/payment-settings/auto-approve", {"mode": "on", "confirm": "1"})
    _, body = parts((await admin.client.get("/admin/payment-settings")).text)
    assert "Turn OFF" in body and "Eligible verified payments may be auto-approved" in body
    assert body.index('action="/admin/payment-settings"') < body.index("Automatic payment approval")
    await admin.post("/admin/payment-settings/auto-approve", {"mode": "off"})
    _, body = parts((await admin.client.get("/admin/payment-settings")).text)
    assert "Turn ON" in body and "Turn OFF" not in body


async def test_rendering_the_page_does_not_change_the_setting(admin):
    before = await admin.pool.fetchval("SELECT auto_approve FROM payment_settings")
    await admin.client.get("/admin/payment-settings")
    assert await admin.pool.fetchval("SELECT auto_approve FROM payment_settings") == before

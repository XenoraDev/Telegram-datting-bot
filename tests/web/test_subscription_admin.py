"""S2: cancel a subscription (running or queued) and revoke an approved payment's subscription."""

import asyncio

import pytest

from tests import payment_images as img
from tests.factories import enable_payments, give_tier, make_plan, make_user
from tests.web.conftest import ADMIN, OTHER_ADMIN

U, V = 5001, 5002


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@pytest.fixture
async def world(admin):
    for uid in (U, V):
        await make_user(admin.pool, uid)
    admin.state.bot = FakeBot()
    return admin


def told(admin, uid):
    return [t for c, t in admin.state.bot.sent if c == uid]


async def sub_rows(admin, uid=U):
    return [
        dict(r)
        for r in await admin.pool.fetch(
            "SELECT * FROM subscriptions WHERE user_id=$1 ORDER BY id", uid
        )
    ]


async def audit(admin, action):
    return [
        dict(r)
        for r in await admin.pool.fetch("SELECT * FROM admin_actions WHERE action=$1", action)
    ]


async def cancel(admin, sid, reason="chargeback on the card", confirm="1", **kw):
    data = {"reason": reason, **({"confirm": confirm} if confirm else {})}
    return await admin.post(f"/admin/subscriptions/{sid}/cancel", data, **kw)


async def queued_premium(admin, uid=U):
    """VIP running + Premium queued behind it. Returns (vip_id, premium_id)."""
    vip = await give_tier(admin.pool, uid, "vip", days=30)
    prem = await give_tier(admin.pool, uid, "premium", days=60)
    await admin.pool.execute(
        "UPDATE subscriptions SET started_at = now() + interval '30 days' WHERE id = $1", prem
    )
    return vip, prem


# ------------------------------------------------------------------ cancel a running subscription


async def test_cancelling_a_running_subscription_keeps_the_row_and_its_dates(world):
    sid = await give_tier(world.pool, U, "pro", days=30)
    before = (await sub_rows(world))[0]
    r = await cancel(world, sid)
    assert r.status_code == 303 and r.headers["location"] == f"/admin/users/{U}?msg=sub_canceled"
    (after,) = await sub_rows(world)
    assert after["status"] == "canceled"
    for col in (
        "id",
        "user_id",
        "plan_id",
        "started_at",
        "current_period_end",
        "provider",
        "created_at",
    ):
        assert after[col] == before[col], col
    assert await world.pool.fetchval("SELECT count(*) FROM subscriptions") == 1  # nothing deleted


async def test_the_confirmation_page_shows_the_subscription_and_changes_nothing(world):
    sid = await give_tier(world.pool, U, "vip")
    html = (await world.client.get(f"/admin/subscriptions/{sid}/cancel")).text
    assert f"Cancel subscription #{sid}?" in html and 'name="reason"' in html
    assert 'name="confirm"' in html and "No money is refunded" in html
    assert (await sub_rows(world))[0]["status"] == "active"
    assert await audit(world, "sub_cancel") == []


async def test_the_user_page_offers_cancel_for_running_and_queued_but_not_past(world):
    vip, prem = await queued_premium(world)
    old = await give_tier(world.pool, U, "pro")
    await world.pool.execute(
        "UPDATE subscriptions SET started_at = now() - interval '40 days', "
        "current_period_end = now() - interval '10 days', status = 'expired' WHERE id = $1",
        old,
    )
    html = (await world.client.get(f"/admin/users/{U}")).text
    assert f'href="/admin/subscriptions/{vip}/cancel"' in html
    assert f'href="/admin/subscriptions/{prem}/cancel"' in html
    assert f'href="/admin/subscriptions/{old}/cancel"' not in html


async def test_the_user_is_told_in_english_without_admin_details_or_a_refund_claim(world):
    sid = await give_tier(world.pool, U, "pro")
    await cancel(world, sid, reason="internal note xyz-secret")
    (text,) = told(world, U)
    assert "Pro" in text and "ended by our team" in text and "/support" in text
    assert "does not mean any payment was refunded" in text
    for leak in ("xyz-secret", str(ADMIN), "admin", "internal"):
        assert leak not in text.lower()


async def test_the_user_is_told_in_hindi_when_that_is_their_language(world):
    await world.pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", U)
    sid = await give_tier(world.pool, U, "pro")
    await cancel(world, sid)
    (text,) = told(world, U)
    assert "हमारी टीम ने बंद कर दिया" in text and "/support" in text and "refund" not in text.lower()


# ------------------------------------------------------------------ queued


async def test_cancelling_a_queued_subscription_leaves_the_running_one_alone(world):
    vip, prem = await queued_premium(world)
    other_user_sub = await give_tier(world.pool, V, "premium")
    before = {r["id"]: r for r in await sub_rows(world)}
    r = await cancel(world, prem)
    assert r.headers["location"].endswith("?msg=sub_canceled")
    after = {r["id"]: r for r in await sub_rows(world)}
    assert after[prem]["status"] == "canceled" and after[vip] == before[vip]
    assert after[prem]["started_at"] == before[prem]["started_at"]  # history kept
    assert (await sub_rows(world, V))[0]["id"] == other_user_sub
    assert (await sub_rows(world, V))[0]["status"] == "active"
    # it can never start by itself: nothing live is left in the future
    assert (
        await world.pool.fetchval(
            "SELECT count(*) FROM subscriptions "
            "WHERE user_id=$1 AND status='active' AND started_at > now()",
            U,
        )
        == 0
    )
    (text,) = told(world, U)
    assert (
        "upcoming" in text and "will not start" in text and "current plan is not affected" in text
    )


async def test_cancelling_the_running_one_does_not_touch_the_queued_one(world):
    vip, prem = await queued_premium(world)
    before = {r["id"]: r for r in await sub_rows(world)}
    await cancel(world, vip)
    after = {r["id"]: r for r in await sub_rows(world)}
    assert after[vip]["status"] == "canceled" and after[prem] == before[prem]
    html = (await world.client.get(f"/admin/subscriptions/{vip}/cancel")).text
    assert "Already ended" in html  # and the page now says there is nothing left to cancel


async def test_the_confirmation_page_warns_that_a_queued_plan_still_starts(world):
    vip, _ = await queued_premium(world)
    assert (
        "a queued one still starts on its own date"
        in (await world.client.get(f"/admin/subscriptions/{vip}/cancel")).text
    )


# ------------------------------------------------------------------ reason, confirmation


@pytest.mark.parametrize("reason", ["", "  ", "ab", "\n\n"])
async def test_a_reason_is_mandatory(world, reason):
    sid = await give_tier(world.pool, U, "pro")
    r = await cancel(world, sid, reason=reason)
    assert r.headers["location"].endswith(f"/admin/subscriptions/{sid}/cancel?msg=sub_reason")
    assert (await sub_rows(world))[0]["status"] == "active"
    assert await audit(world, "sub_cancel") == [] and told(world, U) == []


async def test_the_confirmation_is_checked_on_the_server(world):
    sid = await give_tier(world.pool, U, "pro")
    r = await cancel(world, sid, confirm="")
    assert r.headers["location"].endswith("?msg=sub_confirm")
    r = await cancel(world, sid, confirm="0")
    assert r.headers["location"].endswith("?msg=sub_confirm")
    assert (await sub_rows(world))[0]["status"] == "active"
    assert await audit(world, "sub_cancel") == []


# ------------------------------------------------------------------ security


async def test_a_cancel_needs_a_session(web):
    await make_user(web.pool, U)
    sid = await give_tier(web.pool, U, "pro")
    r = await web.client.post(
        f"/admin/subscriptions/{sid}/cancel", data={"reason": "abcdef", "confirm": "1"}
    )
    assert r.status_code in (303, 401, 403)
    assert (await web.pool.fetchval("SELECT status FROM subscriptions")) == "active"


async def test_a_cancel_needs_the_csrf_token(world):
    sid = await give_tier(world.pool, U, "pro")
    r = await cancel(world, sid, with_csrf=False)
    assert r.status_code == 403
    assert (await sub_rows(world))[0]["status"] == "active" and await audit(
        world, "sub_cancel"
    ) == []


async def test_a_get_never_changes_anything(world):
    sid = await give_tier(world.pool, U, "pro")
    for path in (f"/admin/subscriptions/{sid}/cancel",):
        await world.client.get(path + "?reason=abcdef&confirm=1")
    assert (await sub_rows(world))[0]["status"] == "active"


async def test_a_non_admin_session_changes_nothing(world):
    sid = await give_tier(world.pool, U, "pro")
    world.state.sessions._admin_ids = frozenset({OTHER_ADMIN + 7})
    r = await world.client.post(
        f"/admin/subscriptions/{sid}/cancel",
        data={"reason": "abcdef", "confirm": "1", "csrf_token": world.token()},
    )
    assert r.status_code in (303, 403)
    assert (await sub_rows(world))[0]["status"] == "active" and await audit(
        world, "sub_cancel"
    ) == []


async def test_the_service_itself_refuses_a_non_admin(world):
    sid = await give_tier(world.pool, U, "pro")
    res = await world.state.services.subs.cancel(U, sid, "a good reason", confirmed=True)
    assert res.kind == "denied" and (await sub_rows(world))[0]["status"] == "active"
    assert await world.state.services.subs.preview_cancel(U, sid) is None


async def test_the_form_has_no_user_id_so_the_owner_comes_from_the_subscription(world):
    mine = await give_tier(world.pool, U, "pro")
    theirs = await give_tier(world.pool, V, "pro")
    await world.post(
        f"/admin/subscriptions/{mine}/cancel",
        {
            "reason": "abcdef",
            "confirm": "1",
            "user_id": str(V),
            "uid": str(V),
            "sub_id": str(theirs),
        },
    )
    assert (await sub_rows(world, U))[0]["status"] == "canceled"
    assert (await sub_rows(world, V))[0]["status"] == "active"  # a forged field changed nothing
    assert told(world, V) == []


async def test_an_unknown_subscription_is_a_404_and_a_safe_redirect(world):
    assert (await world.client.get("/admin/subscriptions/999999/cancel")).status_code == 404
    r = await cancel(world, 999999)
    assert r.status_code == 303 and r.headers["location"].endswith("?msg=missing")
    assert (await world.client.get("/admin/subscriptions/0/cancel")).status_code in (404, 422)


# ------------------------------------------------------------------ idempotency, races


async def test_a_double_submit_cancels_once_and_audits_once(world):
    sid = await give_tier(world.pool, U, "pro")
    first = await cancel(world, sid)
    second = await cancel(world, sid)
    assert first.headers["location"].endswith("sub_canceled")
    assert second.headers["location"].endswith("?msg=sub_not_active")
    assert len(await audit(world, "sub_cancel")) == 1 and len(told(world, U)) == 1


async def test_simultaneous_cancels_cancel_once(world):
    sid = await give_tier(world.pool, U, "pro")
    svc = world.state.services.subs
    results = await asyncio.gather(
        *(svc.cancel(ADMIN, sid, "same reason twice", confirmed=True) for _ in range(5))
    )
    assert sorted(r.kind for r in results) == ["not_active"] * 4 + ["ok"]
    assert len(await audit(world, "sub_cancel")) == 1


async def test_an_ended_subscription_cannot_be_cancelled_and_leaves_no_audit(world):
    sid = await give_tier(world.pool, U, "pro")
    await world.pool.execute(
        "UPDATE subscriptions SET started_at = now() - interval '40 days', "
        "current_period_end = now() - interval '1 day' WHERE id = $1",
        sid,
    )
    r = await cancel(world, sid)
    assert r.headers["location"].endswith("?msg=sub_not_active")
    assert (await sub_rows(world))[0]["status"] == "active"  # untouched: it simply ended
    assert await audit(world, "sub_cancel") == []


async def test_the_audit_entry_has_everything(world):
    sid = await give_tier(world.pool, U, "pro")
    await cancel(world, sid, reason="chargeback on the card")
    (a,) = await audit(world, "sub_cancel")
    assert (a["admin_id"], a["target_user_id"], a["subscription_id"], a["payment_id"]) == (
        ADMIN,
        U,
        sid,
        None,
    )
    assert a["reason"] == "chargeback on the card" and a["created_at"] is not None
    assert "Pro" in a["detail"] and "chargeback" in a["detail"]
    html = (await world.client.get("/admin/audit?action=sub_cancel")).text
    assert "sub_cancel" in html and "chargeback on the card" in html


# ------------------------------------------------------------------ revoke an approved payment


async def approved(world, uid=U, plan_code="rv_premium", tier="premium"):
    await enable_payments(world.pool)
    plan = await make_plan(world.pool, plan_code, name=f"Plan {plan_code}", tier=tier)
    svc = world.state.services.payments
    pid = (await svc.start(uid, plan)).order.payment_id
    await svc.attach_evidence(uid, pid, img.JPEG)
    assert (await svc.submit(uid, pid, None)).kind == "ok"
    assert (await svc.approve(ADMIN, pid)).kind == "ok"
    return pid, plan


async def payment_row(world, pid):
    return dict(await world.pool.fetchrow("SELECT * FROM payments WHERE id=$1", pid))


async def revoke(world, pid, reason="approved by mistake", confirm="1", **kw):
    data = {"reason": reason, **({"confirm": confirm} if confirm else {})}
    return await world.post(f"/admin/payments/{pid}/revoke", data, **kw)


async def test_revoking_cancels_exactly_the_subscription_that_payment_created(world):
    other = await give_tier(world.pool, V, "vip")  # someone else's subscription
    pid, _ = await approved(world)
    sub_id = await world.pool.fetchval("SELECT subscription_id FROM payments WHERE id=$1", pid)
    before_payment = await payment_row(world, pid)
    r = await revoke(world, pid)
    assert r.headers["location"] == f"/admin/payments/{pid}?msg=sub_revoked"
    assert (await sub_rows(world))[0]["id"] == sub_id and (await sub_rows(world))[0][
        "status"
    ] == "canceled"
    assert (await sub_rows(world, V))[0]["id"] == other and (await sub_rows(world, V))[0][
        "status"
    ] == "active"
    assert await payment_row(world, pid) == before_payment  # the payment row is untouched
    assert before_payment["status"] == "approved"
    assert await world.pool.fetchval("SELECT count(*) FROM subscriptions WHERE user_id=$1", U) == 1


async def test_revoke_does_not_touch_the_users_other_subscriptions(world):
    await give_tier(world.pool, U, "premium")  # running; an upgrade purchase will end it
    old = await give_tier(world.pool, U, "premium", days=5)
    await world.pool.execute(
        "UPDATE subscriptions SET started_at = now() - interval '40 days', "
        "current_period_end = now() - interval '35 days', status = 'expired' WHERE id = $1",
        old,
    )
    pid, _ = await approved(world, plan_code="rv_pro", tier="pro")  # upgrade
    sub_id = await world.pool.fetchval("SELECT subscription_id FROM payments WHERE id=$1", pid)
    others_before = {r["id"]: r for r in await sub_rows(world) if r["id"] != sub_id}
    assert others_before  # the earlier ones exist
    assert (await revoke(world, pid)).headers["location"].endswith("sub_revoked")
    rows = {r["id"]: r for r in await sub_rows(world)}
    assert rows[sub_id]["status"] == "canceled"
    assert {k: v for k, v in rows.items() if k != sub_id} == others_before  # no one else changed


async def test_revoke_audit_records_payment_and_subscription(world):
    pid, _ = await approved(world)
    sub_id = await world.pool.fetchval("SELECT subscription_id FROM payments WHERE id=$1", pid)
    await revoke(world, pid, reason="approved by mistake")
    (a,) = await audit(world, "sub_revoke")
    assert (a["admin_id"], a["target_user_id"], a["subscription_id"], a["payment_id"]) == (
        ADMIN,
        U,
        sub_id,
        pid,
    )
    assert a["reason"] == "approved by mistake" and a["created_at"] is not None
    ref = await world.pool.fetchval("SELECT reference FROM payments WHERE id=$1", pid)
    assert ref in a["detail"]
    assert await audit(world, "sub_cancel") == []


async def test_revoke_tells_the_user_without_claiming_a_refund(world):
    pid, _ = await approved(world)
    ref = await world.pool.fetchval("SELECT reference FROM payments WHERE id=$1", pid)
    told_before = len(told(world, U))
    await revoke(world, pid, reason="internal note zz-secret")
    text = told(world, U)[told_before]
    assert (
        ref in text
        and "approval was withdrawn" in text
        and "no refund is made automatically" in text
    )
    assert (
        "zz-secret" not in text
        and str(ADMIN) not in text
        and "refunded" not in text.replace("no refund", "")
    )


async def test_revoke_notice_in_hindi(world):
    await world.pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", U)
    pid, _ = await approved(world)
    n = len(told(world, U))
    await revoke(world, pid)
    text = told(world, U)[n]
    assert "मंज़ूरी वापस ले ली गई" in text and "अपने-आप वापस नहीं किए जाते" in text


async def test_the_payment_page_offers_revoke_and_then_shows_it_was_revoked(world):
    pid, _ = await approved(world)
    html = (await world.client.get(f"/admin/payments/{pid}")).text
    assert f'href="/admin/payments/{pid}/revoke"' in html
    confirm = (await world.client.get(f"/admin/payments/{pid}/revoke")).text
    assert (
        "Revoke the approval of payment" in confirm and "No refund is made or recorded" in confirm
    )
    await revoke(world, pid)
    html = (await world.client.get(f"/admin/payments/{pid}")).text
    assert f'href="/admin/payments/{pid}/revoke"' not in html
    assert "revoked" in html and "no refund is recorded" in html


async def test_a_double_revoke_acts_once(world):
    pid, _ = await approved(world)
    first = await revoke(world, pid)
    second = await revoke(world, pid)
    assert first.headers["location"].endswith("sub_revoked")
    assert second.headers["location"].endswith("?msg=sub_not_active")
    assert len(await audit(world, "sub_revoke")) == 1


async def test_simultaneous_revokes_act_once(world):
    pid, _ = await approved(world)
    svc = world.state.services.subs
    results = await asyncio.gather(
        *(svc.revoke_payment(ADMIN, pid, "mistake approval", confirmed=True) for _ in range(5))
    )
    assert [r.kind for r in results].count("ok") == 1
    assert {r.kind for r in results} <= {"ok", "already_ended"}
    assert len(await audit(world, "sub_revoke")) == 1


async def test_a_pending_payment_cannot_be_revoked(world):
    await enable_payments(world.pool)
    plan = await make_plan(world.pool, "rv_pending")
    svc = world.state.services.payments
    pid = (await svc.start(U, plan)).order.payment_id
    await svc.attach_evidence(U, pid, img.JPEG)
    await svc.submit(U, pid, None)
    r = await revoke(world, pid)
    assert r.headers["location"].endswith("?msg=sub_not_approved")
    assert await audit(world, "sub_revoke") == []


async def test_an_unknown_payment_is_refused(world):
    assert (await world.client.get("/admin/payments/999999/revoke")).status_code == 404
    assert (await revoke(world, 999999)).headers["location"].endswith("?msg=missing")


async def test_a_payment_that_only_extended_a_subscription_is_refused(world):
    first, plan = await approved(world)
    svc = world.state.services.payments
    pid2 = (await svc.start(U, plan)).order.payment_id  # same plan again: extends the same row
    await svc.attach_evidence(U, pid2, img.PNG if hasattr(img, "PNG") else img.JPEG)
    await svc.submit(U, pid2, None)
    assert (await svc.approve(ADMIN, pid2)).kind == "ok"
    assert await world.pool.fetchval("SELECT count(*) FROM subscriptions WHERE user_id=$1", U) == 1
    rows_before = await sub_rows(world)
    for pid in (first, pid2):  # neither can be revoked: the subscription holds both payments' time
        r = await revoke(world, pid)
        assert r.headers["location"].endswith("?msg=sub_ambiguous"), pid
    assert await sub_rows(world) == rows_before
    assert await audit(world, "sub_revoke") == []
    assert "shared with other payments" in (await world.client.get(f"/admin/payments/{first}")).text


async def test_a_subscription_that_does_not_carry_the_payments_reference_is_refused(world):
    pid, _ = await approved(world)
    await world.pool.execute("UPDATE subscriptions SET provider_ref = 'SOMETHING-ELSE'")
    r = await revoke(world, pid)
    assert r.headers["location"].endswith("?msg=sub_ambiguous")
    assert (await sub_rows(world))[0]["status"] == "active" and await audit(
        world, "sub_revoke"
    ) == []


async def test_a_subscription_of_another_user_is_never_cancelled(world):
    pid, _ = await approved(world)
    await world.pool.execute("UPDATE subscriptions SET user_id = $1", V)
    r = await revoke(world, pid)
    assert r.headers["location"].endswith("?msg=sub_no_link")
    assert (await sub_rows(world, V))[0]["status"] == "active" and await audit(
        world, "sub_revoke"
    ) == []


async def test_revoke_needs_reason_confirmation_csrf_and_an_admin(world):
    pid, _ = await approved(world)
    sub_active = lambda: world.pool.fetchval("SELECT status FROM subscriptions WHERE user_id=$1", U)  # noqa: E731
    assert (
        (await revoke(world, pid, reason="ab"))
        .headers["location"]
        .endswith("/revoke?msg=sub_reason")
    )
    assert (
        (await revoke(world, pid, confirm=""))
        .headers["location"]
        .endswith("/revoke?msg=sub_confirm")
    )
    assert (await revoke(world, pid, with_csrf=False)).status_code == 403
    assert (
        await world.state.services.subs.revoke_payment(U, pid, "a good reason", confirmed=True)
        is not None
    )
    assert (
        await world.state.services.subs.revoke_payment(U, pid, "a good reason", confirmed=True)
    ).kind == "denied"
    world.state.sessions._admin_ids = frozenset({OTHER_ADMIN + 7})
    r = await world.client.post(
        f"/admin/payments/{pid}/revoke",
        data={"reason": "abcdef", "confirm": "1", "csrf_token": world.token()},
    )
    assert r.status_code in (303, 403)
    assert await sub_active() == "active" and await audit(world, "sub_revoke") == []


async def test_a_get_on_the_revoke_page_changes_nothing(world):
    pid, _ = await approved(world)
    await world.client.get(f"/admin/payments/{pid}/revoke?reason=abcdef&confirm=1")
    assert (await sub_rows(world))[0]["status"] == "active"


async def test_revoke_after_the_subscription_already_ended_is_a_safe_no_op(world):
    pid, _ = await approved(world)
    await world.pool.execute(
        "UPDATE subscriptions SET started_at = now() - interval '40 days', "
        "current_period_end = now() - interval '1 day'"
    )
    r = await revoke(world, pid)
    assert r.headers["location"].endswith("?msg=sub_not_active")
    assert await audit(world, "sub_revoke") == []


async def test_nothing_in_s2_can_grant_extend_or_refund(world):
    from web.app.main import create_app  # noqa: F401  (routes are enumerated below)

    paths = {getattr(r, "path", "") for r in world.app.routes}
    flat = " ".join(sorted(paths))
    for word in ("grant", "extend", "refund", "shorten", "delete"):
        assert word not in flat, word

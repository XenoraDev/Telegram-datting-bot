"""Automatic payment approval: OFF by default, ON only approves fully verified payments."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from bot.services import build_services
from tests import payment_images as tiny
from tests.factories import enable_payments, make_plan, make_user

U, U2, ADMIN, UPI = 8200, 8201, 424242, "Shop.Test@okbank"


def stamp() -> str:
    moment = datetime.now(UTC) + timedelta(hours=5, minutes=30) - timedelta(minutes=1)
    return moment.strftime("%d %b %Y, %I:%M %p")


def screen(amount="₹99", upi=UPI, utr="UPI Ref No: 412345678901", status="Payment Successful"):
    return f"{status}\n{amount}\nPaid to\n{upi}\n{utr}\n{stamp()}\n"


class FakeOcr:
    name = "fake"

    def __init__(self, text):
        self.text = text

    async def read(self, data, width, height):
        return self.text


async def setup(pool, clock, text, *, on=True, uid=U):
    await enable_payments(pool, UPI)
    await make_user(pool, uid)
    plan = await make_plan(pool, price_minor=9900)
    svc = build_services(pool, clock, clock, frozenset({ADMIN}), ocr=FakeOcr(text))
    started = await svc.payments.start(uid, plan)
    assert started.kind == "ok"
    pid = started.order.payment_id
    if on:
        assert (await svc.payments.set_auto_approve(ADMIN, True, confirmed=True)).kind == "ok"
    sub = await svc.payments.submit_screenshot(uid, tiny.PNG, None)
    assert sub.kind == "ok", sub
    return svc, pid, plan


async def row(pool, pid):
    return await pool.fetchrow("SELECT * FROM payments WHERE id = $1", pid)


async def audit(pool, action):
    return await pool.fetch("SELECT * FROM admin_actions WHERE action = $1", action)


# ------------------------------------------------------------------ the switch


async def test_the_switch_is_off_by_default(app_pool):
    assert await app_pool.fetchval("SELECT auto_approve FROM payment_settings") is False


async def test_an_admin_can_turn_it_on_and_off_and_it_is_audited(app_pool, clock):
    await enable_payments(app_pool, UPI)
    svc = build_services(app_pool, clock, clock, frozenset({ADMIN}))
    assert (await svc.payments.set_auto_approve(ADMIN, True, confirmed=True)).kind == "ok"
    assert (await svc.payments.get_settings(ADMIN))["auto_approve"] is True
    assert (await svc.payments.set_auto_approve(ADMIN, False)).kind == "ok"
    assert (await svc.payments.get_settings(ADMIN))["auto_approve"] is False
    details = [r["detail"] for r in await audit(app_pool, "payment_auto_setting")]
    assert sorted(details) == ["automatic payment approval OFF", "automatic payment approval ON"]


async def test_turning_it_on_needs_the_confirmation_and_working_payments(app_pool, clock):
    svc = build_services(app_pool, clock, clock, frozenset({ADMIN}))
    res = await svc.payments.set_auto_approve(ADMIN, True, confirmed=False)
    assert (res.kind, res.field) == ("invalid", "auto_confirm")
    res = await svc.payments.set_auto_approve(ADMIN, True, confirmed=True)
    assert (res.kind, res.field) == ("invalid", "auto_payments_off")  # payments not set up
    assert (await svc.payments.get_settings(ADMIN))["auto_approve"] is False


async def test_only_an_admin_can_change_it(app_pool, clock):
    await enable_payments(app_pool, UPI)
    svc = build_services(app_pool, clock, clock, frozenset({ADMIN}))
    assert (await svc.payments.set_auto_approve(U, True, confirmed=True)).kind == "denied"
    assert await app_pool.fetchval("SELECT auto_approve FROM payment_settings") is False


# ------------------------------------------------------------------ approval


async def test_a_verified_payment_is_approved_when_it_is_on(app_pool, clock):
    svc, pid, _ = await setup(app_pool, clock, screen())
    res = await svc.payments.auto_approve(pid)
    assert res.kind == "ok" and res.user_id == U and res.mode == "started"
    p = await row(app_pool, pid)
    assert (p["status"], p["approval_mode"], p["reviewed_by"]) == ("approved", "auto", 0)
    assert p["utr"] == "412345678901" and p["subscription_id"] is not None
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 1
    assert len(await audit(app_pool, "payment_auto_approve")) == 1
    assert await audit(app_pool, "payment_approve") == []  # distinguishable from a manual one


async def test_the_same_payment_stays_manual_when_it_is_off(app_pool, clock):
    svc, pid, _ = await setup(app_pool, clock, screen(), on=False)
    res = await svc.payments.auto_approve(pid)
    assert (res.kind, res.reason) == ("not_eligible", "auto_off")
    assert (await row(app_pool, pid))["status"] == "pending"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    # and an admin can still approve it by hand, recorded as manual
    assert (await svc.payments.approve(ADMIN, pid)).kind == "ok"
    p = await row(app_pool, pid)
    assert (p["approval_mode"], p["reviewed_by"]) == ("manual", ADMIN)
    assert len(await audit(app_pool, "payment_approve")) == 1


async def test_turning_it_off_again_stops_it(app_pool, clock):
    svc, pid, _ = await setup(app_pool, clock, screen())
    assert (await svc.payments.set_auto_approve(ADMIN, False)).kind == "ok"
    assert (await svc.payments.auto_approve(pid)).reason == "auto_off"


@pytest.mark.parametrize(
    "text,reason",
    [
        (screen(amount="₹29"), "verdict_not_ok"),  # amount mismatch
        (screen(amount="₹29\n₹30"), "verdict_not_ok"),  # unclear amount
        (screen(amount="799"), "verdict_not_ok"),  # rupee sign read as a digit
        (screen(upi="someone.else@okaxis"), "verdict_not_ok"),  # payee mismatch
        (screen(upi="Shop.Tast@okbank"), "verdict_not_ok"),  # payee only close
        ("", "verdict_not_ok"),  # unreadable
        ("lorem ipsum nothing here at all", "verdict_not_ok"),
        (screen(status="Payment Failed"), "verdict_not_ok"),
        (screen(utr="no reference shown"), "verdict_not_ok"),
    ],
)
async def test_uncertain_or_wrong_evidence_never_auto_approves(app_pool, clock, text, reason):
    svc, pid, _ = await setup(app_pool, clock, text)
    res = await svc.payments.auto_approve(pid)
    assert res.kind == "not_eligible", res
    assert (await row(app_pool, pid))["status"] == "pending"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0


async def test_a_typed_utr_that_differs_from_the_screenshot_blocks_it(app_pool, clock):
    await enable_payments(app_pool, UPI)
    await make_user(app_pool, U)
    plan = await make_plan(app_pool, price_minor=9900)
    svc = build_services(app_pool, clock, clock, frozenset({ADMIN}), ocr=FakeOcr(screen()))
    await svc.payments.start(U, plan)
    await svc.payments.set_auto_approve(ADMIN, True, confirmed=True)
    sub = await svc.payments.submit_screenshot(U, tiny.PNG, "UTR 999999999999")
    res = await svc.payments.auto_approve(sub.payment_id)
    assert (res.kind, res.reason) == ("not_eligible", "utr_mismatch")


async def test_a_matching_typed_utr_is_fine(app_pool, clock):
    await enable_payments(app_pool, UPI)
    await make_user(app_pool, U)
    plan = await make_plan(app_pool, price_minor=9900)
    svc = build_services(app_pool, clock, clock, frozenset({ADMIN}), ocr=FakeOcr(screen()))
    await svc.payments.start(U, plan)
    await svc.payments.set_auto_approve(ADMIN, True, confirmed=True)
    sub = await svc.payments.submit_screenshot(U, tiny.PNG, "UTR 412345678901")
    assert (await svc.payments.auto_approve(sub.payment_id)).kind == "ok"


async def test_a_utr_already_used_by_another_payment_blocks_it(app_pool, clock):
    svc, pid, plan = await setup(app_pool, clock, screen())
    await make_user(app_pool, U2)
    other = (await svc.payments.start(U2, plan)).order.payment_id
    await app_pool.execute(
        "UPDATE payments SET status='pending', utr='412345678901', submitted_at=now() WHERE id=$1",
        other,
    )
    res = await svc.payments.auto_approve(pid)
    assert (res.kind, res.reason) == ("not_eligible", "duplicate_utr")
    assert (await row(app_pool, pid))["status"] == "pending"


async def test_a_reused_screenshot_blocks_it(app_pool, clock):
    svc, pid, plan = await setup(app_pool, clock, screen())
    await make_user(app_pool, U2)
    other = (await svc.payments.start(U2, plan)).order.payment_id
    await svc.payments.attach_evidence(U2, other, tiny.PNG)  # byte-identical image
    res = await svc.payments.auto_approve(pid)
    assert (res.kind, res.reason) == ("not_eligible", "image_reused")


async def test_an_inactive_user_or_plan_is_never_auto_approved(app_pool, clock):
    svc, pid, plan = await setup(app_pool, clock, screen())
    await app_pool.execute("UPDATE plans SET is_active = false WHERE id = $1", plan)
    assert (await svc.payments.auto_approve(pid)).kind == "plan_inactive"
    await app_pool.execute("UPDATE plans SET is_active = true WHERE id = $1", plan)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", U)
    assert (await svc.payments.auto_approve(pid)).kind == "user_inactive"
    assert (await row(app_pool, pid))["status"] == "pending"


async def test_payments_switched_off_blocks_it(app_pool, clock):
    svc, pid, _ = await setup(app_pool, clock, screen())
    await app_pool.execute("UPDATE payment_settings SET is_active = false")
    assert (await svc.payments.auto_approve(pid)).reason == "payments_off"


async def test_a_payment_is_approved_once_even_when_called_many_times(app_pool, clock):
    svc, pid, _ = await setup(app_pool, clock, screen())
    results = await asyncio.gather(
        svc.payments.auto_approve(pid),
        svc.payments.auto_approve(pid),
        svc.payments.auto_approve(pid),
        svc.payments.approve(ADMIN, pid),
    )
    kinds = sorted(r.kind for r in results)
    assert kinds.count("ok") == 1 and set(kinds) <= {"ok", "already_handled"}
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 1
    assert (await row(app_pool, pid))["status"] == "approved"


async def test_an_analysis_that_is_missing_means_manual(app_pool, clock):
    svc, pid, _ = await setup(app_pool, clock, screen())
    await app_pool.execute("DELETE FROM payment_evidence_analysis WHERE payment_id = $1", pid)
    res = await svc.payments.auto_approve(pid)
    assert (res.kind, res.reason) == ("not_eligible", "no_analysis")

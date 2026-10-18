"""The admin Plans page assigns a tier; the tier template becomes the plan's entitlements."""

import json

import pytest

from bot.tiers import TIERS
from tests import payment_images as img
from tests.factories import enable_payments, make_user
from tests.web.conftest import ADMIN


async def features(pool, code):
    rows = await pool.fetch(
        "SELECT pe.feature_key, pe.value FROM plan_entitlements pe "
        "JOIN plans p ON p.id = pe.plan_id WHERE p.code = $1",
        code,
    )
    return {r["feature_key"]: json.loads(r["value"]) for r in rows}


FORM = {"code": "gold", "name": "Gold", "days": "30", "price": "299", "active": "1"}


@pytest.mark.parametrize("tier", ["premium", "pro", "vip"])
async def test_creating_a_plan_applies_the_chosen_tier_template(admin, tier):
    r = await admin.post("/admin/plans", FORM | {"tier": tier})
    assert r.headers["location"].endswith("?msg=plan_saved")
    assert await features(admin.pool, "gold") == TIERS[tier]
    assert await admin.pool.fetchval("SELECT tier FROM plans WHERE code='gold'") == tier
    assert "gold" in (await admin.client.get("/admin/plans")).text


async def test_a_new_plan_defaults_to_premium_and_unknown_tiers_are_refused(admin):
    await admin.post("/admin/plans", FORM)
    assert await features(admin.pool, "gold") == TIERS["premium"]
    r = await admin.post("/admin/plans", FORM | {"code": "bad", "tier": "diamond"})
    assert r.headers["location"].endswith("?msg=bad_tier")
    assert await admin.pool.fetchval("SELECT count(*) FROM plans WHERE code='bad'") == 0


async def test_changing_the_tier_replaces_the_entitlements_and_keeping_it_does_not(admin):
    await admin.post("/admin/plans", FORM | {"tier": "premium"})
    pid = await admin.pool.fetchval("SELECT id FROM plans WHERE code='gold'")
    edit = {"name": "Gold", "days": "30", "price": "299", "active": "1"}
    # a hand-tuned entitlement survives an edit that keeps the tier
    await admin.pool.execute(
        'UPDATE plan_entitlements SET value = \'{"enabled": true, "daily_limit": 321}\' '
        "WHERE plan_id=$1 AND feature_key='swipe'",
        pid,
    )
    await admin.post(f"/admin/plans/{pid}", edit | {"tier": "premium"})
    assert (await features(admin.pool, "gold"))["swipe"]["daily_limit"] == 321
    await admin.post(f"/admin/plans/{pid}", edit | {"tier": ""})  # "custom (keep)"
    assert (await features(admin.pool, "gold"))["swipe"]["daily_limit"] == 321
    await admin.post(f"/admin/plans/{pid}", edit | {"tier": "vip"})
    assert await features(admin.pool, "gold") == TIERS["vip"]
    assert await admin.pool.fetchval("SELECT tier FROM plans WHERE code='gold'") == "vip"
    details = [r["detail"] for r in await admin.audit_rows()]
    assert any("vip" in d for d in details)


async def test_the_plans_page_offers_the_three_tiers_and_needs_csrf(admin):
    html = (await admin.client.get("/admin/plans")).text
    for tier in ("premium", "pro", "vip"):
        assert f'<option value="{tier}"' in html
    r = await admin.client.post("/admin/plans", data=FORM | {"tier": "vip"})
    assert r.status_code == 403 and await admin.pool.fetchval("SELECT count(*) FROM plans") == 1


async def test_an_approved_pro_purchase_unlocks_pro_features_for_the_buyer(admin):
    await enable_payments(admin.pool)
    await admin.post("/admin/plans", FORM | {"code": "pro_plan", "tier": "pro"})
    await make_user(admin.pool, 1)
    plan = await admin.pool.fetchval("SELECT id FROM plans WHERE code='pro_plan'")
    svc = admin.state.services
    pid = (await svc.payments.start(1, plan)).order.payment_id
    await svc.payments.attach_evidence(1, pid, img.JPEG)
    await svc.payments.submit(1, pid, None)
    assert (await svc.entitlements.get(1, "see_who_liked")).enabled is False  # not before approval
    await admin.post(f"/admin/payments/{pid}/approve")
    assert (await svc.entitlements.get(1, "see_who_liked")).enabled is True
    assert (await svc.entitlements.get(1, "swipe")).daily_limit == 500
    assert ADMIN  # approved by the logged-in admin


async def test_a_queued_purchase_tells_the_user_when_it_starts(admin, monkeypatch):
    # start() refuses a lower tier while a higher one runs; this is about the notice for an order
    # that was opened earlier and is queued when approved, so the guard is off here.
    async def nothing_running(db, uid):
        return []

    monkeypatch.setattr("bot.db.repo_payments.running_levels", nothing_running)

    class FakeBot:
        sent: list = []

        async def send_message(self, chat_id, text, **kw):
            self.sent.append((chat_id, text))

    await enable_payments(admin.pool)
    await admin.post("/admin/plans", FORM | {"code": "vipp", "name": "VIP", "tier": "vip"})
    await admin.post("/admin/plans", FORM | {"code": "prem", "tier": "premium"})
    await make_user(admin.pool, 1)
    svc = admin.state.services
    admin.state.bot = FakeBot()
    for code in ("vipp", "prem"):
        plan = await admin.pool.fetchval("SELECT id FROM plans WHERE code=$1", code)
        admin.clock.advance(120)
        pid = (await svc.payments.start(1, plan)).order.payment_id
        await svc.payments.attach_evidence(1, pid, img.JPEG)
        await svc.payments.submit(1, pid, None)
        await admin.post(f"/admin/payments/{pid}/approve")
    first, second = (t for _, t in FakeBot.sent)
    assert "approved" in first and "starts on" not in first
    assert "starts on" in second and "when your current plan ends" in second


async def test_default_tier_plans_button_creates_them_without_prices_and_needs_csrf(admin):
    r = await admin.client.post("/admin/plans/defaults", data={})
    assert r.status_code == 403
    r = await admin.post("/admin/plans/defaults", {})
    assert r.headers["location"].endswith("?msg=plan_defaults")
    rows = await admin.pool.fetch(
        "SELECT code, tier, is_active, price_minor FROM plans WHERE code <> 'free' ORDER BY id"
    )
    assert [(x["code"], x["tier"], x["is_active"], x["price_minor"]) for x in rows] == [
        ("premium", "premium", False, None),
        ("pro", "pro", False, None),
        ("vip", "vip", False, None),
    ]
    page = (await admin.client.get("/admin/plans")).text
    assert 'id="h-tiers"' in page and "VIP" in page and "no price yet" in page
    again = await admin.post("/admin/plans/defaults", {})  # idempotent
    assert again.headers["location"].endswith("?msg=plan_defaults")
    assert await admin.pool.fetchval("SELECT count(*) FROM plans WHERE code <> 'free'") == 3
    assert any("default tiers" in a["detail"] for a in await admin.audit_rows())

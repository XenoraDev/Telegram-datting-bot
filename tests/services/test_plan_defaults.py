"""Default Premium / Pro / VIP plans: created safely, completed idempotently, never overwritten."""

import asyncio
import json

from bot.db import repo_payments
from bot.tiers import TIERS
from tests.factories import enable_payments, make_plan, make_user

ADMIN = 424242  # in the services fixture's admin_ids? see admin() below


def admin_svc(app_pool, clock):
    from bot.services import build_services

    return build_services(app_pool, clock, clock, frozenset({ADMIN}))


async def snapshot(pool):
    out = {}
    for table, order in (
        ("plans", "id"),
        ("plan_entitlements", "plan_id, feature_key"),
        ("subscriptions", "id"),
        ("payments", "id"),
    ):
        out[table] = [dict(r) for r in await pool.fetch(f"SELECT * FROM {table} ORDER BY {order}")]  # noqa: S608
    return out


async def features(pool, code):
    rows = await pool.fetch(
        "SELECT pe.feature_key, pe.value FROM plan_entitlements pe "
        "JOIN plans p ON p.id = pe.plan_id WHERE p.code = $1",
        code,
    )
    return {r["feature_key"]: json.loads(r["value"]) for r in rows}


async def test_creates_the_three_tier_plans_ready_but_not_for_sale(app_pool, clock):
    svc = admin_svc(app_pool, clock)
    done = await svc.payments.ensure_default_plans()
    assert done == {"premium": "created", "pro": "created", "vip": "created"}
    for tier, name in (("premium", "Premium"), ("pro", "Pro"), ("vip", "VIP")):
        row = await app_pool.fetchrow("SELECT * FROM plans WHERE code = $1", tier)
        assert (row["name"], row["tier"], row["is_active"]) == (name, tier, False)
        assert row["price_minor"] is None and row["duration_days"] is None  # no invented prices
        assert await features(app_pool, tier) == TIERS[tier]
    assert await repo_payments.purchasable_plans(app_pool) == []


async def test_running_it_again_changes_nothing(app_pool, clock):
    svc = admin_svc(app_pool, clock)
    await svc.payments.ensure_default_plans()
    before = await snapshot(app_pool)
    assert set((await svc.payments.ensure_default_plans()).values()) == {"unchanged"}
    assert await snapshot(app_pool) == before


async def test_concurrent_setups_create_each_plan_once(app_pool, clock):
    svc = admin_svc(app_pool, clock)
    runs = await asyncio.gather(*(svc.payments.ensure_default_plans() for _ in range(6)))
    assert sum(r["pro"] == "created" for r in runs) == 1
    assert (
        await app_pool.fetchval("SELECT count(*) FROM plans WHERE code IN ('premium','pro','vip')")
        == 3
    )


async def test_an_admin_configured_plan_with_a_default_code_is_not_overwritten(app_pool, clock):
    svc = admin_svc(app_pool, clock)
    pid = await make_plan(app_pool, "pro", name="Gold Plus", price_minor=4900, tier="premium")
    await app_pool.execute("UPDATE plans SET tier = NULL WHERE id = $1", pid)  # "custom (keep)"
    await app_pool.execute(
        'UPDATE plan_entitlements SET value = \'{"enabled": true, "daily_limit": 7}\' '
        "WHERE plan_id = $1 AND feature_key = 'swipe'",
        pid,
    )
    before = await features(app_pool, "pro")
    done = await svc.payments.ensure_default_plans()
    assert done["pro"] == "kept_custom"
    assert await features(app_pool, "pro") == before
    row = await app_pool.fetchrow(
        "SELECT name, price_minor, is_active, tier FROM plans WHERE id=$1", pid
    )
    assert (row["name"], row["price_minor"], row["is_active"], row["tier"]) == (
        "Gold Plus",
        4900,
        True,
        None,
    )


async def test_a_plan_of_another_tier_under_a_default_code_is_left_alone(app_pool, clock):
    svc = admin_svc(app_pool, clock)
    await make_plan(app_pool, "vip", name="V", tier="premium")
    before = await features(app_pool, "vip")
    assert (await svc.payments.ensure_default_plans())["vip"] == "kept_custom"
    assert await features(app_pool, "vip") == before


async def test_a_tiered_plan_only_gets_its_missing_keys(app_pool, clock):
    svc = admin_svc(app_pool, clock)
    pid = await make_plan(app_pool, "vip", name="Mine", price_minor=19900, tier="vip")
    await app_pool.execute(
        'UPDATE plan_entitlements SET value = \'{"enabled": true, "daily_limit": 9}\' '
        "WHERE plan_id = $1 AND feature_key = 'swipe'",
        pid,
    )
    await app_pool.execute(
        "DELETE FROM plan_entitlements WHERE plan_id = $1 AND feature_key = 'profile_boost'", pid
    )
    assert (await svc.payments.ensure_default_plans())["vip"] == "completed"
    got = await features(app_pool, "vip")
    assert got["profile_boost"] == {"enabled": True}  # restored
    assert got["swipe"]["daily_limit"] == 9  # the operator's value stays
    row = await app_pool.fetchrow(
        "SELECT name, price_minor, is_active FROM plans WHERE id = $1", pid
    )
    assert (row["name"], row["price_minor"], row["is_active"]) == ("Mine", 19900, True)


async def test_an_untiered_empty_plan_gets_the_template(app_pool, clock):
    svc = admin_svc(app_pool, clock)
    await app_pool.execute(
        "INSERT INTO plans (code, name, duration_days, price_minor) "
        "VALUES ('premium', 'Old', 30, 5900)"
    )
    assert (await svc.payments.ensure_default_plans())["premium"] == "configured"
    assert await features(app_pool, "premium") == TIERS["premium"]
    assert await app_pool.fetchval("SELECT price_minor FROM plans WHERE code='premium'") == 5900


async def test_subscriptions_payments_free_plan_and_other_plans_are_untouched(app_pool, clock):
    svc = admin_svc(app_pool, clock)
    await enable_payments(app_pool)
    await make_user(app_pool, 1)
    pid = await make_plan(app_pool, "old_30", tier="premium")
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "VALUES (1, $1, now() + interval '9 days')",
        pid,
    )
    start = await svc.payments.start(1, pid)
    assert start.kind == "ok"
    before = await snapshot(app_pool)
    await svc.payments.ensure_default_plans()
    after = await snapshot(app_pool)
    for table in ("subscriptions", "payments"):
        assert after[table] == before[table]
    kept = [p for p in before["plans"]]
    assert [p for p in after["plans"] if p["id"] in {k["id"] for k in kept}] == kept
    assert [
        e for e in after["plan_entitlements"] if e["plan_id"] in {k["id"] for k in kept}
    ] == before["plan_entitlements"]


async def test_only_an_admin_can_run_it_from_the_panel_and_it_is_audited(app_pool, clock):
    svc = admin_svc(app_pool, clock)
    assert await svc.payments.ensure_default_plans(111) == {}
    assert await app_pool.fetchval("SELECT count(*) FROM plans WHERE code='pro'") == 0
    done = await svc.payments.ensure_default_plans(ADMIN)
    assert done["pro"] == "created"
    detail = await app_pool.fetchval("SELECT detail FROM admin_actions ORDER BY id DESC LIMIT 1")
    assert "default tiers" in detail and "pro created" in detail


async def test_after_the_admin_prices_a_default_plan_it_is_for_sale_with_the_right_powers(
    app_pool, clock
):
    svc = admin_svc(app_pool, clock)
    await svc.payments.ensure_default_plans()
    pro = await app_pool.fetchval("SELECT id FROM plans WHERE code = 'pro'")
    res = await svc.payments.save_plan(
        ADMIN, pro, code="", name="Pro", days="30", price="499", active=True
    )
    assert res.kind == "ok"
    assert [p["code"] for p in await repo_payments.purchasable_plans(app_pool)] == ["pro"]
    await make_user(app_pool, 1)
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "VALUES (1, $1, now() + interval '30 days')",
        pro,
    )
    for feature in ("advanced_filters", "profile_boost", "see_who_liked"):
        assert (await svc.entitlements.get(1, feature)).enabled
    assert (await svc.entitlements.get(1, "swipe")).daily_limit == 500
    assert (await svc.entitlements.get(1, "match_priority")).level == 2

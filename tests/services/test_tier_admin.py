"""The tier editor: limits and capabilities are data, edited without code, consistent per tier."""

import json

import pytest

from bot.services import build_services
from bot.services.tier_admin import CAPABILITIES, EDITABLE, LIMITS, parse_form
from bot.tiers import FREE, TIERS
from tests.factories import make_plan, make_user

ADMIN = 424242


@pytest.fixture
def adm(app_pool, clock):
    return build_services(app_pool, clock, clock, frozenset({ADMIN}))


def form(**over):
    """A complete valid form: the built-in Premium values, with changes."""
    base = {}
    for feat in LIMITS:
        base[f"{feat}_mode"] = "limited"
        base[f"{feat}_limit"] = "200"
    for cap in ("view_partner_age", "view_partner_gender", "view_partner_city"):
        base[f"cap_{cap}"] = "1"
    base.update({"match_priority": "1", "plan_badge": "1"})
    base.update(over)
    return base


async def rows(pool, code):
    got = await pool.fetch(
        "SELECT feature_key, value FROM plan_entitlements pe JOIN plans p ON p.id = pe.plan_id "
        "WHERE p.code = $1",
        code,
    )
    return {r["feature_key"]: json.loads(r["value"]) for r in got}


async def snapshot(pool):
    out = {}
    for t in ("subscriptions", "payments"):
        out[t] = [dict(r) for r in await pool.fetch(f"SELECT * FROM {t} ORDER BY id")]  # noqa: S608
    out["plans"] = [
        dict(r)
        for r in await pool.fetch(
            "SELECT id, code, name, duration_days, price_minor, is_active, tier "
            "FROM plans ORDER BY id"
        )
    ]
    return out


# ---------------------------------------------------------------- reading


async def test_the_overview_shows_the_documented_defaults(app_pool, adm):
    views = {v.tier: v for v in await adm.tiers.overview(ADMIN)}
    assert list(views) == ["free", "premium", "pro", "vip"]
    assert views["free"].limits == {
        "random_chat": ("limited", 100),
        "browse": ("limited", 100),
        "swipe": ("limited", 50),
    }
    assert views["premium"].limits["swipe"] == ("limited", 200)
    assert views["pro"].limits["browse"] == ("limited", 500)
    assert all(m == "unlimited" for m, _ in views["vip"].limits.values())
    assert views["free"].capabilities == {c: False for c in CAPABILITIES} | {
        "view_partner_city": True
    }
    assert (
        views["premium"].capabilities["view_partner_age"]
        and not views["premium"].capabilities["advanced_filters"]
    )
    assert all(views["vip"].capabilities.values())
    assert [views[t].priority for t in ("free", "premium", "pro", "vip")] == [0, 1, 2, 3]
    assert [views[t].badge for t in ("free", "premium", "pro", "vip")] == [0, 1, 2, 3]
    assert not any(v.customised for v in views.values())


async def test_only_admins_can_read_or_change_anything(app_pool, adm):
    assert await adm.tiers.overview(1) == []
    assert (await adm.tiers.save(1, "premium", form())).kind == "denied"
    assert (await adm.tiers.reset(1, "premium")).kind == "denied"
    assert await app_pool.fetchval("SELECT count(*) FROM tier_settings") == 0
    assert await app_pool.fetchval("SELECT count(*) FROM admin_actions") == 0


# ---------------------------------------------------------------- saving


async def test_saving_a_tier_updates_every_plan_of_that_tier_and_nothing_else(app_pool, adm):
    p30 = await make_plan(app_pool, "premium_30", name="Premium 30", days=30, price_minor=9900)
    p60 = await make_plan(app_pool, "premium_60", name="Premium 60", days=60, price_minor=17900)
    pro = await make_plan(app_pool, "pro_30", name="Pro 30", price_minor=29900, tier="pro")
    custom = await make_plan(app_pool, "legacy", name="Legacy", price_minor=4900, tier="premium")
    await app_pool.execute("UPDATE plans SET tier = NULL WHERE id = $1", custom)
    await app_pool.execute(
        'UPDATE plan_entitlements SET value = \'{"enabled": true, "daily_limit": 7}\' '
        "WHERE plan_id = $1 AND feature_key = 'swipe'",
        custom,
    )
    await make_user(app_pool, 1)
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "VALUES (1, $1, now() + interval '9 days')",
        p30,
    )
    before = await snapshot(app_pool)
    pro_before, custom_before = await rows(app_pool, "pro_30"), await rows(app_pool, "legacy")

    res = await adm.tiers.save(
        ADMIN, "premium", form(swipe_limit="321", browse_mode="unlimited", cap_profile_boost="1")
    )
    assert res.kind == "ok"
    for code in ("premium_30", "premium_60"):
        got = await rows(app_pool, code)
        assert got["swipe"] == {"enabled": True, "daily_limit": 321}
        assert got["browse"] == {"enabled": True, "daily_limit": None}
        assert got["profile_boost"] == {"enabled": True}
        assert "advanced_filters" not in got
    assert await rows(app_pool, "pro_30") == pro_before  # another tier
    assert await rows(app_pool, "legacy") == custom_before  # a custom plan
    after = await snapshot(app_pool)
    assert after == before  # prices, lengths, names, active flags, subscriptions, payments
    del p60, pro
    # the running subscription follows the new limits at once (data, not code)
    assert (await adm.entitlements.get(1, "swipe")).daily_limit == 321
    assert (await adm.entitlements.get(1, "profile_boost")).enabled
    assert (await adm.entitlements.get(2, "profile_boost")).enabled is False


async def test_saved_values_are_kept_for_plans_created_later(app_pool, adm):
    await adm.tiers.save(ADMIN, "pro", form(swipe_limit="640", cap_advanced_filters="1"))
    res = await adm.payments.save_plan(
        ADMIN, None, code="pro_90", name="Pro 90", days="90", price="999", active=True, tier="pro"
    )
    assert res.kind == "ok"
    assert (await rows(app_pool, "pro_90"))["swipe"]["daily_limit"] == 640
    done = await adm.payments.ensure_default_plans()  # the default Pro plan: same template
    assert done["pro"] == "created"
    assert (await rows(app_pool, "pro"))["swipe"]["daily_limit"] == 640


async def test_unlimited_and_zero_are_both_storable(app_pool, adm):
    await make_plan(app_pool, "vip_30", tier="vip")
    ok = await adm.tiers.save(ADMIN, "vip", form(swipe_mode="unlimited", browse_limit="0"))
    assert ok.kind == "ok"
    got = await rows(app_pool, "vip_30")
    assert got["swipe"]["daily_limit"] is None and got["browse"]["daily_limit"] == 0


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"swipe_limit": "-1"}, "swipe_limit"),
        ({"swipe_limit": "abc"}, "swipe_limit"),
        ({"swipe_limit": "1e3"}, "swipe_limit"),
        ({"swipe_limit": "1.5"}, "swipe_limit"),
        ({"swipe_limit": ""}, "swipe_limit"),
        ({"swipe_limit": " "}, "swipe_limit"),
        ({"swipe_limit": "1000001"}, "swipe_limit"),
        ({"swipe_limit": "٣٣"}, "swipe_limit"),  # non-ASCII digits
        ({"swipe_limit": "9" * 40}, "swipe_limit"),
        ({"browse_mode": "infinite"}, "browse_mode"),
        ({"browse_mode": ""}, "browse_mode"),
        ({"match_priority": "101"}, "match_priority"),
        ({"match_priority": "-2"}, "match_priority"),
        ({"match_priority": "x"}, "match_priority"),
        ({"plan_badge": "4"}, "plan_badge"),
        ({"plan_badge": "-1"}, "plan_badge"),
    ],
)
async def test_invalid_values_are_refused_and_nothing_changes(app_pool, adm, over, field):
    await make_plan(app_pool, "premium_30", tier="premium")
    before = await rows(app_pool, "premium_30")
    res = await adm.tiers.save(ADMIN, "premium", form(**over))
    assert (res.kind, res.field) == ("invalid", field)
    assert await rows(app_pool, "premium_30") == before
    assert await app_pool.fetchval("SELECT count(*) FROM tier_settings") == 0
    assert await app_pool.fetchval("SELECT count(*) FROM admin_actions") == 0


def test_a_missing_mode_is_invalid_and_unlimited_ignores_the_number():
    values, bad = parse_form({k: v for k, v in form().items() if k != "swipe_mode"})
    assert values is None and bad == "swipe_mode"
    values, bad = parse_form(form(swipe_mode="unlimited", swipe_limit="garbage"))
    assert bad is None and values["swipe"] == {"enabled": True, "daily_limit": None}


async def test_unknown_tiers_are_not_found(app_pool, adm):
    assert (await adm.tiers.save(ADMIN, "gold", form())).kind == "not_found"
    assert (await adm.tiers.reset(ADMIN, "'; DROP TABLE plans;--")).kind == "not_found"


async def test_switching_a_capability_off_removes_it_from_the_plans(app_pool, adm):
    await make_plan(app_pool, "pro_30", tier="pro")
    await make_user(app_pool, 1)
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "SELECT 1, id, now() + interval '9 days' FROM plans WHERE code = 'pro_30'"
    )
    assert (await adm.entitlements.get(1, "advanced_filters")).enabled
    res = await adm.tiers.save(
        ADMIN, "pro", form(cap_see_who_liked="1", cap_profile_boost="1", match_priority="2")
    )
    assert res.kind == "ok"
    assert "advanced_filters" not in await rows(app_pool, "pro_30")
    assert (await adm.entitlements.get(1, "advanced_filters")).enabled is False
    assert (await adm.entitlements.get(1, "see_who_liked")).enabled
    # and the stored override remembers it, so a plan created later agrees
    again = await adm.payments.save_plan(
        ADMIN, None, code="pro_90", name="Pro 90", days="90", price="999", active=True, tier="pro"
    )
    assert again.kind == "ok" and "advanced_filters" not in await rows(app_pool, "pro_90")


async def test_priority_and_badge_are_editable(app_pool, adm):
    await make_plan(app_pool, "premium_30", tier="premium")
    await adm.tiers.save(ADMIN, "premium", form(match_priority="9", plan_badge="3"))
    got = await rows(app_pool, "premium_30")
    assert got["match_priority"] == {"enabled": True, "level": 9}
    assert got["plan_badge"] == {"enabled": True, "level": 3}
    await adm.tiers.save(ADMIN, "premium", form(plan_badge="0"))
    assert "plan_badge" not in await rows(app_pool, "premium_30")


# ---------------------------------------------------------------- free tier


async def test_the_free_tier_is_edited_in_place(app_pool, adm):
    res = await adm.tiers.save(
        ADMIN,
        "free",
        form(
            random_chat_limit="120",
            browse_limit="130",
            swipe_limit="60",
            match_priority="0",
            plan_badge="0",
        )
        | {"cap_view_partner_age": "", "cap_view_partner_gender": "", "cap_view_partner_city": "1"},
    )
    assert res.kind == "ok"
    got = await rows(app_pool, "free")
    assert (got["random_chat"]["daily_limit"], got["browse"]["daily_limit"]) == (120, 130)
    assert got["swipe"]["daily_limit"] == 60
    assert "view_partner_age" not in got and got["view_partner_city"] == {"enabled": True}
    assert (
        await app_pool.fetchval("SELECT count(*) FROM tier_settings") == 0
    )  # free has no tier row
    await make_user(app_pool, 1)
    assert (await adm.entitlements.get(1, "swipe")).daily_limit == 60


async def test_a_paid_tier_below_free_is_flagged_because_free_wins(app_pool, adm):
    await adm.tiers.save(ADMIN, "premium", form(swipe_limit="10"))
    view = next(v for v in await adm.tiers.overview(ADMIN) if v.tier == "premium")
    assert view.warnings == ["swipe"]
    await make_user(app_pool, 1)
    pid = await make_plan(app_pool, "premium_30", tier="premium")
    await adm.tiers.save(ADMIN, "premium", form(swipe_limit="10"))
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "VALUES (1, $1, now() + interval '9 days')",
        pid,
    )
    assert (await adm.entitlements.get(1, "swipe")).daily_limit == 50  # Free's 50 beats 10


# ---------------------------------------------------------------- reset, audit, defaults


async def test_reset_returns_a_tier_to_the_built_in_template(app_pool, adm):
    await make_plan(app_pool, "pro_30", tier="pro")
    await adm.tiers.save(ADMIN, "pro", form(swipe_limit="1", cap_profile_boost=""))
    assert (await adm.tiers.reset(ADMIN, "pro")).kind == "ok"
    assert await rows(app_pool, "pro_30") == TIERS["pro"]
    assert await app_pool.fetchval("SELECT count(*) FROM tier_settings WHERE tier='pro'") == 0
    view = next(v for v in await adm.tiers.overview(ADMIN) if v.tier == "pro")
    assert not view.customised


async def test_free_reset_restores_the_free_defaults(app_pool, adm):
    await adm.tiers.save(ADMIN, "free", form(swipe_limit="999"))
    await adm.tiers.reset(ADMIN, "free")
    got = await rows(app_pool, "free")
    assert {k: got[k] for k in FREE} == FREE


async def test_every_change_is_audited_with_what_was_saved(app_pool, adm):
    await adm.tiers.save(ADMIN, "vip", form(swipe_mode="unlimited", cap_advanced_filters="1"))
    await adm.tiers.reset(ADMIN, "vip")
    details = [
        r["detail"] for r in await app_pool.fetch("SELECT detail FROM admin_actions ORDER BY id")
    ]
    assert "tier vip saved" in details[0] and "swipe=unlimited" in details[0]
    assert "advanced_filters" in details[0]
    assert details[1] == "tier vip reset to defaults"


def test_the_editor_covers_every_advertised_entitlement():
    assert set(EDITABLE) == {
        "random_chat",
        "browse",
        "swipe",
        "view_partner_age",
        "view_partner_gender",
        "view_partner_city",
        "advanced_filters",
        "see_who_liked",
        "profile_boost",
        "match_priority",
        "plan_badge",
    }
    assert set(EDITABLE) >= set(FREE) | {k for t in TIERS.values() for k in t}


async def test_rate_limiting_applies_to_the_editor(app_pool, adm):
    kinds = [(await adm.tiers.save(ADMIN, "premium", form())).kind for _ in range(40)]
    assert "rate_limited" in kinds

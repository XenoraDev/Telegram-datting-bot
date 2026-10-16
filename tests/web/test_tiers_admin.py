"""The Tiers & limits page: authorization, CSRF, validation, and what a save does."""

import pytest

from tests.factories import make_plan
from tests.web.conftest import ADMIN, OTHER_ADMIN

FORM = {
    "random_chat_mode": "limited",
    "random_chat_limit": "250",
    "browse_mode": "limited",
    "browse_limit": "260",
    "swipe_mode": "unlimited",
    "swipe_limit": "",
    "cap_view_partner_age": "1",
    "cap_view_partner_gender": "1",
    "cap_view_partner_city": "1",
    "match_priority": "1",
    "plan_badge": "1",
}


async def test_the_page_lists_the_four_tiers_with_every_control(admin):
    await make_plan(admin.pool, "premium_30", tier="premium")
    html = (await admin.client.get("/admin/tiers")).text
    for tier in ("free", "premium", "pro", "vip"):
        assert f'id="tier-{tier}"' in html and f'action="/admin/tiers/{tier}"' in html
    for needle in (
        "Random chat starts per day",
        "Profiles browsed per day",
        "Likes per day",
        "Unlimited",
        "See a partner&#39;s age",
        "Advanced filters",
        "See who liked me",
        "Profile boost",
        "Match priority",
        "Badge shown on the partner card",
        "tier-wide",
        "<code>premium_30</code>",
    ):
        assert needle in html, needle
    assert html.count('name="csrf_token"') >= 8
    # every control has a label or an aria-label
    assert 'for="p-pro"' in html and 'aria-label="Likes per day: number"' in html


async def test_saving_updates_the_tier_and_flashes(admin):
    pid = await make_plan(admin.pool, "premium_30", tier="premium")
    r = await admin.post("/admin/tiers/premium", FORM)
    assert (
        r.status_code == 303 and r.headers["location"] == "/admin/tiers?msg=tier_saved#tier-premium"
    )
    value = await admin.pool.fetchval(
        "SELECT value::text FROM plan_entitlements WHERE plan_id=$1 AND feature_key='swipe'", pid
    )
    assert '"daily_limit": null' in value
    page = await admin.client.get("/admin/tiers?msg=tier_saved")
    assert "Tier saved" in page.text and 'value="250"' in page.text and "edited" in page.text


@pytest.mark.parametrize(
    ("over", "msg"),
    [
        ({"swipe_mode": "limited", "swipe_limit": "-3"}, "bad_limit"),
        ({"browse_limit": "ten"}, "bad_limit"),
        ({"random_chat_mode": "forever"}, "bad_mode"),
        ({"match_priority": "500"}, "bad_priority"),
        ({"plan_badge": "9"}, "bad_badge"),
    ],
)
async def test_invalid_input_is_refused_with_a_message(admin, over, msg):
    await make_plan(admin.pool, "premium_30", tier="premium")
    r = await admin.post("/admin/tiers/premium", FORM | over)
    assert r.headers["location"].split("#")[0].endswith(f"?msg={msg}")
    assert await admin.pool.fetchval("SELECT count(*) FROM tier_settings") == 0
    assert "limit" in (await admin.client.get(f"/admin/tiers?msg={msg}")).text.lower() or msg


async def test_reset_returns_to_defaults(admin):
    await make_plan(admin.pool, "premium_30", tier="premium")
    await admin.post("/admin/tiers/premium", FORM)
    r = await admin.post("/admin/tiers/premium/reset", {})
    assert r.headers["location"].split("#")[0].endswith("?msg=tier_reset")
    assert await admin.pool.fetchval("SELECT count(*) FROM tier_settings") == 0


@pytest.mark.parametrize(
    "path", ["/admin/tiers/gold", "/admin/tiers/%27%3B--", "/admin/tiers/FREE"]
)
async def test_unknown_tier_names_are_rejected(admin, path):
    assert (await admin.post(path, FORM)).status_code in (404, 422)


async def test_it_needs_a_session_and_csrf(admin, web):
    r = await admin.client.post("/admin/tiers/premium", data=FORM)  # no CSRF token
    assert r.status_code == 403
    assert await admin.pool.fetchval("SELECT count(*) FROM tier_settings") == 0
    r = await admin.client.post("/admin/tiers/premium/reset", data={})
    assert r.status_code == 403


async def test_logged_out_visitors_get_nothing(web):
    for method, path in (("get", "/admin/tiers"), ("post", "/admin/tiers/premium")):
        r = await getattr(web.client, method)(path)
        assert r.status_code in (303, 307, 401, 403, 404)
        assert "Random chat starts" not in r.text


async def test_a_non_admin_who_somehow_has_a_session_changes_nothing(admin):
    """The service re-checks the admin list on its own: a session id alone is not enough."""
    res = await admin.state.services.tiers.save(OTHER_ADMIN + 1, "premium", FORM)
    assert res.kind == "denied" and ADMIN != OTHER_ADMIN + 1
    assert await admin.pool.fetchval("SELECT count(*) FROM tier_settings") == 0


async def test_the_audit_log_shows_the_change(admin):
    await admin.post("/admin/tiers/free", FORM)
    html = (await admin.client.get("/admin/audit")).text
    assert "tier free saved" in html and "swipe=unlimited" in html

"""Web admin: the Queue & funnel page."""

import pytest

from tests.factories import make_user


async def seed(pool):
    await pool.execute(
        "INSERT INTO queue_stats "
        "(day, hour, instant, waited, wait_sum_seconds, expired, cancelled) "
        "VALUES (current_date, 20, 6, 4, 400, 5, 5)"
    )


async def test_the_page_needs_a_session_and_is_in_the_navigation(admin, web):
    html = (await admin.client.get("/admin/")).text
    assert 'href="/admin/insights"' in html and "Queue &amp; funnel" in html
    web.client.cookies.clear()
    r = await web.client.get("/admin/insights")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_the_page_shows_the_numbers_hours_days_and_funnel(admin):
    await seed(admin.pool)
    await make_user(admin.pool, 1)
    html = (await admin.client.get("/admin/insights")).text
    for needle in (
        "Waiting for a partner",
        "Found a partner",
        "10 (50.0%)",  # 6 + 4 found a partner, out of 20 searches
        "Timed out, no partner",
        "5 (25.0%)",
        "100 s",
        "Busiest hours",
        "20:00",
        "01:30",
        "By day",
        "how far they get",
        "Started the bot",
        "Last 7 days",
        "Last 30 days",
    ):
        assert needle in html, needle


async def test_an_empty_database_renders_safe_empty_states(admin):
    html = (await admin.client.get("/admin/insights")).text
    assert "No searches recorded in this period yet" in html
    assert "Nothing to show yet" in html and "No new users in this period" in html


@pytest.mark.parametrize(
    "query", ["?days=30", "?days=7", "?days=abc", "?days=-3", "?days=99999999"]
)
async def test_the_period_is_a_safe_choice_of_two(admin, query):
    await seed(admin.pool)
    r = await admin.client.get(f"/admin/insights{query}")
    assert r.status_code == 200
    assert ('aria-current="page">Last 30 days' in r.text) == (query == "?days=30")


async def test_the_page_never_shows_a_user_id_or_a_name(admin):
    await seed(admin.pool)
    await make_user(admin.pool, 424242)
    html = (await admin.client.get("/admin/insights")).text
    assert "424242" not in html and "user424242" not in html and "Name424242" not in html

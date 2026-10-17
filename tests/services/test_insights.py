"""Queue counters (written by matchmaking) and the funnel / queue report for the admin page."""

import pytest

from bot.services import build_services
from tests.factories import make_user

ADMIN = 9000001


@pytest.fixture
def isvc(app_pool, clock):
    return build_services(app_pool, clock, clock, frozenset({ADMIN}))


async def totals(pool):
    return dict(
        await pool.fetchrow(
            "SELECT coalesce(sum(instant), 0)::int AS instant, "
            "coalesce(sum(waited), 0)::int AS waited, "
            "coalesce(sum(wait_sum_seconds), 0)::int AS wait, "
            "coalesce(sum(expired), 0)::int AS expired, "
            "coalesce(sum(cancelled), 0)::int AS cancelled FROM queue_stats"
        )
    )


async def users(pool, *ids):
    for uid in ids:
        await make_user(pool, uid)


# ------------------------------------------------------------------ the counters


async def test_an_instant_and_a_waited_pairing_are_counted_with_the_wait(app_pool, isvc):
    await users(app_pool, 1, 2)
    await isvc.matchmaking.search(1)  # 1 waits
    await app_pool.execute("UPDATE chat_queue SET joined_at = now() - interval '90 seconds'")
    assert (await isvc.matchmaking.search(2)).kind == "matched"
    t = await totals(app_pool)
    assert (t["instant"], t["waited"]) == (1, 1) and 88 <= t["wait"] <= 95
    assert (t["expired"], t["cancelled"]) == (0, 0)


async def test_a_timeout_and_a_stop_are_counted(app_pool, isvc):
    await users(app_pool, 1, 2, 3)
    await isvc.matchmaking.search(1)
    await isvc.matchmaking.search(2)  # these two pair at once ...
    await isvc.matchmaking.stop(1)
    await isvc.matchmaking.search(3)  # ... 3 waits
    await app_pool.execute("UPDATE chat_queue SET joined_at = now() - interval '40 minutes'")
    assert await isvc.matchmaking.expire_stale_queue() == [3]
    await isvc.matchmaking.search(3)
    assert (await isvc.matchmaking.stop(3)).kind == "left_queue"
    t = await totals(app_pool)
    assert (t["expired"], t["cancelled"]) == (1, 1)


async def test_stopping_a_chat_or_doing_nothing_counts_nothing(app_pool, isvc):
    await users(app_pool, 1, 2)
    await isvc.matchmaking.search(1)
    await isvc.matchmaking.search(2)
    before = await totals(app_pool)
    await isvc.matchmaking.stop(1)  # ends a chat: not a queue event
    await isvc.matchmaking.stop(2)  # nothing to stop
    assert await totals(app_pool) == before


async def test_a_statistics_failure_never_breaks_matching_or_the_timeout_sweep(app_pool, isvc):
    await users(app_pool, 1, 2, 3)
    await app_pool.execute("DROP TABLE queue_stats")  # the worst case: the table is gone
    await isvc.matchmaking.search(1)
    assert (await isvc.matchmaking.search(2)).kind == "matched"
    await isvc.matchmaking.search(3)
    await app_pool.execute("UPDATE chat_queue SET joined_at = now() - interval '40 minutes'")
    assert await isvc.matchmaking.expire_stale_queue() == [3]
    assert (await isvc.matchmaking.stop(1)).kind == "ended"


async def test_the_counters_add_up_over_many_events_in_one_hour_row(app_pool, isvc):
    await users(app_pool, 1, 2, 3, 4)
    for a, b in ((1, 2), (3, 4)):
        await isvc.matchmaking.search(a)
        await isvc.matchmaking.search(b)
        await isvc.matchmaking.stop(a)
    assert await app_pool.fetchval("SELECT count(*) FROM queue_stats") == 1
    assert (await totals(app_pool))["instant"] == 2


# ------------------------------------------------------------------ the report


async def seed(pool):
    await pool.execute(
        "INSERT INTO queue_stats "
        "(day, hour, instant, waited, wait_sum_seconds, expired, cancelled) "
        "VALUES (current_date, 20, 6, 4, 400, 5, 5), (current_date, 21, 2, 2, 200, 3, 1), "
        "(current_date - 20, 3, 100, 100, 1000, 100, 100)"
    )


async def test_the_report_gives_rates_wait_hours_and_days(app_pool, isvc):
    await seed(app_pool)
    r = await isvc.insights.report(ADMIN, 7)
    q = r["queue"]
    assert (q["searches"], q["paired"], q["expired"], q["cancelled"]) == (28, 14, 8, 6)
    assert (q["paired_pct"], q["expired_pct"], q["cancelled_pct"]) == (50.0, 28.6, 21.4)
    assert q["avg_wait_s"] == 100 and q["waited"] == 6  # 600 s over the 6 who waited
    busiest = max(r["hours"], key=lambda h: h["searches"])
    assert (busiest["hour"], busiest["searches"], busiest["ist"]) == (20, 20, "01:30")
    assert len(r["hours"]) == 24 and r["hours"][0]["searches"] == 0
    assert [d["searches"] for d in r["daily"]] == [28]  # the old day is outside 7 days
    wide = await isvc.insights.report(ADMIN, 30)
    assert wide["queue"]["searches"] == 28 + 400 and len(wide["daily"]) == 2


async def test_only_seven_and_thirty_day_windows_exist_and_an_empty_report_is_safe(app_pool, isvc):
    for odd in (0, 1, -5, 31, 9999):
        assert (await isvc.insights.report(ADMIN, odd))["days"] == 7
    q = (await isvc.insights.report(ADMIN, 7))["queue"]
    assert q["searches"] == 0 and q["avg_wait_s"] is None and q["expired_pct"] == 0.0


async def test_only_admins_get_a_report(isvc):
    assert await isvc.insights.report(1, 7) is None
    assert await isvc.insights.report(ADMIN + 1, 30) is None


async def test_the_funnel_counts_real_new_users_step_by_step(app_pool, isvc):
    await users(app_pool, 1, 2, 3, 4)
    await app_pool.execute(
        "INSERT INTO users (id, status) VALUES (5, 'onboarding')"
    )  # stopped at the gate
    await app_pool.execute(
        "INSERT INTO users (id, status) VALUES (-1, 'onboarding')"
    )  # a load-test user
    await app_pool.execute("UPDATE users SET created_at = now() - interval '40 days' WHERE id = 4")
    await app_pool.execute(
        "INSERT INTO users (id, status, created_at) "
        "VALUES (6, 'onboarding', now() - interval '20 days')"
    )
    await isvc.matchmaking.search(1)
    await isvc.matchmaking.search(2)  # 1 and 2 chat
    await app_pool.execute(
        "INSERT INTO swipes (swiper_id, target_id, action) VALUES (1, 3, 'like'), (3, 2, 'pass')"
    )
    f = (await isvc.insights.report(ADMIN, 7))["funnel"]
    assert f["started"] == 4  # users 1, 2, 3 and 5; not the 40-day-old 4, not the negative id
    n = {s["label"]: s["n"] for s in f["steps"]}
    assert n == {
        "Started the bot": 4,
        "Confirmed 18+": 3,
        "Finished the profile": 3,
        "Had a random chat or a friend chat": 2,
        "Followed someone": 1,
    }
    assert [s["pct"] for s in f["steps"]] == [100.0, 75.0, 75.0, 50.0, 25.0]
    assert (await isvc.insights.report(ADMIN, 30))["funnel"][
        "started"
    ] == 5  # plus user 6 (20 days)

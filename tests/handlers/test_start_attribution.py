"""/start with a campaign link: first touch for new accounts only; nothing visible to the user."""

import pytest

from tests.handlers.harness import ADMIN_ID

U = [9201, 9202, 9203, 9204, 9205, 9206]


async def acq(pool, uid):
    row = await pool.fetchrow("SELECT * FROM user_acquisition WHERE user_id = $1", uid)
    return dict(row) if row else None


@pytest.fixture
async def camp(tg):
    res = await tg.services.attribution.create_campaign(ADMIN_ID, "instagram", "Reel A")
    assert res.kind == "ok"
    return res.campaign


def shown(tg, uid):
    return [(o.kind, o.text) for o in tg.inbox(uid)]


async def test_a_new_user_arriving_by_a_campaign_link_is_attributed(tg, app_pool, camp):
    await tg.send(U[0], f"/start c_{camp['code']}")
    row = await acq(app_pool, U[0])
    assert (row["kind"], row["campaign_id"], row["source"]) == ("campaign", camp["id"], "instagram")


async def test_the_user_sees_exactly_what_a_plain_start_shows(tg, app_pool, camp):
    await tg.send(U[0], "/start")
    plain = shown(tg, U[0])
    await tg.send(U[1], f"/start c_{camp['code']}")
    await tg.send(U[2], "/start c_zzzzzzzz")  # unknown
    await tg.send(U[3], "/start " + "x" * 300)  # oversized junk
    assert shown(tg, U[1]) == plain == shown(tg, U[2]) == shown(tg, U[3])  # same text, no error
    assert [(await acq(app_pool, u))["kind"] for u in U[:4]] == [
        "direct",
        "campaign",
        "invalid",
        "invalid",
    ]


async def test_a_plain_start_is_direct(tg, app_pool):
    await tg.send(U[0], "/start")
    assert (await acq(app_pool, U[0]))["kind"] == "direct"


async def test_a_paused_campaign_is_invalid_and_onboarding_still_works(tg, app_pool, camp):
    await tg.services.attribution.set_status(ADMIN_ID, camp["id"], paused=True)
    await tg.send(U[0], f"/start c_{camp['code']}")
    row = await acq(app_pool, U[0])
    assert (row["kind"], row["source"]) == ("invalid", None)
    await tg.onboard(U[1])  # the whole sign-up of a normal user still works
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = $1", U[1]) == "active"
    assert tg.texts(U[0]) and not any("invalid" in t.lower() for t in tg.texts(U[0]))


async def test_repeated_start_and_a_second_link_never_change_the_first_touch(tg, app_pool, camp):
    other = (await tg.services.attribution.create_campaign(ADMIN_ID, "youtube", "Video")).campaign
    await tg.send(U[0], f"/start c_{camp['code']}")
    before = await acq(app_pool, U[0])
    for text in (f"/start c_{other['code']}", "/start", f"/start c_{camp['code']}", "/start junk"):
        await tg.send(U[0], text)
    assert await acq(app_pool, U[0]) == before
    assert await app_pool.fetchval("SELECT count(*) FROM user_acquisition") == 1


async def test_an_existing_user_is_not_attributed_by_a_link(tg, app_pool, camp):
    await tg.onboard(U[0])  # an account that exists and has no row (it predates tracking)
    await app_pool.execute("DELETE FROM user_acquisition WHERE user_id = $1", U[0])
    await tg.send(U[0], f"/start c_{camp['code']}")
    assert await acq(app_pool, U[0]) is None  # nothing is invented for an old account
    assert tg.texts(U[0])  # they simply get the normal reply


async def test_a_bot_restart_in_the_middle_of_sign_up_changes_nothing(tg, app_pool, camp):
    await tg.send(U[0], f"/start c_{camp['code']}")
    before = await acq(app_pool, U[0])
    await tg.send(U[0], "hello?")  # the conversation state is gone: the bot resumes the sign-up
    assert await acq(app_pool, U[0]) == before


async def test_a_blocked_account_is_never_attributed(tg, app_pool, camp):
    # a banned / underage id that comes back with a fresh users row is still refused first
    await app_pool.execute(
        "INSERT INTO deletion_tombstones (user_id, reason) VALUES ($1, 'underage')", U[0]
    )
    await tg.send(U[0], f"/start c_{camp['code']}")
    assert await acq(app_pool, U[0]) is None


async def test_a_failure_in_attribution_never_breaks_the_sign_up(tg, app_pool, camp, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(tg.services.attribution, "record_first_touch", boom)
    await tg.send(U[0], f"/start c_{camp['code']}")
    assert tg.texts(U[0])  # the user still gets the normal first screen
    assert await acq(app_pool, U[0]) is None


async def test_nothing_about_the_payload_is_stored_or_logged(tg, app_pool, camp, caplog):
    caplog.set_level("DEBUG")
    secret = "c_" + "q" * 20
    await tg.send(U[0], f"/start {secret}")
    assert secret not in caplog.text
    assert secret not in (
        await app_pool.fetchval("SELECT user_acquisition::text FROM user_acquisition")
    )

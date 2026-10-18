"""A chat with no message for N minutes is closed; every message restarts the timer."""

import asyncio

import pytest

from bot.config import ConfigError, load_settings
from bot.services import build_services
from bot.services.chat_relay import Incoming
from tests.factories import make_user

A, B, C, D = 9101, 9102, 9103, 9104


def text(s="hello there"):
    return Incoming("text", s)


async def start_random(pool, svc, a=A, b=B):
    for uid in (a, b):
        await make_user(pool, uid)
    assert (await svc.matchmaking.search(a)).kind == "queued"
    res = await svc.matchmaking.search(b)
    assert res.kind == "matched"
    return res.session_id


async def start_friends(pool, svc, a=C, b=D):
    for uid in (a, b):
        await make_user(pool, uid)
    await pool.execute(
        "INSERT INTO matches (user_a_id, user_b_id) VALUES ($1, $2)", min(a, b), max(a, b)
    )
    handle = (await pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", b)).hex
    res = await svc.matchmaking.chat_with_match(a, handle)
    assert res.kind == "started"
    return res.session_id


async def age(pool, sid, minutes):
    await pool.execute(
        "UPDATE chat_sessions SET last_activity_at = now() - make_interval(mins => $2) "
        "WHERE id = $1",
        sid,
        float(minutes),
    )


async def status(pool, sid):
    return await pool.fetchrow(
        "SELECT status, end_reason, ended_by FROM chat_sessions WHERE id = $1", sid
    )


def test_the_default_is_ten_minutes():
    from bot import domain

    assert domain.CHAT_INACTIVITY_TIMEOUT_MINUTES == 10
    assert (
        load_settings(
            {"BOT_TOKEN": "1:x", "DATABASE_URL": "postgresql://u@h/d"}
        ).chat_inactivity_timeout_minutes
        == 10
    )


async def test_a_chat_stays_open_before_ten_minutes(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await age(app_pool, sid, 9.5)
    assert await svc.matchmaking.end_idle_chats() == []
    assert (await status(app_pool, sid))["status"] == "active"


async def test_a_chat_closes_after_ten_minutes_of_silence(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await age(app_pool, sid, 10.5)
    closed = await svc.matchmaking.end_idle_chats()
    assert len(closed) == 1 and closed[0][0] == sid and set(closed[0][1:]) == {A, B}
    row = await status(app_pool, sid)
    assert (row["status"], row["end_reason"], row["ended_by"]) == ("ended", "inactivity", None)
    assert (
        await app_pool.fetchval(
            "SELECT count(*) FROM chat_session_members WHERE session_id = $1 AND active", sid
        )
        == 0
    )
    assert await svc.matchmaking.current_chat(A) is None
    assert await svc.matchmaking.current_chat(B) is None


async def test_both_people_can_start_another_chat_afterwards(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await age(app_pool, sid, 11)
    await svc.matchmaking.end_idle_chats()
    assert (await svc.matchmaking.search(A)).kind == "queued"  # not "already_in_chat"
    assert (await svc.matchmaking.search(B)).kind in ("queued", "matched")


async def test_a_message_restarts_the_timer(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await age(app_pool, sid, 9.9)
    assert (await svc.relay.relay(A, text())).status == "ok"
    last = await app_pool.fetchval(
        "SELECT now() - last_activity_at FROM chat_sessions WHERE id=$1", sid
    )
    assert last.total_seconds() < 5  # restarted
    assert await svc.matchmaking.end_idle_chats() == []
    assert (await status(app_pool, sid))["status"] == "active"


async def test_a_message_from_the_other_person_restarts_it_too(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await age(app_pool, sid, 9.9)
    assert (await svc.relay.relay(B, text("hi back"))).status == "ok"
    assert await svc.matchmaking.end_idle_chats() == []


async def test_a_sticker_counts_as_activity(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await age(app_pool, sid, 9.9)
    assert (await svc.relay.relay(A, Incoming("sticker", sticker_id="S1"))).status == "ok"
    assert await svc.matchmaking.end_idle_chats() == []


async def test_a_message_that_is_not_delivered_does_not_keep_a_silent_chat_alive(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await age(app_pool, sid, 11)
    assert (await svc.relay.relay(A, Incoming("text", "call me on 9876543210"))).status != "ok"
    assert len(await svc.matchmaking.end_idle_chats()) == 1
    assert (await status(app_pool, sid))["status"] == "ended"


async def test_a_message_after_the_close_is_not_delivered(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await age(app_pool, sid, 11)
    await svc.matchmaking.end_idle_chats()
    assert (await svc.relay.relay(A, text())).status == "not_in_chat"


async def test_an_ended_chat_is_not_processed_again(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await age(app_pool, sid, 11)
    assert len(await svc.matchmaking.end_idle_chats()) == 1
    assert await svc.matchmaking.end_idle_chats() == []
    # a chat the user ended by hand is never reported as an inactivity close
    other = await start_random(app_pool, svc, 9111, 9112)
    await svc.matchmaking.stop(9111)
    await age(app_pool, other, 60)
    assert await svc.matchmaking.end_idle_chats() == []
    assert (await status(app_pool, other))["end_reason"] == "user_stop"


async def test_two_sweeps_at_once_close_a_chat_exactly_once(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await age(app_pool, sid, 11)
    results = await asyncio.gather(*(svc.matchmaking.end_idle_chats() for _ in range(4)))
    assert sum(len(r) for r in results) == 1
    assert (await status(app_pool, sid))["status"] == "ended"


@pytest.mark.parametrize("round_", range(12))
async def test_a_message_racing_the_timeout_never_leaves_a_delivered_message_in_a_closed_chat(
    app_pool, svc, round_
):
    a, b = 9200 + round_ * 2, 9201 + round_ * 2
    sid = await start_random(app_pool, svc, a, b)
    await age(app_pool, sid, 11)
    sweep, msg = await asyncio.gather(svc.matchmaking.end_idle_chats(), svc.relay.relay(a, text()))
    state = (await status(app_pool, sid))["status"]
    if msg.status == "ok":  # the message won: the chat must still be open and the sweep empty
        assert state == "active" and sweep == []
    else:  # the sweep won: the chat is closed and the message was NOT delivered
        assert msg.status == "not_in_chat" and state == "ended" and len(sweep) == 1


async def test_friends_chat_is_closed_the_same_way(app_pool, svc):
    sid = await start_friends(app_pool, svc)
    await age(app_pool, sid, 10.5)
    assert len(await svc.matchmaking.end_idle_chats()) == 1
    assert (await status(app_pool, sid))["end_reason"] == "inactivity"
    # and a Friends chat can be opened again
    handle = (await app_pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", D)).hex
    assert (await svc.matchmaking.chat_with_match(C, handle)).kind == "started"


async def test_a_new_friends_chat_starts_with_a_fresh_timer(app_pool, svc):
    sid = await start_friends(app_pool, svc)
    assert await svc.matchmaking.end_idle_chats() == []
    assert (await status(app_pool, sid))["status"] == "active"


async def test_the_timeout_is_configurable(app_pool, clock):
    short = build_services(app_pool, clock, clock, chat_inactivity_minutes=2)
    sid = await start_random(app_pool, short)
    await age(app_pool, sid, 1.5)
    assert await short.matchmaking.end_idle_chats() == []
    await age(app_pool, sid, 2.5)
    assert len(await short.matchmaking.end_idle_chats()) == 1


def test_the_setting_is_read_from_the_environment_and_validated():
    base = {"BOT_TOKEN": "1:x", "DATABASE_URL": "postgresql://u@h/d"}
    assert (
        load_settings(
            {**base, "CHAT_INACTIVITY_TIMEOUT_MINUTES": "15"}
        ).chat_inactivity_timeout_minutes
        == 15
    )
    for bad in ("0", "abc", "-3", "1441", "1.5"):
        with pytest.raises(ConfigError):
            load_settings({**base, "CHAT_INACTIVITY_TIMEOUT_MINUTES": bad})


async def test_no_message_text_is_stored_only_a_time(app_pool, svc):
    sid = await start_random(app_pool, svc)
    await svc.relay.relay(A, text("secret words here"))
    cols = {
        r["column_name"]
        for r in await app_pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'chat_sessions'"
        )
    }
    assert "last_activity_at" in cols and not any("text" in c or "message" in c for c in cols)
    dump = str(dict(await app_pool.fetchrow("SELECT * FROM chat_sessions WHERE id = $1", sid)))
    assert "secret" not in dump


async def test_synthetic_load_test_chats_are_never_touched_by_the_sweep(app_pool, svc):
    for uid in (-901, -902):
        await app_pool.execute(
            "INSERT INTO users (id, status, age_confirmed_at) VALUES ($1, 'active', now())", uid
        )
    sid = await app_pool.fetchval(
        "INSERT INTO chat_sessions (user_a, user_b, last_activity_at) "
        "VALUES (-901, -902, now() - interval '3 hours') RETURNING id"
    )
    assert await svc.matchmaking.end_idle_chats() == []
    assert (await status(app_pool, sid))["status"] == "active"

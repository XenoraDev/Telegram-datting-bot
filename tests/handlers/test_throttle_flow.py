"""Per-user rate limiting at the Telegram layer."""

from bot.db import repo_safety
from bot.services.rate_limit import MUTE_AFTER_VIOLATIONS, POLICIES
from bot.texts import en as t


async def test_a_burst_is_cut_off_and_answered_with_a_single_notice(tg):
    await tg.onboard(1)
    tg.auto_advance = 0
    burst = int(POLICIES["message"].capacity)
    outs = [await tg.send(1, f"msg {i}") for i in range(burst + 5)]
    handled = [o for o in outs if o and o[0].text == t.NOT_IN_CHAT]
    dropped_notices = [o for o in outs if o and o[0].text == t.SLOW_DOWN]
    assert len(handled) <= burst
    assert len(dropped_notices) == 1  # not one reply per dropped message


async def test_normal_pace_is_never_throttled(tg):
    await tg.onboard(1)
    tg.auto_advance = 1.0
    for i in range(30):
        out = await tg.send(1, f"msg {i}")
        assert all(o.text != t.SLOW_DOWN for o in out)


async def test_one_users_flood_does_not_affect_another(tg):
    await tg.onboard(1)
    await tg.onboard(2)
    tg.auto_advance = 0
    for _ in range(20):
        await tg.send(1, "spam")
    tg.auto_advance = 10
    out = await tg.send(2, "hello")
    assert out[0].text == t.NOT_IN_CHAT


async def test_persistent_flooding_earns_a_temporary_mute(tg, app_pool):
    await tg.onboard(1)
    tg.auto_advance = 0
    for i in range(int(POLICIES["message"].capacity) + MUTE_AFTER_VIOLATIONS + 2):
        await tg.send(1, f"spam {i}")
    assert await repo_safety.active_restriction(app_pool, 1, "mute") is not None


async def test_throttling_recovers_after_a_pause(tg):
    await tg.onboard(1)
    tg.auto_advance = 0
    for _ in range(12):
        await tg.send(1, "spam")
    tg.clock.advance(30)
    tg.auto_advance = 10
    assert (await tg.send(1, "back"))[0].text == t.NOT_IN_CHAT


async def test_button_presses_are_throttled_too(tg):
    await tg.onboard(1)
    tg.auto_advance = 0
    outs = [await tg.press(1, "cancel") for _ in range(int(POLICIES["callback"].capacity) + 6)]
    answers = [o for out in outs for o in out if o.kind == "AnswerCallbackQuery"]
    slow = [a for a in answers if a.method.text == t.SLOW_DOWN]
    assert 1 <= len(slow) <= 2


async def test_unknown_senders_are_throttled_before_any_database_work(tg, app_pool):
    tg.auto_advance = 0
    for _ in range(40):
        await tg.send(999, "/start")
    assert await app_pool.fetchval("SELECT count(*) FROM users WHERE id = 999") == 1

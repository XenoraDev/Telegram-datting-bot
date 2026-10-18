"""The inactivity close seen from Telegram: both people are told once, in their own language."""

from bot.main import close_idle_chats
from bot.texts.i18n import tr

A, B = 9301, 9302


async def chatting(tg):
    await tg.onboard(A)
    await tg.onboard(B, name="Sam")
    await tg.send(A, "💬 Random Chat")
    await tg.send(B, "💬 Random Chat")
    return await tg.services.matchmaking.current_chat(A)


def sent(out, uid):
    return [o.text for o in out if o.kind == "SendMessage" and o.chat_id == uid]


async def age(app_pool, minutes):
    await app_pool.execute(
        "UPDATE chat_sessions SET last_activity_at = now() - make_interval(mins => $1)",
        float(minutes),
    )


async def test_both_participants_are_told_when_a_silent_chat_closes(tg, app_pool):
    sid, _ = await chatting(tg)
    await age(app_pool, 11)
    tg.out.clear()
    assert await close_idle_chats(tg.services, tg.bot) == 1
    for uid in (A, B):
        texts = sent(tg.out, uid)
        assert tr("chat_ended_idle", "en", minutes=10) in texts
        assert tr("after_chat", "en") in texts  # the usual report / block prompt follows
    assert await tg.services.matchmaking.current_chat(A) is None
    assert sid


async def test_nothing_is_sent_while_the_chat_is_still_young(tg, app_pool):
    await chatting(tg)
    await age(app_pool, 9)
    tg.out.clear()
    assert await close_idle_chats(tg.services, tg.bot) == 0
    assert tg.out == []


async def test_a_second_run_does_not_notify_again(tg, app_pool):
    await chatting(tg)
    await age(app_pool, 11)
    assert await close_idle_chats(tg.services, tg.bot) == 1
    tg.out.clear()
    assert await close_idle_chats(tg.services, tg.bot) == 0
    assert tg.out == []


async def test_each_person_is_told_in_their_own_language(tg, app_pool):
    await chatting(tg)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", B)
    await age(app_pool, 11)
    tg.out.clear()
    await close_idle_chats(tg.services, tg.bot)
    assert tr("chat_ended_idle", "en", minutes=10) in sent(tg.out, A)
    hindi = tr("chat_ended_idle", "hi", minutes=10)
    assert hindi in sent(tg.out, B) and "10" in hindi and "मिनट" in hindi


async def test_after_the_close_both_can_chat_again_and_a_message_is_refused(tg, app_pool):
    await chatting(tg)
    await age(app_pool, 11)
    await close_idle_chats(tg.services, tg.bot)
    out = await tg.send(A, "anyone there?")
    assert not any(o.chat_id == B for o in out if o.kind == "SendMessage")
    out = await tg.send(A, "💬 Random Chat")
    assert any(o.kind == "SendMessage" and o.chat_id == A for o in out)


async def test_a_message_keeps_a_chat_open_through_the_sweep(tg, app_pool):
    await chatting(tg)
    await age(app_pool, 9)
    await tg.send(A, "still here")  # delivered: restarts the timer
    await age(app_pool, 0)  # the clock "moves" on
    assert await close_idle_chats(tg.services, tg.bot) == 0


async def test_a_blocked_or_unreachable_partner_does_not_stop_the_other_notice(tg, app_pool):
    await chatting(tg)
    await age(app_pool, 11)
    tg.unreachable.add(A)
    tg.out.clear()
    assert await close_idle_chats(tg.services, tg.bot) == 1
    assert tr("chat_ended_idle", "en", minutes=10) in sent(tg.out, B)

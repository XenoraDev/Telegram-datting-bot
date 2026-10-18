"""After support answers, "Reply to support" makes that ticket the live target for messages."""

import pytest
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers.common import notify_user
from bot.texts.i18n import tr
from tests.handlers.harness import ADMIN_ID

U1, U2, U3 = 6262001, 6262002, 6262003


def cbs(out):
    return [
        b.callback_data
        for o in out
        for row in (getattr(o.markup, "inline_keyboard", None) or [])
        for b in row
    ]


def mine(out, uid):
    return [o.text for o in out if o.kind == "SendMessage" and o.chat_id == uid]


async def new_ticket(tg, uid=U1, text="my payment is missing"):
    await tg.send(uid, "/support")
    await tg.press(uid, "sp:c:payment")
    await tg.send(uid, text)
    return (
        await tg.services.tickets.my_tickets(uid)
        and (await tg.services.tickets.my_tickets(uid))[0]["id"]
    )


async def admin_answers(tg, tid, text="we are checking"):
    """What the web panel does when an admin replies: store it, then notify with the button."""
    res = await tg.services.tickets.reply(ADMIN_ID, tid, text)
    assert res.kind == "ok"
    tg.out.clear()
    await notify_user(
        tg.bot,
        res.user_id,
        "support.admin_replied",
        tg.services,
        live_ticket=tid,
        id=tid,
        reply=text,
    )
    return list(tg.out)


async def messages(app_pool, tid):
    return [
        (r["sender"], r["ticket_text"])
        for r in await app_pool.fetch(
            "SELECT sender, ticket_text FROM support_messages WHERE ticket_id=$1 ORDER BY id", tid
        )
    ]


async def live_ids(app_pool):
    return [
        r["id"]
        for r in await app_pool.fetch(
            "SELECT id FROM support_tickets WHERE live_until > now() ORDER BY id"
        )
    ]


@pytest.fixture
async def ticket(tg):
    await tg.onboard(U1)
    return await new_ticket(tg)


# ------------------------------------------------------------------ the notification


async def test_the_admin_answer_arrives_with_the_ticket_number_and_a_reply_button(tg, ticket):
    out = await admin_answers(tg, ticket)
    (text,) = mine(out, U1)
    assert f"#{ticket}" in text and "we are checking" in text and "Reply to support" in text
    assert cbs(out) == [f"sp:l:{ticket}"]
    assert [b.text for o in out for r in o.markup.inline_keyboard for b in r] == [
        "💬 Reply to support"
    ]


async def test_the_button_and_text_are_in_hindi_for_a_hindi_user(tg, app_pool, ticket):
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", U1)
    out = await admin_answers(tg, ticket)
    assert [b.text for o in out for r in o.markup.inline_keyboard for b in r] == [
        tr("support.btn_live", "hi")
    ]
    assert "सहायता को जवाब दें" in mine(out, U1)[0] and f"#{ticket}" in mine(out, U1)[0]
    out = await tg.press(U1, f"sp:l:{ticket}")
    assert "टिकट #" in mine(out, U1)[-1] and cbs(out) == ["sp:s"]


async def test_the_existing_tickets_list_is_still_there(tg, ticket):
    out = await tg.send(U1, "/tickets")
    assert cbs(out) == [f"sp:v:{ticket}"]
    out = await tg.press(U1, f"sp:v:{ticket}")
    assert cbs(out) == [f"sp:r:{ticket}", f"sp:k:{ticket}"]  # read / reply / close as before


# ------------------------------------------------------------------ choosing the ticket


async def test_the_button_makes_that_exact_ticket_the_live_target(tg, app_pool, ticket):
    await admin_answers(tg, ticket)
    out = await tg.press(U1, f"sp:l:{ticket}")
    text = mine(out, U1)[-1]
    assert f"#{ticket}" in text and "30 minutes" in text and "go straight to that ticket" in text
    assert cbs(out) == ["sp:s"]
    assert await live_ids(app_pool) == [ticket]


async def test_plain_messages_then_go_to_that_ticket_without_opening_tickets(tg, app_pool, ticket):
    await admin_answers(tg, ticket)
    await tg.press(U1, f"sp:l:{ticket}")
    out = await tg.send(U1, "here is my UTR 123456")
    assert await messages(app_pool, ticket) == [
        ("user", "my payment is missing"),
        ("admin", "we are checking"),
        ("user", "here is my UTR 123456"),
    ]
    assert await app_pool.fetchval("SELECT status FROM support_tickets") == "open"
    assert "Sent to support" in mine(out, U1)[-1]
    assert any(f"#{ticket}" in a for a in tg.texts(ADMIN_ID))  # the admins are nudged


async def test_several_messages_in_a_row_all_go_to_the_same_ticket_with_one_acknowledgement(
    tg, app_pool, ticket
):
    await admin_answers(tg, ticket)
    await tg.press(U1, f"sp:l:{ticket}")
    tg.out.clear()
    for n in range(1, 4):
        await tg.send(U1, f"part {n}")
    assert [m for _, m in (await messages(app_pool, ticket))[-3:]] == ["part 1", "part 2", "part 3"]
    assert sum("Sent to support" in t for t in tg.texts(U1)) == 1  # not spammy
    assert sum("support ticket" in a or f"#{ticket}" in a for a in tg.texts(ADMIN_ID)) == 1
    assert await live_ids(app_pool) == [ticket]  # still live


async def test_with_several_tickets_only_the_chosen_one_is_live(tg, app_pool):
    await tg.onboard(U1)
    a = await new_ticket(tg, text="first problem")
    b = await new_ticket(tg, text="second problem")
    await admin_answers(tg, a)
    await admin_answers(tg, b)
    await tg.press(U1, f"sp:l:{b}")
    await tg.send(U1, "about the second")
    assert (await messages(app_pool, a))[-1] == ("admin", "we are checking")  # nothing for a
    assert (await messages(app_pool, b))[-1] == ("user", "about the second")
    # a new answer on ticket a does not change the target until its own button is pressed
    await admin_answers(tg, a, "another thing")
    await tg.send(U1, "still the second")
    assert (await messages(app_pool, b))[-1] == ("user", "still the second")
    await tg.press(U1, f"sp:l:{a}")
    assert await live_ids(app_pool) == [a]
    await tg.send(U1, "now the first")
    assert (await messages(app_pool, a))[-1] == ("user", "now the first")
    assert (await messages(app_pool, b))[-1] == ("user", "still the second")


async def test_nothing_is_live_until_the_button_is_pressed(tg, app_pool, ticket):
    await admin_answers(tg, ticket)
    await tg.send(U1, "this is not a support message")
    await tg.send(U1, "neither is this")
    assert len(await messages(app_pool, ticket)) == 2
    assert await live_ids(app_pool) == []


# ------------------------------------------------------------------ commands and menus


@pytest.mark.parametrize(
    "command", ["/help", "/start", "/tickets", "/support", "/premium", "/myplan"]
)
async def test_commands_work_normally_and_are_never_swallowed(tg, app_pool, ticket, command):
    await admin_answers(tg, ticket)
    await tg.press(U1, f"sp:l:{ticket}")
    out = await tg.send(U1, command)
    assert mine(out, U1), command  # the command was answered
    assert not any("Sent to support" in t for t in mine(out, U1))
    assert len(await messages(app_pool, ticket)) == 2  # the command text is no support message


async def test_a_command_or_menu_button_ends_live_mode_so_later_text_is_not_support(
    tg, app_pool, ticket
):
    await admin_answers(tg, ticket)
    await tg.press(U1, f"sp:l:{ticket}")
    await tg.send(U1, "/help")
    assert await live_ids(app_pool) == []
    await tg.send(U1, "hello, just talking")
    assert len(await messages(app_pool, ticket)) == 2
    await tg.press(U1, f"sp:l:{ticket}")
    await tg.send(U1, "💬 Random Chat")  # a menu button leaves too
    assert await live_ids(app_pool) == []


async def test_the_stop_button_ends_live_mode(tg, app_pool, ticket):
    await tg.press(U1, f"sp:l:{ticket}")
    out = await tg.press(U1, "sp:s")
    assert "no longer go to support" in mine(out, U1)[-1]
    assert await live_ids(app_pool) == []
    await tg.send(U1, "anyone?")
    assert len(await messages(app_pool, ticket)) == 1


async def test_the_old_one_shot_reply_button_still_takes_exactly_one_message(tg, app_pool, ticket):
    await tg.press(U1, f"sp:r:{ticket}")
    await tg.send(U1, "one reply")
    await tg.send(U1, "not a reply")
    assert [m for _, m in await messages(app_pool, ticket)] == [
        "my payment is missing",
        "one reply",
    ]
    assert await live_ids(app_pool) == []


# ------------------------------------------------------------------ closed tickets


async def test_a_closed_ticket_cannot_be_chosen(tg, app_pool, ticket):
    await tg.services.tickets.close(ADMIN_ID, ticket)
    out = await tg.press(U1, f"sp:l:{ticket}")
    assert (
        "can't take replies" in tg.callback_answers()[-1].text
        and tg.callback_answers()[-1].show_alert
    )
    assert mine(out, U1) == [] and await live_ids(app_pool) == []


@pytest.mark.parametrize("closer", ["admin", "user"])
async def test_closing_clears_live_mode_and_later_text_is_not_added(tg, app_pool, ticket, closer):
    await tg.press(U1, f"sp:l:{ticket}")
    assert await live_ids(app_pool) == [ticket]
    if closer == "admin":
        assert (await tg.services.tickets.close(ADMIN_ID, ticket)).kind == "ok"
    else:
        assert (await tg.services.tickets.close_by_user(U1, ticket)).kind == "ok"
    assert await live_ids(app_pool) == []
    assert await app_pool.fetchval("SELECT live_until FROM support_tickets") is None
    n = len(await messages(app_pool, ticket))
    await tg.send(U1, "are you still there?")
    assert len(await messages(app_pool, ticket)) == n


async def test_a_ticket_closed_between_two_messages_gets_nothing_more(tg, app_pool, ticket):
    await tg.press(U1, f"sp:l:{ticket}")
    await tg.send(U1, "first")
    await tg.services.tickets.close(ADMIN_ID, ticket)
    await tg.send(U1, "second")
    assert [m for _, m in await messages(app_pool, ticket)] == ["my payment is missing", "first"]


async def test_the_database_itself_refuses_a_live_closed_ticket(tg, app_pool, ticket):
    import asyncpg

    await tg.services.tickets.close(ADMIN_ID, ticket)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("UPDATE support_tickets SET live_until = now() + interval '5 min'")


async def test_the_service_says_no_live_for_a_closed_ticket(tg, app_pool, ticket):
    await tg.press(U1, f"sp:l:{ticket}")
    await app_pool.execute("UPDATE support_tickets SET live_until = now() - interval '1 s'")
    assert (await tg.services.tickets.send_live(U1, "late")).kind == "no_live"


async def test_a_full_ticket_stops_live_mode_with_a_message(tg, app_pool, ticket):
    await tg.press(U1, f"sp:l:{ticket}")
    await app_pool.execute(
        "INSERT INTO support_messages (ticket_id, sender, ticket_text) "
        "SELECT $1, 'user', 'filler' FROM generate_series(1, 40)",
        ticket,
    )
    out = await tg.send(U1, "one too many")
    assert mine(out, U1)  # the user is told
    assert await live_ids(app_pool) == []


# ------------------------------------------------------------------ ownership and routing safety


async def test_another_users_ticket_cannot_be_made_live(tg, app_pool, ticket):
    await tg.onboard(U2)
    await tg.press(U2, f"sp:l:{ticket}")
    assert "can't take replies" in tg.callback_answers()[-1].text
    assert await live_ids(app_pool) == []
    await tg.send(U2, "I am not the owner")
    assert len(await messages(app_pool, ticket)) == 1


async def test_a_forged_or_unknown_ticket_id_does_nothing(tg, app_pool, ticket):
    for bad in (ticket + 99, ticket + 1, 10**12):
        await tg.press(U1, f"sp:l:{bad}")
        assert await live_ids(app_pool) == []
    for bad in ("sp:l:", "sp:l:-1", "sp:l:abc", "sp:l:1:2", "sp:L:1"):
        assert [o for o in await tg.press(U1, bad) if o.kind == "SendMessage"] == []


async def test_live_mode_belongs_to_one_user_only(tg, app_pool, ticket):
    await tg.onboard(U2)
    other = await new_ticket(tg, uid=U2, text="mine")
    await tg.press(U1, f"sp:l:{ticket}")
    await tg.send(U2, "from user two")  # U2 never pressed anything
    assert (await messages(app_pool, other))[-1] == ("user", "mine")
    assert len(await messages(app_pool, ticket)) == 1


async def test_a_chat_message_is_never_sent_to_support(tg, app_pool, ticket):
    await tg.onboard(U2)
    await tg.press(U1, f"sp:l:{ticket}")
    assert (await tg.services.matchmaking.search(U1)).kind == "queued"  # no command: still live
    assert (await tg.services.matchmaking.search(U2)).kind == "matched"
    assert await live_ids(app_pool) == [ticket]
    out = await tg.send(U1, "hi stranger")
    assert any(o.chat_id == U2 and "hi stranger" in (o.text or "") for o in out)  # relayed
    assert len(await messages(app_pool, ticket)) == 1  # not a support message


async def test_support_buttons_stay_closed_during_a_chat(tg, app_pool, ticket):
    await tg.onboard(U2)
    await tg.services.matchmaking.search(U1)
    await tg.services.matchmaking.search(U2)
    await tg.press(U1, f"sp:l:{ticket}")
    assert "Finish your chat first" in tg.callback_answers()[-1].text
    assert await live_ids(app_pool) == []


async def test_another_conversation_has_priority_over_live_mode(tg, app_pool, ticket):
    from bot.handlers.support import SupportFlow  # noqa: F401

    await tg.press(U1, f"sp:l:{ticket}")
    await tg.send(U1, "/support")  # leaves live mode and opens the topic list
    out = await tg.press(U1, "sp:c:other")
    assert "Describe the problem" in mine(out, U1)[-1]
    await tg.send(U1, "a brand new ticket")
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 2
    assert len(await messages(app_pool, ticket)) == 1


# ------------------------------------------------------------------ persistence and limits


async def test_live_mode_survives_a_restart_of_the_in_memory_state(tg, app_pool, ticket):
    await tg.press(U1, f"sp:l:{ticket}")
    tg.dp.fsm.storage = MemoryStorage()  # what a bot restart does to FSM state
    await tg.send(U1, "after the restart")
    assert (await messages(app_pool, ticket))[-1] == ("user", "after the restart")


async def test_live_mode_expires_by_itself(tg, app_pool, ticket):
    await tg.press(U1, f"sp:l:{ticket}")
    await app_pool.execute("UPDATE support_tickets SET live_until = now() - interval '1 minute'")
    await tg.send(U1, "much later")
    assert len(await messages(app_pool, ticket)) == 1


async def test_each_message_pushes_the_deadline_forward(tg, app_pool, ticket):
    await tg.press(U1, f"sp:l:{ticket}")
    await app_pool.execute("UPDATE support_tickets SET live_until = now() + interval '1 minute'")
    await tg.send(U1, "keep it open")
    left = await app_pool.fetchval(
        "SELECT extract(epoch FROM live_until - now()) FROM support_tickets"
    )
    assert left > 20 * 60


async def test_empty_or_too_long_text_keeps_live_mode(tg, app_pool, ticket):
    await tg.press(U1, f"sp:l:{ticket}")
    out = await tg.send(U1, "x" * 2001)
    assert mine(out, U1) and len(await messages(app_pool, ticket)) == 1
    assert await live_ids(app_pool) == [ticket]
    await tg.send(U1, "ok now")
    assert (await messages(app_pool, ticket))[-1] == ("user", "ok now")


async def test_a_user_in_another_conversation_state_is_not_hijacked(tg, app_pool, ticket):
    """A plain text that belongs to another open conversation (here the payment UTR window)."""
    from bot.handlers.premium import PremiumFlow

    await tg.press(U1, f"sp:l:{ticket}")
    key = tg.dp.fsm.storage.__class__  # noqa: F841
    from aiogram.fsm.storage.base import StorageKey

    skey = StorageKey(bot_id=tg.bot.id, chat_id=U1, user_id=U1)
    await tg.dp.fsm.storage.set_state(skey, PremiumFlow.utr)
    await tg.send(U1, "some text for the other flow")
    assert len(await messages(app_pool, ticket)) == 1


async def test_the_support_messages_stay_in_the_existing_tables(tg, app_pool, ticket):
    await tg.press(U1, f"sp:l:{ticket}")
    await tg.send(U1, "secret words zzyy")
    cols = {
        r["column_name"]
        for r in await app_pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='support_tickets'"
        )
    }
    assert "live_until" in cols and not any("text" in c or "message" in c for c in cols)
    dump = str([dict(r) for r in await app_pool.fetch("SELECT * FROM support_tickets")])
    assert "zzyy" not in dump
    assert any(m == "secret words zzyy" for _, m in await messages(app_pool, ticket))

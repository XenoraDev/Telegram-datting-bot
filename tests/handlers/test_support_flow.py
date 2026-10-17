"""/support and /tickets end to end through the fake Telegram."""

import pytest

from bot import keyboards as kb
from tests import payment_images as img
from tests.handlers.harness import ADMIN_ID

U1, U2, U3 = 6161001, 6161002, 6161003


def cbs(out):
    return [
        b.callback_data
        for o in out
        for row in (getattr(o.markup, "inline_keyboard", None) or [])
        for b in row
    ]


def mine(out, uid):
    return [o.text for o in out if o.kind == "SendMessage" and o.chat_id == uid]


def last(out, uid):
    return mine(out, uid)[-1]


def alerts(tg):
    return tg.texts(ADMIN_ID)


async def new_ticket(tg, uid=U1, category="payment", text="my payment is missing"):
    await tg.send(uid, "/support")
    await tg.press(uid, f"sp:c:{category}")
    out = await tg.send(uid, text)
    return int(last(out, uid).split("#")[1].split("<")[0].split(" ")[0]), out


async def test_the_menu_has_a_support_button_that_lists_the_topics(tg):
    await tg.onboard(U1)
    assert kb.labels("btn.support") <= {b.text for r in kb.main_menu("en").keyboard for b in r} | {
        b.text for r in kb.main_menu("hi").keyboard for b in r
    }
    for opener in ("/support", "🆘 Support"):
        out = await tg.send(U1, opener)
        assert "What do you need help with" in last(out, U1)
        assert cbs(out) == [
            f"sp:c:{c}" for c in ("payment", "account", "chat", "report", "other")
        ] + ["sp:x"]


async def test_the_whole_flow_category_then_message_then_the_admin_is_told(tg, app_pool):
    await tg.onboard(U1)
    await tg.send(U1, "/support")
    out = await tg.press(U1, "sp:c:payment")
    assert "Describe the problem in one message" in last(out, U1) and cbs(out) == ["sp:x"]
    out = await tg.send(U1, "I paid but no premium")
    tid = await app_pool.fetchval("SELECT id FROM support_tickets")
    assert f"#{tid}" in last(out, U1) and "/tickets" in last(out, U1)
    t = await app_pool.fetchrow("SELECT * FROM support_tickets")
    assert (t["user_id"], t["category"], t["status"]) == (U1, "payment", "open")
    assert (
        await app_pool.fetchval("SELECT ticket_text FROM support_messages")
        == "I paid but no premium"
    )
    (note,) = [a for a in alerts(tg) if "support ticket" in a]
    assert f"#{tid}" in note and "payment" in note and "web admin" in note
    assert "I paid" not in note and str(U1) not in note  # the alert carries no user text or id
    # the conversation is over: the next message is an ordinary message, not another ticket
    await tg.send(U1, "hello?")
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 1


async def test_cancel_and_leaving_the_flow_create_nothing(tg, app_pool):
    await tg.onboard(U1)
    await tg.send(U1, "/support")
    await tg.press(U1, "sp:c:other")
    out = await tg.press(U1, "sp:x")
    assert "Nothing was sent" in last(out, U1)
    await tg.send(U1, "this is not a ticket")
    # a command / menu button in the middle ends the flow and runs normally
    for leave in ("/help", "🌐 Language", "⭐ Premium", "/payments"):
        await tg.send(U1, "/support")
        await tg.press(U1, "sp:c:chat")
        await tg.send(U1, leave)
        await tg.send(U1, "after leaving")
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 0


async def test_bad_text_keeps_the_conversation_open(tg, app_pool):
    await tg.onboard(U1)
    await tg.send(U1, "/support")
    await tg.press(U1, "sp:c:account")
    assert "too long" in last(await tg.send(U1, "x" * 2001), U1)
    assert "Please send some text" in last(await tg.photo_bytes(U1, img.JPEG), U1)
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 0
    out = await tg.send(U1, "ok, short now")  # still waiting for the text
    assert "was sent" in last(out, U1)


async def test_html_in_a_ticket_is_stored_as_text_and_shown_escaped(tg, app_pool):
    await tg.onboard(U1)
    tid, _ = await new_ticket(tg, text="<b>bold</b> & <script>x</script>")
    out = await tg.press(U1, f"sp:v:{tid}")
    shown = last(out, U1)
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; &lt;script&gt;" in shown and "<script>" not in shown


async def test_tickets_lists_opens_replies_and_closes(tg, app_pool):
    await tg.onboard(U1)
    assert "no tickets yet" in last(await tg.send(U1, "/tickets"), U1)
    tid, _ = await new_ticket(tg)
    out = await tg.send(U1, "/tickets")
    assert cbs(out) == [f"sp:v:{tid}"] and "Your tickets" in last(out, U1)
    assert "🟡" in out[-1].markup.inline_keyboard[0][0].text
    out = await tg.press(U1, f"sp:v:{tid}")
    assert "my payment is missing" in last(out, U1) and "waiting for support" in last(out, U1)
    assert cbs(out) == [f"sp:r:{tid}", f"sp:k:{tid}"]
    # answer from support, then a follow-up from the user
    await app_pool.execute(
        "INSERT INTO support_messages (ticket_id, sender, admin_id, ticket_text) "
        "VALUES ($1, 'admin', $2, 'did you pay?')",
        tid,
        ADMIN_ID,
    )
    await app_pool.execute("UPDATE support_tickets SET status = 'answered'")
    view = last(await tg.press(U1, f"sp:v:{tid}"), U1)
    assert "Support: did you pay?" in view and "You: my payment is missing" in view
    assert view.index("You:") < view.index("Support:")
    out = await tg.press(U1, f"sp:r:{tid}")
    assert "Send your reply" in last(out, U1)
    out = await tg.send(U1, "yes, UTR 123")
    assert "reply was sent" in last(out, U1)
    assert await app_pool.fetchval("SELECT status FROM support_tickets") == "open"
    assert any(f"#{tid}" in a and "wrote again" in a for a in alerts(tg))
    out = await tg.press(U1, f"sp:k:{tid}")
    assert f"Ticket #{tid} is closed" in last(out, U1)
    t = await app_pool.fetchrow("SELECT status, closed_by FROM support_tickets")
    assert (t["status"], t["closed_by"]) == ("closed", "user")
    assert cbs(await tg.press(U1, f"sp:v:{tid}")) == []  # nothing left to press
    out = await tg.press(U1, f"sp:r:{tid}")
    assert "already closed" in tg.callback_answers()[-1].text


async def test_a_fourth_open_ticket_is_refused(tg, app_pool):
    await tg.onboard(U1)
    for i in range(3):
        await new_ticket(tg, text=f"t{i}")
        tg.clock.advance(61)
    await tg.send(U1, "/support")
    await tg.press(U1, "sp:c:other")
    out = await tg.send(U1, "a fourth")
    assert "already have 3 open tickets" in last(out, U1)
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 3


async def test_somebody_elses_ticket_cannot_be_viewed_answered_or_closed(tg, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    tid, _ = await new_ticket(tg, uid=U1, text="private matter")
    for verb in ("v", "r", "k"):
        out = await tg.press(U2, f"sp:{verb}:{tid}")
        assert (
            "not found" in tg.callback_answers()[-1].text and tg.callback_answers()[-1].show_alert
        )
        assert "private matter" not in "".join(o.text for o in out)
    # also a reply typed after a (forged) state cannot reach it
    assert await app_pool.fetchval("SELECT status FROM support_tickets") == "open"
    assert await app_pool.fetchval("SELECT count(*) FROM support_messages") == 1
    for nowhere in (tid + 50, 1):
        await tg.press(U2, f"sp:v:{nowhere}")
        assert "not found" in tg.callback_answers()[-1].text


async def test_malformed_callbacks_do_nothing(tg):
    await tg.onboard(U1)
    for bad in ("sp:", "sp:v:", "sp:v:-1", "sp:z:1", "sp:c:hacking", "sp:v:1:2", "sp:v:abc"):
        out = await tg.press(U1, bad)
        assert [o for o in out if o.kind == "SendMessage"] == []


async def test_support_is_closed_during_a_chat(tg, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    tid, _ = await new_ticket(tg, uid=U1)
    await tg.send(U1, "/chat")
    await tg.send(U2, "/chat")
    assert "Finish your chat first" in last(await tg.send(U1, "/support"), U1)
    assert "Finish your chat first" in last(await tg.send(U1, "/tickets"), U1)
    await tg.press(U1, f"sp:v:{tid}")
    assert "Finish your chat first" in tg.callback_answers()[-1].text
    # the words are relayed to the partner as chat, never turned into a ticket
    await tg.send(U1, "hello partner")
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 1


async def test_everything_is_available_in_hindi(tg, app_pool):
    await tg.onboard(U1)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", U1)
    out = await tg.send(U1, "🆘 सहायता")
    assert "सहायता" in last(out, U1) and "भुगतान" in out[-1].markup.inline_keyboard[0][0].text
    await tg.press(U1, "sp:c:payment")
    out = await tg.send(U1, "मेरा भुगतान नहीं दिखा")
    assert "भेज दिया गया" in last(out, U1)
    tid = await app_pool.fetchval("SELECT id FROM support_tickets")
    view = last(await tg.press(U1, f"sp:v:{tid}"), U1)
    assert "टिकट" in view and "आप: मेरा भुगतान नहीं दिखा" in view


async def test_deleteme_removes_the_tickets(tg, app_pool):
    await tg.onboard(U1)
    await new_ticket(tg, text="remove me")
    assert await app_pool.fetchval("SELECT count(*) FROM support_messages") == 1
    await tg.send(U1, "/deleteme")
    await tg.press(U1, "del:yes")
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 0
    assert await app_pool.fetchval("SELECT count(*) FROM support_messages") == 0


def test_the_callback_data_is_strictly_parsed_and_short():
    assert kb.parse_support_cb("sp:c:payment") == ("c", "payment")
    assert kb.parse_support_cb("sp:v:12") == ("v", 12)
    assert kb.parse_support_cb("sp:x") == ("x", 0)
    for bad in (
        None,
        "",
        "sp:c:",
        "sp:c:Payment",
        "sp:v:0x1",
        "sp:r:" + "9" * 13,
        "SP:x",
        "sp:x:1",
    ):
        assert kb.parse_support_cb(bad) is None
    for data in (
        cb
        for r in kb.support_categories("en").inline_keyboard
        for b in r
        for cb in [b.callback_data]
    ):
        assert data.isascii() and len(data.encode()) <= 64


@pytest.mark.parametrize("lang", ["en", "hi"])
def test_ticket_buttons_carry_no_text_only_ids(lang):
    rows = [(7, "payment", "open"), (8, "other", "closed")]
    data = [b.callback_data for r in kb.ticket_list(rows, lang).inline_keyboard for b in r]
    assert data == ["sp:v:7", "sp:v:8"]
    assert kb.ticket_actions(7, True, lang) is None


async def test_a_notice_does_not_swap_the_chat_buttons_for_the_main_menu(tg):
    from bot.handlers.common import notify_user

    await tg.onboard(U1)
    await tg.onboard(U2)
    await notify_user(tg.bot, U1, "support.admin_closed", tg.services, id=5)
    assert {b.text for r in tg.out[-1].markup.keyboard for b in r} >= {"💬 Random Chat"}
    await tg.send(U1, "/chat")
    await tg.send(U2, "/chat")
    await notify_user(tg.bot, U1, "support.admin_closed", tg.services, id=5)
    assert [b.text for r in tg.out[-1].markup.keyboard for b in r] == [
        "➡️ Next",
        "🛑 Stop",
        "🚨 Report",
    ]

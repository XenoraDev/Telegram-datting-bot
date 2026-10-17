"""💬 Chat on a card in /matches: Like -> Match -> /matches -> Chat opens the human chat."""

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from bot import keyboards as kb
from bot.db import repo_safety
from bot.texts.i18n import tr
from tests.factories import give_tier

A, B, C, D = 7400001, 7400002, 7400003, 7400004
NAMES = {A: "Ann", B: "Bob", C: "Cee", D: "Dee"}  # nothing derived from the Telegram id


async def handle(pool, uid):
    return (await pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", uid)).hex


def buttons(out):
    return [
        b for o in out for row in (getattr(o.markup, "inline_keyboard", None) or []) for b in row
    ]


def inline(out):
    """Only the messages that carry inline buttons (the others carry reply keyboards)."""
    return [o for o in out if getattr(o.markup, "inline_keyboard", None)]


def to(out, uid):
    return [o.text for o in out if o.kind == "SendMessage" and o.chat_id == uid]


async def match(tg, pool, a=A, b=B):
    """Both onboarded (if needed) and liked each other."""
    for uid in (a, b):
        if await pool.fetchval("SELECT 1 FROM users WHERE id = $1", uid) is None:
            await tg.onboard(uid, name=NAMES[uid], city="Rome")
    await tg.press(a, f"lk:{await handle(pool, b)}")
    await tg.press(b, f"lk:{await handle(pool, a)}")


async def sessions(pool):
    return await pool.fetch("SELECT * FROM chat_sessions ORDER BY id")


async def active(pool):
    return await pool.fetchval("SELECT count(*) FROM chat_sessions WHERE status = 'active'")


# ------------------------------------------------------------------ the button


async def test_every_valid_match_card_has_a_chat_button_above_report_and_block(tg, app_pool):
    await match(tg, app_pool, A, B)
    await match(tg, app_pool, A, C)
    out = [o for o in inline(await tg.send(A, "/matches")) if o.chat_id == A]
    assert len(out) == 2
    for o in out:
        rows = o.markup.inline_keyboard
        assert [b.text for b in rows[0]] == ["💬 Chat"]  # full width, its own row, on top
        assert [b.text for b in rows[1]] == ["🚨 Report", "🚫 Block"]
        assert all(b.callback_data.split(":")[0] in ("mc", "mr", "mk") for r in rows for b in r)
    assert {rows.markup.inline_keyboard[0][0].callback_data for rows in out} == {
        f"mc:{await handle(app_pool, B)}",
        f"mc:{await handle(app_pool, C)}",
    }


async def test_the_button_data_is_only_the_opaque_handle(tg, app_pool):
    await match(tg, app_pool)
    out = await tg.send(A, "/matches")
    for b in buttons(out):
        assert b.callback_data.isascii() and len(b.callback_data.encode()) <= 64
        for secret in (str(A), str(B), f"user{B}", "@", "Rome", "Bob"):
            assert secret not in b.callback_data
    texts = " ".join(to(out, A))
    assert "Bob" in texts  # the card shows the profile name, as /matches always did
    assert "@" not in texts and str(B) not in texts and f"user{B}" not in texts


# ------------------------------------------------------------------ opening the chat


async def test_tapping_chat_opens_a_human_chat_for_both_sides(tg, app_pool):
    await match(tg, app_pool)
    out = await tg.press(A, f"mc:{await handle(app_pool, B)}")
    (session,) = await sessions(app_pool)
    assert {session["user_a"], session["user_b"]} == {A, B} and session["status"] == "active"
    header = tr("match_chat.header", "en")
    assert header in to(out, A) and header in to(out, B)  # both are told, in their own chat
    for uid in (A, B):  # each gets the other's card (name hidden: the usual chat card)
        card = [t for t in to(out, uid) if "Partner Profile" in t]
        assert card and "@" not in card[0] and "Ann" not in " ".join(to(out, B))
        keyboard = [o for o in out if o.chat_id == uid and o.kind == "SendMessage"][0].markup
        assert [b.text for r in keyboard.keyboard for b in r] == ["➡️ Next", "🛑 Stop", "🚨 Report"]
    # and they really can talk
    assert to(await tg.send(A, "hello there"), B) == ["hello there"]
    assert to(await tg.send(B, "hi back"), A) == ["hi back"]


async def test_the_partner_card_still_follows_privacy_and_plan_rules(tg, app_pool):
    await match(tg, app_pool)
    await app_pool.execute("UPDATE profiles SET show_city = false WHERE user_id = $1", B)
    out = await tg.press(A, f"mc:{await handle(app_pool, B)}")
    card = [t for t in to(out, A) if "Partner Profile" in t][0]
    assert "Hidden by the user" in card and "Rome" not in card  # owner privacy wins
    assert "Age: ••" in card  # a Free viewer does not see age; the 👁 button is there
    assert any(b.callback_data.startswith("pc:r:") for b in buttons(out))
    await tg.send(A, "/stop")
    await give_tier(app_pool, A, "premium")
    out = await tg.press(A, f"mc:{await handle(app_pool, B)}")
    assert "Age: 25" in [t for t in to(out, A) if "Partner Profile" in t][0]


async def test_tapping_again_reuses_the_chat_and_never_makes_a_second_session(tg, app_pool):
    await match(tg, app_pool)
    pid = await handle(app_pool, B)
    await tg.press(A, f"mc:{pid}")
    for presser, target in ((A, B), (A, B), (B, A)):
        out = await tg.press(presser, f"mc:{await handle(app_pool, target)}")
        assert tr("match_chat.already", "en") in to(out, presser)
        assert [o for o in out if o.kind == "SendMessage" and o.chat_id == target] == []
    assert len(await sessions(app_pool)) == 1 and await active(app_pool) == 1


async def test_a_chat_that_ended_can_be_opened_again_as_a_new_session(tg, app_pool):
    await match(tg, app_pool)
    pid = await handle(app_pool, B)
    await tg.press(A, f"mc:{pid}")
    await tg.send(A, "/stop")
    await tg.press(A, f"mc:{pid}")  # no "recently chatted" rule applies to a match
    assert len(await sessions(app_pool)) == 2 and await active(app_pool) == 1


async def test_it_does_not_use_up_a_random_chat_start(tg, app_pool):
    await match(tg, app_pool)
    await tg.press(A, f"mc:{await handle(app_pool, B)}")
    assert await tg.services.entitlements.used_today(A, "random_chat") == 0


async def test_a_searching_user_leaves_the_queue_when_the_chat_opens(tg, app_pool):
    await match(tg, app_pool)
    await tg.send(A, "/chat")
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 1
    await tg.press(A, f"mc:{await handle(app_pool, B)}")
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 0
    assert await active(app_pool) == 1


# ------------------------------------------------------------------ who may use it


async def test_nobody_can_use_somebody_elses_match(tg, app_pool):
    await match(tg, app_pool, A, B)
    await tg.onboard(C, name="Cee", city="Pune")
    pid_b = await handle(app_pool, B)
    pid_a = await handle(app_pool, A)
    out = await tg.press(C, f"mc:{pid_b}")  # C was never matched with B
    assert tr("match_chat.unavailable", "en") in to(out, C) and await sessions(app_pool) == []
    assert to(out, B) == []  # B is not bothered
    out = await tg.press(C, f"mc:{pid_a}")
    assert tr("match_chat.unavailable", "en") in to(out, C) and await sessions(app_pool) == []
    # a like that is not (yet) mutual is not a match either
    await tg.press(C, f"lk:{pid_b}")
    out = await tg.press(C, f"mc:{pid_b}")
    assert tr("match_chat.unavailable", "en") in to(out, C) and await sessions(app_pool) == []


async def test_a_match_with_oneself_or_a_made_up_handle_does_nothing(tg, app_pool):
    await match(tg, app_pool)
    for data in (f"mc:{await handle(app_pool, A)}", f"mc:{'0' * 32}", f"mc:{'f' * 32}"):
        out = await tg.press(A, data)
        assert tr("match_chat.unavailable", "en") in to(out, A)
    for bad in ("mc:", "mc:xyz", "mc:" + "A" * 32, "mc:1", "mc:" + "a" * 31, "mc:" + "a" * 33):
        out = await tg.press(A, bad)
        assert [o for o in out if o.kind == "SendMessage"] == []
    assert await sessions(app_pool) == []


async def test_a_stale_match_after_the_other_person_deleted_their_account(tg, app_pool):
    await match(tg, app_pool)
    pid = await handle(app_pool, B)
    await tg.press(B, "del:yes")
    out = await tg.press(A, f"mc:{pid}")
    assert to(out, A) == [tr("match_chat.unavailable", "en")]
    assert await sessions(app_pool) == []


async def test_a_blocked_match_cannot_be_chatted_with_from_either_side(tg, app_pool):
    await match(tg, app_pool)
    pid_a, pid_b = await handle(app_pool, A), await handle(app_pool, B)
    await repo_safety.add_block(app_pool, B, A)  # B blocked A
    for presser, pid in ((A, pid_b), (B, pid_a)):
        out = await tg.press(presser, f"mc:{pid}")
        assert to(out, presser) == [tr("match_chat.unavailable", "en")]  # same answer, no hint
    assert await sessions(app_pool) == []


async def test_a_banned_or_unfinished_partner_is_unavailable(tg, app_pool):
    await match(tg, app_pool)
    pid = await handle(app_pool, B)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", B)
    out = await tg.press(A, f"mc:{pid}")
    assert to(out, A) == [tr("match_chat.unavailable", "en")]
    assert await sessions(app_pool) == []


async def test_the_viewers_own_status_and_restrictions_are_respected(tg, app_pool):
    await match(tg, app_pool)
    pid = await handle(app_pool, B)
    await app_pool.execute(
        "INSERT INTO restrictions (user_id, kind, until) "
        "VALUES ($1, 'chat_cooldown', now() + interval '1 hour')",
        A,
    )
    out = await tg.press(A, f"mc:{pid}")
    assert tr("cooldown", "en") in to(out, A) and await sessions(app_pool) == []
    await app_pool.execute("DELETE FROM restrictions")
    await app_pool.execute(  # the PARTNER is restricted: unavailable, without saying why
        "INSERT INTO restrictions (user_id, kind, until) "
        "VALUES ($1, 'chat_cooldown', now() + interval '1 hour')",
        B,
    )
    out = await tg.press(A, f"mc:{pid}")
    assert to(out, A) == [tr("match_chat.unavailable", "en")] and await sessions(app_pool) == []
    await app_pool.execute("DELETE FROM restrictions")
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", A)
    out = await tg.press(A, f"mc:{pid}")  # a banned user is stopped before any handler
    assert await sessions(app_pool) == []


async def test_existing_chats_decide(tg, app_pool):
    await match(tg, app_pool, A, B)
    await match(tg, app_pool, A, C)
    await tg.onboard(D, name="Dee")
    # A is chatting with C: B's card cannot take A away from that chat
    await tg.press(A, f"mc:{await handle(app_pool, C)}")
    out = await tg.press(A, f"mc:{await handle(app_pool, B)}")
    assert to(out, A) == [tr("match_chat.busy", "en")]
    assert await active(app_pool) == 1
    # B is busy with somebody else: A (free now) is told so, nothing is created
    await tg.send(A, "/stop")
    await match(tg, app_pool, B, D)
    await tg.press(B, f"mc:{await handle(app_pool, D)}")
    out = await tg.press(A, f"mc:{await handle(app_pool, B)}")
    assert to(out, A) == [tr("match_chat.partner_busy", "en")]
    assert to(out, B) == []
    assert await active(app_pool) == 1


async def test_a_partner_who_cannot_be_reached_leaves_no_dead_chat(tg, app_pool):
    await match(tg, app_pool)
    tg.fail_chat[B] = TelegramForbiddenError(SendMessage(chat_id=B, text="x"), "bot was blocked")
    out = await tg.press(A, f"mc:{await handle(app_pool, B)}")
    assert tr("match_chat.unavailable", "en") in to(out, A)
    assert await active(app_pool) == 0  # the session was ended again
    assert await app_pool.fetchval("SELECT end_reason FROM chat_sessions") == "partner_unreachable"


async def test_pressing_it_over_and_over_is_rate_limited(tg, app_pool):
    await match(tg, app_pool)
    pid = await handle(app_pool, B)
    tg.auto_advance = 0.0
    await tg.press(A, f"mc:{pid}")
    await tg.send(A, "/stop")
    seen = []
    for _ in range(8):
        seen += to(await tg.press(A, f"mc:{pid}"), A)
        await tg.send(A, "/stop")
    assert tr("match_chat.rate", "en") in seen


# ------------------------------------------------------------------ report and block still work


async def test_report_and_block_on_the_same_card_still_work(tg, app_pool):
    await match(tg, app_pool)
    pid = await handle(app_pool, B)
    out = await tg.press(A, f"mr:{pid}")
    assert any(b.callback_data == f"rm:{pid}:spam" for b in buttons(out))
    out = await tg.press(A, f"rm:{pid}:spam")
    assert tr("report_created", "en") in to(out, A)
    assert await app_pool.fetchval("SELECT count(*) FROM reports WHERE reported_id = $1", B) == 1
    out = await tg.press(A, f"mk:{pid}")
    assert tr("profile_blocked", "en") in to(out, A)
    assert await repo_safety.is_blocked_either_way(app_pool, A, B)
    out = await tg.press(A, f"mc:{pid}")  # and a blocked match can no longer be chatted with
    assert to(out, A) == [tr("match_chat.unavailable", "en")] and await sessions(app_pool) == []


async def test_blocking_from_inside_the_match_chat_ends_it_and_chat_cannot_restart(tg, app_pool):
    await match(tg, app_pool)
    pid = await handle(app_pool, B)
    await tg.press(A, f"mc:{pid}")
    out = await tg.press(A, f"mk:{pid}")  # the old card's button, mid-chat
    assert tr("profile_blocked", "en") in to(out, A)
    assert await active(app_pool) == 0
    assert to(await tg.press(A, f"mc:{pid}"), A) == [tr("match_chat.unavailable", "en")]


# ------------------------------------------------------------------ Hindi


async def test_the_button_and_every_answer_are_in_hindi(tg, app_pool):
    await match(tg, app_pool)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", A)
    out = await tg.send(A, "/matches")
    rows = inline(out)[0].markup.inline_keyboard
    assert [b.text for b in rows[0]] == ["💬 चैट"]
    pid = await handle(app_pool, B)
    out = await tg.press(A, f"mc:{pid}")
    assert tr("match_chat.header", "hi") in to(out, A)
    assert tr("match_chat.header", "en") in to(out, B)  # B reads it in English
    assert tr("match_chat.already", "hi") in to(await tg.press(A, f"mc:{pid}"), A)
    out = await tg.press(A, f"mc:{'0' * 32}")
    assert to(out, A) == [tr("match_chat.unavailable", "hi")]


KEYS = (
    "match_chat_btn",
    "match_chat.header",
    "match_chat.already",
    "match_chat.busy",
    "match_chat.partner_busy",
    "match_chat.unavailable",
    "match_chat.rate",
)


def test_the_parser_accepts_only_a_handle_and_the_texts_exist_in_both_languages():
    pid = "a" * 32
    assert kb.parse_profile_cb(f"mc:{pid}") == ("mc", pid)
    for bad in (None, "", "mc:", f"mc:{pid}1", f"mc:{pid[:-1]}", f"mc:{'G' * 32}", f"MC:{pid}"):
        assert kb.parse_profile_cb(bad) is None
    for lang in ("en", "hi"):
        for key in KEYS:
            assert tr(key, lang).strip()
        data = [b.callback_data for r in kb.match_card(pid, lang).inline_keyboard for b in r]
        assert data == [f"mc:{pid}", f"mr:{pid}", f"mk:{pid}"]

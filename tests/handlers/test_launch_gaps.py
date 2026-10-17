"""A5 launch gaps through the fake Telegram: errors, Telegram rate limits, terms and about, the
language picker, the random-chat queue, and recovery after a restart."""

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage

from bot import keyboards as kb
from bot.main import run_maintenance
from bot.services.alerts import WINDOW
from bot.texts import en as t
from bot.texts.i18n import tr
from tests.factories import make_user
from tests.handlers.harness import ADMIN_ID

A, B, C = 7100001, 7100002, 7100003
SECRET = "postgresql://user:hunter2@db/secret-db BOT_TOKEN=123:ABC"


def sent(out, uid):
    return [o.text for o in out if o.kind == "SendMessage" and o.chat_id == uid]


def callback_texts(tg):
    return [a.text for a in tg.callback_answers()]


def error_alerts(tg):
    return [m for m in tg.texts(ADMIN_ID) if "Bot error" in m]


class Boom:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        raise self.exc


def restart(tg):
    """What a restart does to the conversation state: it is memory, so it is gone."""
    tg.dp.fsm.storage = MemoryStorage()


@pytest.fixture
def waits(monkeypatch):
    seen: list[float] = []

    async def fake_sleep(seconds):
        seen.append(seconds)

    monkeypatch.setattr("bot.telegram_send.asyncio.sleep", fake_sleep)
    return seen


def rate_limited(seconds=1):
    return TelegramRetryAfter(SendMessage(chat_id=1, text="x"), "Too Many Requests", seconds)


# ------------------------------------------------------------------ 1. the global error handler


async def test_an_unexpected_error_gives_the_user_a_safe_line_and_the_admin_an_alert(
    tg, monkeypatch
):
    await tg.onboard(A)
    monkeypatch.setattr(tg.services.language, "set", Boom(RuntimeError(SECRET)))
    out = await tg.press(A, "lg:hi")
    assert callback_texts(tg)[-1] == tr("error.generic", "en")
    assert tg.callback_answers()[-1].show_alert
    shown = " ".join(sent(out, A)) + " ".join(callback_texts(tg))  # what the user sees
    for leak in ("hunter2", "BOT_TOKEN", "RuntimeError", "Traceback", "postgresql://", ".py"):
        assert leak not in shown
    (alert,) = error_alerts(tg)
    assert "RuntimeError" in alert and "1×" in alert and "language.py" in alert
    for leak in ("hunter2", "BOT_TOKEN", "postgresql://", str(A)):
        assert leak not in alert  # kind and place only: no message, no user id


async def test_a_message_that_crashes_gets_the_safe_line_in_the_users_language(tg, monkeypatch):
    await tg.onboard(A)
    await tg.press(A, "lg:hi")
    monkeypatch.setattr(tg.services.matchmaking, "search", Boom(ValueError(SECRET)))
    out = await tg.send(A, "/chat")
    assert sent(out, A) == [tr("error.generic", "hi")]
    assert not any("hunter2" in o.text for o in out)


async def test_a_database_failure_while_finding_the_language_still_answers_in_english(
    tg, monkeypatch
):
    await tg.onboard(A)
    monkeypatch.setattr(tg.services.matchmaking, "search", Boom(RuntimeError("db down")))
    monkeypatch.setattr(tg.services.language, "get", Boom(RuntimeError("db down")))
    out = await tg.send(A, "/chat")
    assert sent(out, A) == [tr("error.generic", "en")]


async def test_admin_alerts_are_throttled_and_count_what_was_held_back(tg, monkeypatch):
    await tg.onboard(A)
    monkeypatch.setattr(tg.services.matchmaking, "search", Boom(RuntimeError("x")))
    tg.auto_advance = 20.0  # past the one-notice-per-8s limiter, well inside the alert window
    for _ in range(5):
        await tg.send(A, "/chat")
    assert len(error_alerts(tg)) == 1
    tg.clock.advance(WINDOW)
    await tg.send(A, "/chat")
    alerts = error_alerts(tg)
    assert len(alerts) == 2 and "5×" in alerts[1]
    monkeypatch.setattr(tg.services.matchmaking, "search", Boom(KeyError("another")))
    await tg.send(A, "/chat")
    assert len(error_alerts(tg)) == 3  # a different kind of error is announced at once


async def test_users_get_at_most_one_error_notice_at_a_time(tg, monkeypatch):
    await tg.onboard(A)
    monkeypatch.setattr(tg.services.matchmaking, "search", Boom(RuntimeError("x")))
    tg.auto_advance = 0.0
    notices = [sent(await tg.send(A, "/chat"), A) for _ in range(4)]
    assert notices[0] == [tr("error.generic", "en")] and notices[1:] == [[], [], []]


@pytest.mark.parametrize(
    "exc", [TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "bot was blocked")]
)
async def test_a_user_who_blocked_the_bot_is_not_an_error(tg, monkeypatch, exc):
    await tg.onboard(A)
    monkeypatch.setattr(tg.services.matchmaking, "search", Boom(exc))
    out = await tg.send(A, "/chat")
    assert sent(out, A) == [] and error_alerts(tg) == []


async def test_a_crash_in_the_error_path_never_escapes(tg, monkeypatch):
    await tg.onboard(A)
    monkeypatch.setattr(tg.services.matchmaking, "search", Boom(RuntimeError("x")))
    monkeypatch.setattr(tg.services.alerts, "alert", Boom(RuntimeError("alerting broke")))
    out = await tg.send(A, "/chat")  # does not raise
    assert sent(out, A) == [tr("error.generic", "en")]


# ------------------------------------------------------------------ 2. Telegram rate limits


async def test_a_notice_to_a_user_survives_a_telegram_rate_limit(tg, waits):
    from bot.handlers.common import notify_user

    await tg.onboard(A)
    tg.send_errors = [rate_limited(3)]
    await notify_user(tg.bot, A, "support.admin_closed", tg.services, id=7)
    assert waits == [3]
    assert "#7" in tg.texts(A)[-1]
    assert [m for m in tg.texts(ADMIN_ID) if "keep failing" in m] == []


async def test_a_failure_that_does_not_go_away_alerts_the_admin_once(tg, waits):
    from bot.handlers.common import notify_user

    await tg.onboard(A)
    before = len(tg.texts(A))
    tg.send_errors = [rate_limited(1)] * 3  # the first try and both retries
    await notify_user(tg.bot, A, "support.admin_closed", tg.services, id=7)
    assert waits == [1, 1] and len(tg.texts(A)) == before  # two retries, then given up
    (alert,) = [m for m in tg.texts(ADMIN_ID) if "keep failing" in m]
    assert "TelegramRetryAfter" in alert
    tg.send_errors = [rate_limited(1)] * 3
    await notify_user(tg.bot, A, "support.admin_closed", tg.services, id=8)
    assert len([m for m in tg.texts(ADMIN_ID) if "keep failing" in m]) == 1  # throttled


async def test_stopping_a_chat_still_works_when_the_partner_cannot_be_told(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    tg.fail_chat[B] = TelegramForbiddenError(SendMessage(chat_id=B, text="x"), "blocked")
    out = await tg.send(A, "/stop")
    assert t.CHAT_ENDED_ME in sent(out, A)
    assert await app_pool.fetchval("SELECT count(*) FROM chat_sessions WHERE ended_at IS NULL") == 0
    assert error_alerts(tg) == []


async def test_a_rate_limited_relay_keeps_the_chat_and_asks_for_a_resend(tg, app_pool, waits):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    tg.send_errors = [rate_limited(1)] * 3
    out = await tg.send(A, "hello there")
    assert sent(out, A) == [tr("chat.delivery_delayed", "en")]
    assert waits == [1, 1]
    # the chat was not ended as if the partner had left
    assert await app_pool.fetchval("SELECT count(*) FROM chat_sessions WHERE ended_at IS NULL") == 1
    out = await tg.send(A, "second try")
    assert sent(out, B) == ["second try"]
    assert any("keep failing" in m for m in tg.texts(ADMIN_ID))


async def test_one_rate_limit_in_a_relay_is_simply_retried(tg, waits):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    tg.send_errors = [rate_limited(2)]
    out = await tg.send(A, "hello")
    assert sent(out, B) == ["hello"] and waits == [2] and sent(out, A) == []


async def test_the_expiry_notice_survives_a_rate_limit_and_a_blocked_bot(tg, app_pool, waits):
    await tg.onboard(A)
    await tg.onboard(B)
    for uid in (A, B):
        await tg.send(uid, "/chat") if uid == A else None
    await app_pool.execute("UPDATE chat_queue SET joined_at = now() - interval '40 minutes'")
    tg.send_errors = [rate_limited(2)]
    await run_maintenance(tg.services, tg.bot)
    assert waits == [2] and t.SEARCH_EXPIRED in tg.texts(A)
    # a user who blocked the bot does not stop the run
    await tg.send(B, "/chat")
    await app_pool.execute("UPDATE chat_queue SET joined_at = now() - interval '40 minutes'")
    tg.fail_chat[B] = TelegramForbiddenError(SendMessage(chat_id=B, text="x"), "blocked")
    await run_maintenance(tg.services, tg.bot)
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 0


# ------------------------------------------------------------------ 3. terms and about


@pytest.mark.parametrize("cmd", ["/terms", "/about"])
async def test_terms_and_about_are_readable_by_anyone(tg, app_pool, cmd):
    key = cmd[1:]
    assert sent(await tg.send(C, cmd), C) == [tr(key, "en")]  # no account at all
    await tg.onboard(A)
    assert sent(await tg.send(A, cmd), A) == [tr(key, "en")]
    await make_user(app_pool, B, status="banned")
    assert sent(await tg.send(B, cmd), B) == [tr(key, "en")]  # even a banned user may read them
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", A)
    assert sent(await tg.send(A, cmd), A) == [tr(key, "hi")]


async def test_help_lists_terms_and_about(tg):
    await tg.onboard(A)
    help_text = sent(await tg.send(A, "/help"), A)[0]
    assert "/terms" in help_text and "/about" in help_text


def test_terms_and_about_state_only_what_the_product_does():
    from bot.services.chat_relay import BUFFER_TTL_SECONDS
    from bot.services.payments import EVIDENCE_RETENTION_DAYS

    for lang in ("en", "hi"):
        about = tr("about", lang)
        assert str(int(BUFFER_TTL_SECONDS // 60)) in about  # minutes the last messages stay in RAM
        assert str(EVIDENCE_RETENTION_DAYS) in about and "30" in about
        assert "/deleteme" in about and "/privacy" in about and "/terms" in about
        terms = tr("terms", lang)
        assert "18" in terms and "/about" in terms
        assert len(terms) < 3500 and len(about) < 3500  # one Telegram message each


# ------------------------------------------------------------------ 4. language before the gate


async def test_the_first_start_asks_for_a_language_before_the_age_gate(tg, app_pool):
    out = await tg.send(A, "/start")
    (text,) = sent(out, A)
    assert tr("start.pick_language", "en") in text and tr("start.pick_language", "hi") in text
    assert [b.callback_data for b in out[-1].markup.inline_keyboard[0]] == ["lg:en", "lg:hi"]
    assert "18" not in text  # the gate has not been shown yet
    assert await app_pool.fetchval("SELECT age_confirmed_at FROM users WHERE id = $1", A) is None


@pytest.mark.parametrize("code", ["en", "hi"])
async def test_choosing_a_language_shows_the_age_gate_in_it_and_signup_continues(
    tg, app_pool, code
):
    await tg.send(A, "/start")
    out = await tg.press(A, f"lg:{code}")
    assert sent(out, A) == [tr("lang.changed", code), tr("age_gate", code)]
    gate = [b.callback_data for row in out[-1].markup.inline_keyboard for b in row]
    assert gate == ["age:yes", "age:no"]
    assert await app_pool.fetchval("SELECT language FROM users WHERE id = $1", A) == code
    out = await tg.press(A, "age:yes")  # the existing onboarding carries on, in that language
    assert tr("ask_name", code) in sent(out, A)
    assert await app_pool.fetchval("SELECT age_confirmed_at FROM users WHERE id = $1", A)


async def test_saying_no_at_the_gate_after_the_picker_still_blocks_a_minor(tg, app_pool):
    await tg.send(A, "/start")
    await tg.press(A, "lg:en")
    out = await tg.press(A, "age:no")
    assert t.BLOCKED_ADULTS_ONLY in sent(out, A)
    assert (
        await app_pool.fetchval("SELECT status FROM users WHERE id = $1", A) == "blocked_underage"
    )
    assert (await tg.send(A, "/start"))[0].text == t.BLOCKED_ADULTS_ONLY


async def test_nobody_sees_the_picker_again_after_the_gate_or_when_active(tg, app_pool):
    await tg.onboard(A)
    assert sent(await tg.send(A, "/start"), A) == [tr("welcome_back", "en")]
    await tg.send(B, "/start")
    await tg.press(B, "age:yes")  # confirmed the age, profile not finished
    out = await tg.send(B, "/start")
    assert tr("start.pick_language", "en") not in " ".join(sent(out, B))
    assert tr("ask_name", "en") in sent(out, B)


async def test_changing_language_later_does_not_repeat_the_gate(tg):
    await tg.send(A, "/start")
    await tg.press(A, "age:yes")
    out = await tg.press(A, "lg:hi")
    assert sent(out, A) == [tr("lang.changed", "hi")]


# ------------------------------------------------------------------ 5. the queue


async def test_the_waiting_message_is_clear_and_points_to_browse(tg):
    await tg.onboard(A)
    text = sent(await tg.send(A, "/chat"), A)[0]
    assert text == t.SEARCHING
    for needle in ("message you", "Browse", "/stop"):
        assert needle in text
    assert len(text) < 110 and len(t.ALREADY_SEARCHING) < 90 and len(t.SEARCH_EXPIRED) < 110
    again = sent(await tg.send(A, "/chat"), A)[0]
    assert again == t.ALREADY_SEARCHING and "/stop" in again
    assert "Browse" in t.SEARCH_EXPIRED and "Random Chat" in t.SEARCH_EXPIRED


async def test_a_queued_user_can_really_browse_meanwhile(tg):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    out = await tg.send(A, "/browse")
    assert t.BROWSE_IN_CHAT not in sent(out, A)  # allowed: the text points them there
    assert sent(out, A)  # and answered (a profile or "nothing to show")


async def test_the_queue_timeout_is_configurable(tg, app_pool):
    from bot.config import ConfigError, load_settings
    from bot.services import build_services

    base = {"BOT_TOKEN": "1:x", "DATABASE_URL": "postgresql://u:p@h/d"}
    assert load_settings(base).queue_timeout_minutes == 30
    assert load_settings({**base, "QUEUE_TIMEOUT_MINUTES": "7"}).queue_timeout_minutes == 7
    for bad in ("0", "241", "-3", "abc", "1.5"):
        with pytest.raises(ConfigError, match="QUEUE_TIMEOUT_MINUTES"):
            load_settings({**base, "QUEUE_TIMEOUT_MINUTES": bad})
    assert build_services(app_pool).matchmaking.queue_timeout_minutes == 30
    assert build_services(app_pool, queue_timeout_minutes=7).matchmaking.queue_timeout_minutes == 7

    await tg.onboard(A)
    await tg.send(A, "/chat")
    await app_pool.execute("UPDATE chat_queue SET joined_at = now() - interval '5 minutes'")
    assert await tg.services.matchmaking.expire_stale_queue() == []  # 30 min: still waiting
    tg.services.matchmaking.queue_timeout_minutes = 3
    assert await tg.services.matchmaking.expire_stale_queue() == [A]  # 3 min: expired


async def test_matching_itself_is_unchanged(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    out = await tg.send(B, "/chat")
    assert any("Partner Found" in x for x in sent(out, A) + sent(out, B))
    assert await app_pool.fetchval("SELECT count(*) FROM chat_sessions WHERE ended_at IS NULL") == 1


# ------------------------------------------------------------------ 6. restart recovery


async def test_a_restart_in_the_middle_of_signup_is_recovered_by_a_menu_button(tg, app_pool):
    await tg.send(A, "/start")
    await tg.press(A, "age:yes")
    await tg.send(A, "Asha")
    restart(tg)
    out = await tg.send(A, "25")  # plain text: the old hint, no surprise
    assert sent(out, A) == [t.FINISH_SETUP]
    out = await tg.send(A, "💬 Random Chat")  # a menu button picks the sign-up up again
    assert sent(out, A) == [tr("restart.resume", "en"), t.ASK_NAME]
    for answer in ("Asha", "25", "👩 Female", "👨 Men", "🌍 Other", "Pune", "hello there"):
        await tg.send(A, answer)
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = $1", A) == "active"


async def test_a_restart_before_the_age_gate_is_answered_resumes_at_the_gate(tg):
    await tg.send(A, "/start")
    restart(tg)
    out = await tg.send(A, "🔎 Browse")
    assert sent(out, A) == [tr("restart.resume", "en"), t.AGE_GATE]


async def test_start_after_a_restart_begins_again_cleanly(tg):
    await tg.send(A, "/start")
    await tg.press(A, "age:yes")
    await tg.send(A, "Asha")
    restart(tg)
    out = await tg.send(A, "/start")
    assert sent(out, A) == [t.ASK_NAME]
    key = StorageKey(bot_id=tg.bot.id, chat_id=A, user_id=A)
    assert await tg.dp.fsm.storage.get_state(key) == "Reg:name"


async def test_resume_asks_nothing_of_active_or_unknown_users(tg):
    await tg.onboard(A)
    restart(tg)
    out = await tg.send(A, "just some words")
    assert sent(out, A) == [tr("not_in_chat", "en")]  # the normal answer, no sign-up restart
    assert sent(await tg.send(C, "💬 Random Chat"), C) == [tr("need_start", "en")]


async def test_a_menu_button_ends_any_half_finished_conversation(tg, app_pool):
    await tg.onboard(A)
    await tg.send(A, "/support")
    await tg.press(A, "sp:c:other")  # the bot now waits for the ticket text
    out = await tg.send(A, "💬 Random Chat")  # a menu button, not a ticket
    assert t.SEARCHING in sent(out, A)
    await tg.send(A, "this is plain text")
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 0


async def test_a_menu_button_during_a_live_signup_form_keeps_the_form(tg):
    await tg.send(A, "/start")
    await tg.press(A, "age:yes")
    out = await tg.send(A, "💬 Random Chat")  # the form is live: its own hint, not a restart
    assert tr("restart.resume", "en") not in sent(out, A)
    await tg.send(A, "Asha")
    assert (await tg.send(A, "25"))[0].text == t.ASK_GENDER


def test_every_menu_button_label_is_covered():
    labels = kb.MENU_BUTTON_LABELS
    for lang in ("en", "hi"):
        for menu in (kb.main_menu(lang), kb.chat_menu(lang)):
            for row in menu.keyboard:
                for button in row:
                    assert button.text in labels


# ------------------------------------------------------------------ abusive language


async def test_an_abusive_chat_message_is_refused_with_a_short_notice(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    out = await tg.send(A, "tu chutiya hai")
    assert sent(out, A) == [tr("abuse_blocked", "en")] and sent(out, B) == []
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", A)
    tg.auto_advance = 20.0
    out = await tg.send(A, "madarchod")
    assert sent(out, A) == [tr("abuse_blocked", "hi")] and sent(out, B) == []
    assert sent(await tg.send(A, "sorry, hi"), B) == ["sorry, hi"]  # the chat goes on
    assert await app_pool.fetchval("SELECT count(*) FROM chat_sessions WHERE ended_at IS NULL") == 1


async def test_abusive_profile_text_is_refused_but_ordinary_words_are_fine(tg, app_pool):
    await tg.send(A, "/start")
    await tg.press(A, "age:yes")
    out = await tg.send(A, "Fuck Off")  # the name
    assert sent(out, A) == [tr("invalid.text_abuse", "en")]
    out = await tg.send(A, "Class Assistant")  # whole words only: this is a fine name
    assert tr("invalid.text_abuse", "en") not in sent(out, A)
    assert await app_pool.fetchval("SELECT count(*) FROM profiles") == 0  # not finished yet

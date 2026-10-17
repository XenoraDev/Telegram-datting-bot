"""/myplan (plan, days left, today's use, benefits) and the "plan ends in 3 days" reminder."""

import re
from datetime import UTC, datetime

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from bot.main import run_maintenance, send_expiry_reminders
from bot.texts.i18n import tr
from tests.factories import give_tier, make_plan
from tests.handlers.harness import ADMIN_ID

A, B, C = 7300001, 7300002, 7300003


def texts_to(out, uid):
    return [o.text for o in out if o.kind == "SendMessage" and o.chat_id == uid]


def plain(text):
    return re.sub(r"</?b>", "", text)  # the bold markers are formatting, not content


async def myplan(tg, uid=A):
    return plain(texts_to(await tg.send(uid, "/myplan"), uid)[-1])


async def set_end(pool, sub_id, interval):
    await pool.execute(
        f"UPDATE subscriptions SET current_period_end = now() + interval '{interval}' "  # noqa: S608
        "WHERE id = $1",  # `interval` is a literal from this file, never user input
        sub_id,
    )


# ------------------------------------------------------------------ /myplan


async def test_a_free_user_sees_free_the_limits_and_how_to_upgrade(tg):
    await tg.onboard(A)
    text = await myplan(tg)
    assert "My plan" in text and "Plan: Free" in text and "days left" not in text
    assert "Random chats: 0 / 100" in text and "Profiles browsed: 0 / 100" in text
    assert "Follows: 0 / 50" in text
    assert (
        "Your benefits" in text
        and "partner's city" in text
        and "age" not in text.split("Your benefits")[1].replace("partner's city", "")
    )
    assert text.rstrip().endswith("/premium")


async def test_todays_use_is_counted(tg):
    await tg.onboard(A)
    for _ in range(3):
        await tg.services.entitlements.consume(A, "random_chat")
    assert "Random chats: 3 / 100" in await myplan(tg)


async def test_a_premium_user_sees_the_plan_the_end_date_and_the_days_left(tg, app_pool):
    await tg.onboard(A)
    await give_tier(app_pool, A, "premium", days=30)
    text = await myplan(tg)
    assert "Plan: Premium" in text and "(days left: 30)" in text
    end = await app_pool.fetchval("SELECT current_period_end FROM subscriptions")
    assert end.astimezone(UTC).strftime("%d-%m-%Y") in text
    assert "Random chats: 0 / 200" in text
    assert "age, gender, city" in text and "Plan badge" in text and "Plan: Free" not in text


@pytest.mark.parametrize(
    ("interval", "days"),
    [("2 days 23 hours", 3), ("2 days 1 hour", 3), ("1 day 2 hours", 2), ("30 minutes", 1)],
)
async def test_days_left_round_up_and_never_show_zero(tg, app_pool, interval, days):
    await tg.onboard(A)
    sub = await give_tier(app_pool, A, "premium")
    await set_end(app_pool, sub, interval)
    assert f"(days left: {days})" in await myplan(tg)


async def test_vip_shows_no_limits_and_pro_shows_its_extras(tg, app_pool):
    await tg.onboard(A)
    await give_tier(app_pool, A, "vip")
    text = await myplan(tg)
    assert "Plan: Vip" in text and "Random chats: 0 (no limit)" in text
    assert "Advanced profile filters" in text and "Profile boost" in text


async def test_a_queued_renewal_is_listed_and_the_running_plan_keeps_its_own_end(tg, app_pool):
    await tg.onboard(A)
    running = await give_tier(app_pool, A, "premium", days=10)
    await give_tier(app_pool, A, "pro", days=40)
    await app_pool.execute(
        "UPDATE subscriptions SET started_at = now() + interval '10 days' WHERE id <> $1", running
    )
    text = await myplan(tg)
    assert "Plan: Premium" in text and "(days left: 10)" in text
    start = await app_pool.fetchval("SELECT started_at FROM subscriptions WHERE id <> $1", running)
    assert f"Next: Pro starts on {start.astimezone(UTC).strftime('%d-%m-%Y')}" in text


async def test_an_expired_plan_is_free_again_and_nobody_sees_anothers_plan(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    sub = await give_tier(app_pool, A, "premium")
    await give_tier(app_pool, B, "vip")
    assert "Plan: Premium" in await myplan(tg, A)
    assert "Plan: Vip" not in await myplan(tg, A)  # B's plan is B's
    await set_end(app_pool, sub, "-1 hour")
    text = await myplan(tg, A)
    assert "Plan: Free" in text and "Premium" not in text.split("Your benefits")[0]


async def test_the_plan_name_is_escaped_and_hindi_works(tg, app_pool):
    await tg.onboard(A)
    plan = await make_plan(app_pool, "x1", name="<b>Gold</b> & co", tier="premium")
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "VALUES ($1, $2, now() + interval '5 days')",
        A,
        plan,
    )
    assert "&lt;b&gt;Gold&lt;/b&gt; &amp; co" in await myplan(tg)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", A)
    text = await myplan(tg)
    assert "मेरा प्लान" in text and "बचे दिन: 5" in text and "आज का उपयोग" in text
    assert "आपके लाभ" in text


async def test_myplan_works_in_a_chat_and_is_listed_in_help(tg):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    assert "My plan" in await myplan(tg)
    assert "/myplan" in texts_to(await tg.send(A, "/help"), A)[-1]
    # still in the chat afterwards
    assert B in [o.chat_id for o in await tg.send(A, "still here")]


async def test_a_user_without_an_account_gets_nothing(tg):
    out = await tg.send(C, "/myplan")
    assert texts_to(out, C) == [tr("need_start", "en")]


# ------------------------------------------------------------------ the reminder


async def due(tg):
    return [r["user_id"] for r in await tg.services.payments.expiry_reminders()]


async def test_a_plan_ending_within_three_days_is_reminded_once(tg, app_pool):
    await tg.onboard(A)
    sub = await give_tier(app_pool, A, "premium")
    await set_end(app_pool, sub, "2 days 23 hours")
    assert await due(tg) == [A]
    assert await due(tg) == []  # the same end date is never reminded twice
    assert await app_pool.fetchval("SELECT count(*) FROM expiry_reminders") == 1


async def test_only_the_last_three_days_count(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.onboard(C)
    far = await give_tier(app_pool, A, "premium")
    gone = await give_tier(app_pool, B, "premium")
    edge = await give_tier(app_pool, C, "premium")
    await set_end(app_pool, far, "3 days 1 hour")
    await set_end(app_pool, gone, "-1 minute")
    await set_end(app_pool, edge, "3 days")
    assert await due(tg) == [C]


async def test_a_queued_renewal_means_the_user_is_not_reminded_yet(tg, app_pool):
    await tg.onboard(A)
    first = await give_tier(app_pool, A, "premium", days=2)
    await give_tier(app_pool, A, "premium", days=32)
    await app_pool.execute(
        "UPDATE subscriptions SET started_at = now() + interval '2 days' WHERE id <> $1", first
    )
    assert await due(tg) == []  # the last end is 32 days away


async def test_a_renewal_resets_it_so_the_next_end_is_reminded_again(tg, app_pool):
    await tg.onboard(A)
    sub = await give_tier(app_pool, A, "premium")
    await set_end(app_pool, sub, "2 days")
    assert await due(tg) == [A]
    await set_end(app_pool, sub, "30 days")  # renewed
    assert await due(tg) == []
    await set_end(app_pool, sub, "2 days")  # a month later: a new end date
    assert await due(tg) == [A]
    assert await app_pool.fetchval("SELECT count(*) FROM expiry_reminders") == 2


async def test_banned_users_and_inactive_plans_are_not_reminded(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    s1 = await give_tier(app_pool, A, "premium")
    s2 = await give_tier(app_pool, B, "vip")
    await set_end(app_pool, s1, "1 day")
    await set_end(app_pool, s2, "1 day")
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", A)
    await app_pool.execute("UPDATE plans SET is_active = false WHERE code = 't_vip'")
    assert await due(tg) == []


async def test_the_reminder_is_sent_in_the_users_language_with_plan_date_and_days(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    sa = await give_tier(app_pool, A, "premium")
    sb = await give_tier(app_pool, B, "pro")
    await set_end(app_pool, sa, "2 days 5 hours")
    await set_end(app_pool, sb, "1 day")
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", B)
    assert await send_expiry_reminders(tg.services, tg.bot) == 2
    ea = await app_pool.fetchval("SELECT current_period_end FROM subscriptions WHERE id=$1", sa)
    en = tg.texts(A)[-1]
    assert "Your <b>Premium</b> ends on" in en and "(days left: 3)" in en and "/premium" in en
    assert ea.astimezone(UTC).strftime("%d-%m-%Y") in en
    hi = tg.texts(B)[-1]
    assert "आपका <b>Pro</b>" in hi and "बचे दिन: 1" in hi
    assert await send_expiry_reminders(tg.services, tg.bot) == 0  # nothing the second time
    assert len([x for x in tg.texts(A) if "ends on" in x]) == 1


async def test_maintenance_sends_it_and_a_user_in_a_chat_keeps_the_chat_buttons(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    sub = await give_tier(app_pool, A, "premium")
    await set_end(app_pool, sub, "1 day")
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    await run_maintenance(tg.services, tg.bot)
    reminder = [o for o in tg.out if o.chat_id == A and "ends on" in o.text][-1]
    assert [b.text for r in reminder.markup.keyboard for b in r] == [
        "➡️ Next",
        "🛑 Stop",
        "🚨 Report",
    ]
    await run_maintenance(tg.services, tg.bot)
    assert len([o for o in tg.out if o.chat_id == A and "ends on" in o.text]) == 1


async def test_a_user_who_blocked_the_bot_does_not_stop_the_others(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    for uid in (A, B):
        await set_end(app_pool, await give_tier(app_pool, uid, "premium"), "1 day")
    tg.fail_chat[A] = TelegramForbiddenError(SendMessage(chat_id=A, text="x"), "blocked")
    assert await send_expiry_reminders(tg.services, tg.bot) == 2
    assert any("ends on" in x for x in tg.texts(B))
    assert [m for m in tg.texts(ADMIN_ID) if "Bot error" in m or "keep failing" in m] == []


async def test_deleting_the_account_removes_its_reminder_rows(tg, app_pool):
    await tg.onboard(A)
    await set_end(app_pool, await give_tier(app_pool, A, "premium"), "1 day")
    await send_expiry_reminders(tg.services, tg.bot)
    assert await app_pool.fetchval("SELECT count(*) FROM expiry_reminders") == 1
    await tg.send(A, "/deleteme")
    await tg.press(A, "del:yes")
    assert await app_pool.fetchval("SELECT count(*) FROM expiry_reminders") == 0


def test_the_catalog_has_the_reminder_in_both_languages_with_the_same_slots():
    for lang in ("en", "hi"):
        text = tr("reminder.expiring", lang, plan="P", date="01-01-2030", days=2)
        assert "P" in text and "01-01-2030" in text and "2" in text and "/premium" in text
        assert re.search(r"\{|\}", text) is None
    assert datetime.now(UTC).tzinfo is not None

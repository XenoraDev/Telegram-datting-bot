"""with_retry / deliver: a Telegram rate limit is waited out (twice at most), a user who blocked
the bot is normal, and a failure that persists is reported to the admins (throttled)."""

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage

from bot.services.alerts import MAX_PER_HOUR, WINDOW, AdminAlerter
from bot.telegram_send import MAX_RETRIES, MAX_WAIT, deliver, with_retry

METHOD = SendMessage(chat_id=1, text="x")


def retry_after(seconds=1):
    return TelegramRetryAfter(METHOD, "Too Many Requests", seconds)


class Sleeps:
    def __init__(self):
        self.waited: list[float] = []

    async def __call__(self, seconds):
        self.waited.append(seconds)


class Calls:
    """An async callable that raises the given errors in order, then returns "ok"."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.n = 0

    async def __call__(self):
        self.n += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


async def test_a_rate_limit_is_waited_out_and_retried():
    sleep, call = Sleeps(), Calls(retry_after(3))
    assert await with_retry(call, sleep=sleep) == "ok"
    assert (call.n, sleep.waited) == (2, [3])


async def test_at_most_two_retries_then_the_error_is_raised():
    assert MAX_RETRIES == 2
    sleep, call = Sleeps(), Calls(*[retry_after(1) for _ in range(5)])
    with pytest.raises(TelegramRetryAfter):
        await with_retry(call, sleep=sleep)
    assert call.n == 3 and sleep.waited == [1, 1]  # the first try plus two retries


async def test_two_rate_limits_then_success_still_works():
    sleep, call = Sleeps(), Calls(retry_after(1), retry_after(2))
    assert await with_retry(call, sleep=sleep) == "ok"
    assert (call.n, sleep.waited) == (3, [1, 2])


async def test_a_penalty_longer_than_the_cap_is_not_waited_for():
    sleep, call = Sleeps(), Calls(retry_after(MAX_WAIT + 1))
    with pytest.raises(TelegramRetryAfter):
        await with_retry(call, sleep=sleep)
    assert call.n == 1 and sleep.waited == []


@pytest.mark.parametrize(
    "error",
    [TelegramForbiddenError(METHOD, "bot was blocked"), TelegramBadRequest(METHOD, "bad")],
)
async def test_other_errors_are_not_retried(error):
    sleep, call = Sleeps(), Calls(error)
    with pytest.raises(type(error)):
        await with_retry(call, sleep=sleep)
    assert call.n == 1 and sleep.waited == []


# ------------------------------------------------------------------ deliver


class FakeBot:
    def __init__(self, *errors):
        self.errors = list(errors)
        self.sent: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kw):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((chat_id, text, kw))


async def test_deliver_sends_and_passes_the_options_through():
    bot = FakeBot()
    assert await deliver(bot, 5, "hi", reply_markup="M") is True
    assert bot.sent == [(5, "hi", {"reply_markup": "M"})]


async def test_deliver_retries_a_rate_limit_and_succeeds():
    sleep, bot = Sleeps(), FakeBot(retry_after(2))
    assert await deliver(bot, 5, "hi", sleep=sleep) is True
    assert len(bot.sent) == 1 and sleep.waited == [2]


async def test_a_user_who_blocked_the_bot_is_quiet_not_an_alert():
    alerter = AdminAlerter(frozenset({9}))
    bot = FakeBot(TelegramForbiddenError(METHOD, "blocked"))
    assert await deliver(bot, 5, "hi", alerts=alerter) is False
    assert bot.sent == [] and alerter._sent == type(alerter._sent)()  # nothing was alerted


async def test_a_bad_request_is_false_and_never_raises():
    assert await deliver(FakeBot(TelegramBadRequest(METHOD, "chat not found")), 5, "hi") is False


@pytest.mark.parametrize(
    ("error", "attempts"),
    [
        (retry_after(1), 3),  # the first try and both retries
        (TelegramNetworkError(METHOD, "boom"), 1),  # not a rate limit: no retry
        (TelegramServerError(METHOD, "502"), 1),
    ],
)
async def test_a_failure_that_persists_alerts_the_admins_once(error, attempts):
    sleep = Sleeps()
    alerter = AdminAlerter(frozenset({9, 10}))
    admin_bot = FakeBot(*[error] * attempts)
    ok = await deliver(admin_bot, 5, "hi", alerts=alerter, sleep=sleep)
    assert ok is False
    # the alert goes to both admins (the same fake bot, now healthy again)
    assert sorted(chat for chat, *_ in admin_bot.sent) == [9, 10]
    assert all("keep failing" in text and "1×" in text for _, text, _ in admin_bot.sent)


async def test_persistent_failures_are_throttled_and_counted():
    now = [0.0]
    alerter = AdminAlerter(frozenset({9}), clock=lambda: now[0])
    bot = FakeBot()
    for _ in range(4):
        await alerter.telegram_failure(bot, "TelegramRetryAfter")
        now[0] += 60
    assert len(bot.sent) == 1  # one alert for four failures within the window
    now[0] += WINDOW
    await alerter.telegram_failure(bot, "TelegramRetryAfter")
    assert len(bot.sent) == 2 and "4×" in bot.sent[1][1]  # the next alert counts the ones held back
    await alerter.telegram_failure(bot, "TelegramNetworkError")  # another kind is its own alert
    assert len(bot.sent) == 3


async def test_the_hourly_cap_holds_even_for_many_different_problems():
    alerter = AdminAlerter(frozenset({9}), clock=lambda: 0.0)
    bot = FakeBot()
    for i in range(MAX_PER_HOUR + 5):
        await alerter.alert(bot, f"kind-{i}", "problem {count}")
    assert len(bot.sent) == MAX_PER_HOUR


async def test_no_admins_means_no_alert_and_no_error():
    bot = FakeBot()
    assert await AdminAlerter(frozenset()).alert(bot, "x", "t") is False and bot.sent == []


async def test_an_alert_that_cannot_be_delivered_does_not_raise_or_recurse():
    bot = FakeBot(*[retry_after(1)] * 9)
    assert await AdminAlerter(frozenset({9})).alert(bot, "x", "problem") is True

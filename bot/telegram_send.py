"""Sending to Telegram without losing messages to a rate limit.

Telegram answers a burst with "retry after N seconds" (TelegramRetryAfter). `with_retry` waits
that long and tries again, at most MAX_RETRIES times, and never waits longer than MAX_WAIT for a
single retry (a long penalty is not worth blocking a handler for). `deliver` is the best-effort
send used for notices to users and admins: it never raises, and it reports a failure that does not
go away (rate limit that outlasts the retries, Telegram down) to the admins, throttled.

A user who blocked the bot (TelegramForbiddenError) is normal and is neither retried nor alerted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

if TYPE_CHECKING:
    from .services.alerts import AdminAlerter

log = logging.getLogger(__name__)

MAX_RETRIES = 2
MAX_WAIT = 10.0  # seconds; a longer retry_after is not waited out inside a handler


async def with_retry[T](
    call: Callable[[], Awaitable[T]],
    *,
    retries: int = MAX_RETRIES,
    max_wait: float = MAX_WAIT,
    sleep: Callable[[float], Awaitable[object]] | None = None,
) -> T:
    """Run `call`; on TelegramRetryAfter wait and retry (at most `retries` times). Anything else,
    and a RetryAfter that is too long or keeps coming, is raised to the caller."""
    wait = sleep or asyncio.sleep
    attempt = 0
    while True:
        try:
            return await call()
        except TelegramRetryAfter as exc:
            if attempt >= retries or exc.retry_after > max_wait:
                raise
            attempt += 1
            log.info("telegram asked to slow down; retry %d/%d", attempt, retries)
            await wait(exc.retry_after)


async def deliver(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    alerts: AdminAlerter | None = None,
    sleep: Callable[[float], Awaitable[object]] | None = None,
    **kwargs: object,
) -> bool:
    """Send a message, retrying a rate limit. True when it was sent; False when it was not (the
    user blocked the bot, the chat is gone, or Telegram is failing). Never raises."""
    try:
        await with_retry(lambda: bot.send_message(chat_id, text, **kwargs), sleep=sleep)
        return True
    except TelegramForbiddenError:
        log.info("a user has blocked the bot; message not delivered")
    except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError) as exc:
        log.warning("telegram send failed after retries: %s", type(exc).__name__)
        if alerts is not None:
            await alerts.telegram_failure(bot, type(exc).__name__)
    except TelegramAPIError:
        log.info("could not deliver a bot message to a user")
    return False

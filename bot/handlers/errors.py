"""The last line of defence: an exception that escaped a handler.

The user gets one short, safe sentence (never an exception message or a traceback), the admins get
a throttled alert saying what kind of error and where, and the full traceback goes to the server
log only. Expected Telegram noise (a user who blocked the bot, an edit that changed nothing, an
expired button) is ignored quietly.
"""

from __future__ import annotations

import contextlib
import logging
import traceback

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ErrorEvent

from ..services import Services
from ..texts import en as t
from ..texts.i18n import tr

log = logging.getLogger(__name__)

_HARMLESS_BAD_REQUEST = (
    "message is not modified",
    "query is too old",
    "query id is invalid",
    "message to edit not found",
    "message can't be edited",
)


def where(exc: BaseException) -> str:
    """ "file.py:123 in function" of the innermost frame in our own code: no message, no values."""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "/bot/" in f.filename or "/web/" in f.filename]
    frame = (ours or frames or [None])[-1]
    if frame is None:
        return "unknown"
    return f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} in {frame.name}"


def _harmless(exc: BaseException) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True
    return isinstance(exc, TelegramBadRequest) and any(
        s in str(exc).lower() for s in _HARMLESS_BAD_REQUEST
    )


async def on_error(event: ErrorEvent, bot: Bot, services: Services) -> bool:
    exc = event.exception
    if _harmless(exc):
        log.info("ignored a harmless telegram error: %s", type(exc).__name__)
        return True
    place = where(exc)
    log.error("unhandled error in %s", place, exc_info=exc)  # the traceback stays in the log
    update = event.update
    message = update.message
    callback = update.callback_query
    user = message.from_user if message else callback.from_user if callback else None
    if user is not None and services.limiter.allow("notice", user.id):
        lang = "en"
        with contextlib.suppress(Exception):  # the database may be what failed
            lang = await services.language.get(user.id)
        text = tr("error.generic", lang)
        with contextlib.suppress(TelegramAPIError):
            if callback is not None:
                await callback.answer(text, show_alert=True)
            elif message is not None:
                await message.answer(text)
    with contextlib.suppress(Exception):  # an alert must never raise inside the error handler
        await services.alerts.alert(
            bot,
            f"error:{type(exc).__name__}:{place}",
            t.ADMIN_ERROR,
            error=type(exc).__name__,
            where=place,
        )
    return True

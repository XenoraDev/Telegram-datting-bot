"""Telegram side of the load-test confirmation (layer 3 of the start protection).

The web admin sends the confirmation message (``send_confirmation``); the admin answers with a
button. The button payload is only a one-time token: who may answer, whether it is still valid and
which configuration it starts are all decided by the service from the database, never from the
callback data. Nothing here sends test traffic.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

from .. import keyboards as kb
from ..loadtest.config import TestConfig
from ..services import Services
from ..services.loadtest import CONFIRM_TTL_SECONDS
from ..texts.i18n import tr

log = logging.getLogger(__name__)

_RESULT_KEY = {
    "ok": "loadtest.confirmed",
    "cancelled": "loadtest.cancelled",
    "expired": "loadtest.expired",
    "used": "loadtest.invalid",
    "invalid": "loadtest.invalid",
    "blocked": "loadtest.blocked",
    "busy": "loadtest.busy",
    "denied": "loadtest.denied",
}


def _duration(seconds: int, lang: str) -> str:
    if seconds == 60:
        return tr("loadtest.duration.minute", lang)
    if seconds and seconds % 60 == 0:
        return tr("loadtest.duration.minutes", lang, n=seconds // 60)
    return tr("loadtest.duration.seconds", lang, n=seconds)


def confirmation_text(config: TestConfig, lang: str) -> str:
    return tr(
        "loadtest.confirm",
        lang,
        chats=f"{config.chats:,}",
        rate=config.msgs_per_chat_min,
        ramp=_duration(config.ramp_s, lang),
        duration=_duration(config.duration_s, lang),
        estimated=f"{config.estimated_total_messages:,}",
        valid=CONFIRM_TTL_SECONDS // 60,
    )


async def send_confirmation(
    bot: Bot, services: Services, admin_id: int, cid: int, token: str, config: TestConfig
) -> bool:
    """Send the confirmation message to the admin. False when Telegram cannot deliver it."""
    lang = await services.language.get(admin_id)
    try:
        message = await bot.send_message(
            admin_id, confirmation_text(config, lang), reply_markup=kb.loadtest_confirm(token, lang)
        )
    except TelegramAPIError:
        log.warning("load test confirmation could not be delivered")
        await services.loadtest.delivery_failed(admin_id, cid)
        return False
    await services.loadtest.delivered(admin_id, cid, message.chat.id, message.message_id)
    return True


async def decide(callback: CallbackQuery, services: Services, lang: str) -> None:
    parsed = kb.parse_loadtest_cb(callback.data)
    if parsed is None or callback.from_user is None:
        await callback.answer()
        return
    action, token = parsed
    uid = callback.from_user.id
    if action == "y":
        res = await services.loadtest.confirm(uid, token)
    else:
        res = await services.loadtest.cancel_confirmation(uid, token)
    key = _RESULT_KEY.get(res.kind, "loadtest.invalid")
    text = tr(key, lang, run=res.run_id) if res.kind == "ok" else tr(key, lang)
    if res.kind == "denied":  # someone else pressed it: leave the admin's message intact
        await callback.answer(text, show_alert=True)
        return
    try:
        if callback.message is not None:
            await callback.message.edit_text(text, reply_markup=None)
    except TelegramAPIError:
        pass
    await callback.answer()


def build_router() -> Router:
    r = Router(name="loadtest")
    r.callback_query.register(decide, F.data.startswith("lt:"))
    return r

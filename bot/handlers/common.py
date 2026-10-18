"""Small helpers shared by several handlers."""

from __future__ import annotations

import contextlib

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..services import Services
from ..telegram_send import deliver
from ..texts.i18n import tr


async def drop_buttons(callback: CallbackQuery) -> None:
    """Remove the inline keyboard of the message a button was pressed on (best effort)."""
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)


async def notify_partner_left(bot: Bot, partner_id: int, services: Services) -> None:
    """Tell the other participant the chat ended, in THEIR language."""
    lang = await services.language.get(partner_id)
    await deliver(
        bot,
        partner_id,
        tr("chat_ended_partner", lang),
        alerts=services.alerts,
        reply_markup=kb.main_menu(lang),
    )


async def notify_user(
    bot: Bot,
    uid: int,
    key: str,
    services: Services,
    *,
    live_ticket: int | None = None,
    **fmt: object,
) -> None:
    """Send catalog message `key` to a user in THEIR language (best effort; a Telegram rate limit
    is retried, a failure that persists is reported to the admins). `live_ticket` adds the
    "Reply to support" button bound to that exact ticket."""
    lang = await services.language.get(uid)
    # a user in a chat keeps the chat buttons (Next / Stop / Report); anyone else gets the menu
    in_chat = await services.matchmaking.current_chat(uid) is not None
    menu = kb.chat_menu(lang) if in_chat else kb.main_menu(lang)
    if live_ticket is not None:
        menu = kb.support_live_button(live_ticket, lang)
    await deliver(bot, uid, tr(key, lang, **fmt), alerts=services.alerts, reply_markup=menu)

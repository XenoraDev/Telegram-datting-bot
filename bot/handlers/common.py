"""Small helpers shared by several handlers."""

from __future__ import annotations

import contextlib

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..services import Services
from ..texts.i18n import tr


async def drop_buttons(callback: CallbackQuery) -> None:
    """Remove the inline keyboard of the message a button was pressed on (best effort)."""
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)


async def notify_partner_left(bot: Bot, partner_id: int, services: Services) -> None:
    """Tell the other participant the chat ended, in THEIR language."""
    lang = await services.language.get(partner_id)
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(
            partner_id, tr("chat_ended_partner", lang), reply_markup=kb.main_menu(lang)
        )


async def notify_user(bot: Bot, uid: int, key: str, services: Services, **fmt: object) -> None:
    """Send catalog message `key` to a user in THEIR language (best effort)."""
    lang = await services.language.get(uid)
    # a user in a chat keeps the chat buttons (Next / Stop / Report); anyone else gets the menu
    in_chat = await services.matchmaking.current_chat(uid) is not None
    menu = kb.chat_menu(lang) if in_chat else kb.main_menu(lang)
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(uid, tr(key, lang, **fmt), reply_markup=menu)

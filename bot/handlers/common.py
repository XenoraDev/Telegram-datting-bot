"""Small helpers shared by several handlers."""

from __future__ import annotations

import contextlib

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..texts import en as t


async def drop_buttons(callback: CallbackQuery) -> None:
    """Remove the inline keyboard of the message a button was pressed on (best effort)."""
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)


async def notify_partner_left(bot: Bot, partner_id: int) -> None:
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(partner_id, t.CHAT_ENDED_PARTNER, reply_markup=kb.main_menu)

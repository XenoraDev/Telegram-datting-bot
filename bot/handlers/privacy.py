"""/privacy: the owner's switches for what partners may see (age, gender, city)."""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..services import Services
from ..texts.i18n import tr
from .filters import ActiveUser


async def privacy_cmd(message: Message, services: Services, lang: str) -> None:
    settings = await services.privacy.get(message.from_user.id)
    if settings is None:
        return await message.answer(tr("privacy.no_profile", lang))
    await message.answer(tr("privacy.header", lang), reply_markup=kb.privacy_kb(settings, lang))


async def privacy_cb(callback: CallbackQuery, services: Services, lang: str) -> None:
    field = kb.parse_privacy_cb(callback.data)
    settings = await services.privacy.toggle(callback.from_user.id, field) if field else None
    if settings is None or not isinstance(callback.message, Message):
        return await callback.answer(tr("privacy.no_profile", lang))
    await callback.answer(tr("privacy.in_chat", lang))
    with contextlib.suppress(TelegramAPIError):  # e.g. "message is not modified"
        await callback.message.edit_reply_markup(reply_markup=kb.privacy_kb(settings, lang))


def build_router() -> Router:
    r = Router(name="privacy")
    active = ActiveUser()
    r.message.register(privacy_cmd, Command("privacy"), active)
    r.callback_query.register(privacy_cb, F.data.startswith("pv:"), active)
    return r

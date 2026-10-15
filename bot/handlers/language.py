"""🌐 Language selector: /language or the menu button. Persists the choice per user."""

from __future__ import annotations

import contextlib

import asyncpg
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import domain
from .. import keyboards as kb
from ..services import Services
from ..texts.i18n import tr


def _usable(user: asyncpg.Record | None) -> bool:
    return bool(user and user["status"] in (domain.ONBOARDING, domain.ACTIVE))


async def language_cmd(message: Message, user: asyncpg.Record | None = None) -> None:
    if not _usable(user):
        return await message.answer(tr("lang.unavailable", user["language"] if user else None))
    await message.answer(tr("lang.prompt", user["language"]), reply_markup=kb.language_choice())


async def language_cb(
    callback: CallbackQuery, services: Services, user: asyncpg.Record | None = None
) -> None:
    code = kb.parse_language_cb(callback.data)
    if not _usable(user) or code is None:
        return await callback.answer(tr("lang.invalid", user["language"] if user else None))
    if not await services.language.set(callback.from_user.id, code):
        return await callback.answer(tr("lang.invalid", code))
    await callback.answer()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_text(tr("lang.changed", code))


def build_router() -> Router:
    r = Router(name="language")
    r.message.register(language_cmd, Command("language"))
    r.message.register(language_cmd, F.text == kb.BTN_LANGUAGE)
    r.callback_query.register(language_cb, F.data.startswith("lg:"))
    return r

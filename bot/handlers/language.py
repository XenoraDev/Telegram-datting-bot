"""🌐 Language selector: /language or the menu button. Persists the choice per user."""

from __future__ import annotations

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import domain
from .. import keyboards as kb
from ..services import Services
from ..texts.i18n import tr
from .common import drop_buttons


def _usable(user: asyncpg.Record | None) -> bool:
    return bool(user and user["status"] in (domain.ONBOARDING, domain.ACTIVE))


async def language_cmd(message: Message, lang: str, user: asyncpg.Record | None = None) -> None:
    if not _usable(user):
        return await message.answer(tr("lang.unavailable", lang))
    await message.answer(tr("lang.prompt", lang), reply_markup=kb.language_choice())


async def language_cb(
    callback: CallbackQuery, services: Services, lang: str, user: asyncpg.Record | None = None
) -> None:
    code = kb.parse_language_cb(callback.data)
    if not _usable(user) or code is None:
        return await callback.answer(tr("lang.invalid", lang))
    if not await services.language.set(callback.from_user.id, code):
        return await callback.answer(tr("lang.invalid", lang))
    await callback.answer()
    if isinstance(callback.message, Message):
        await drop_buttons(callback)
        # Reply keyboards can only be replaced by sending a message, so confirm with the menu in
        # the new language (onboarding users have no menu yet).
        menu = kb.main_menu(code) if user["status"] == domain.ACTIVE else None
        await callback.message.answer(tr("lang.changed", code), reply_markup=menu)
        if user["status"] == domain.ONBOARDING and user["age_confirmed_at"] is None:
            # first /start: the language came first, the 18+ gate follows it in that language
            await callback.message.answer(tr("age_gate", code), reply_markup=kb.age_gate(code))


def build_router() -> Router:
    r = Router(name="language")
    r.message.register(language_cmd, Command("language"))
    r.message.register(language_cmd, F.text.in_(kb.labels("btn.language")))
    r.callback_query.register(language_cb, F.data.startswith("lg:"))
    return r

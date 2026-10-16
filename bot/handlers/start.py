"""/start, the 18+ gate, /ping and /help."""

from __future__ import annotations

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..services import Services
from ..texts.i18n import tr
from .common import drop_buttons, notify_partner_left
from .profile import begin_profile


async def start(message: Message, services: Services, state: FSMContext, lang: str) -> None:
    await state.clear()
    result = await services.onboarding.start(message.from_user.id, message.from_user.username)
    if result.state == "blocked":
        await message.answer(tr("blocked_adults_only", lang), reply_markup=kb.remove)
    elif result.state == "need_age_confirmation":
        await message.answer(tr("age_gate", lang), reply_markup=kb.age_gate(lang))
    elif result.state == "need_profile":
        await begin_profile(message, state, lang)
    else:
        await message.answer(tr("welcome_back", lang), reply_markup=kb.main_menu(lang))


async def age_yes(
    callback: CallbackQuery, services: Services, state: FSMContext, lang: str
) -> None:
    ok = await services.onboarding.confirm_adult(callback.from_user.id)
    await drop_buttons(callback)
    if not ok:
        await callback.answer()
        return
    await callback.answer(tr("thanks", lang))
    if isinstance(callback.message, Message):
        await begin_profile(callback.message, state, lang)


async def age_no(callback: CallbackQuery, services: Services, state: FSMContext, lang: str) -> None:
    partner = await services.onboarding.declare_minor(callback.from_user.id)
    await state.clear()
    await drop_buttons(callback)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(tr("blocked_adults_only", lang), reply_markup=kb.remove)
    if partner is not None:
        await notify_partner_left(callback.bot, partner, services)


async def ping(message: Message, pool: asyncpg.Pool) -> None:
    try:
        await pool.fetchval("SELECT 1")
    except Exception:  # health probe: report any failure instead of crashing the handler
        await message.answer("🏓 pong — database: ERROR")
        return
    await message.answer("🏓 pong — database: ok")


async def help_(message: Message, lang: str) -> None:
    await message.answer(tr("help", lang))


def build_router() -> Router:
    router = Router(name="start")
    router.message.register(start, CommandStart())
    router.callback_query.register(age_yes, F.data == "age:yes")
    router.callback_query.register(age_no, F.data == "age:no")
    router.message.register(ping, Command("ping"))
    router.message.register(help_, Command("help"))
    return router

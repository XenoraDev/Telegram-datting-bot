"""/start, the 18+ gate, /ping and /help."""

from __future__ import annotations

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..services import Services
from ..texts import en as t
from .common import drop_buttons, notify_partner_left
from .profile import begin_profile


async def start(message: Message, services: Services, state: FSMContext) -> None:
    await state.clear()
    result = await services.onboarding.start(message.from_user.id, message.from_user.username)
    if result.state == "blocked":
        await message.answer(t.BLOCKED_ADULTS_ONLY, reply_markup=kb.remove)
    elif result.state == "need_age_confirmation":
        await message.answer(t.AGE_GATE, reply_markup=kb.age_gate())
    elif result.state == "need_profile":
        await begin_profile(message, state)
    else:
        await message.answer(t.WELCOME_BACK, reply_markup=kb.main_menu)


async def age_yes(callback: CallbackQuery, services: Services, state: FSMContext) -> None:
    ok = await services.onboarding.confirm_adult(callback.from_user.id)
    await drop_buttons(callback)
    if not ok:
        await callback.answer()
        return
    await callback.answer("Thanks!")
    if isinstance(callback.message, Message):
        await begin_profile(callback.message, state)


async def age_no(callback: CallbackQuery, services: Services, state: FSMContext) -> None:
    partner = await services.onboarding.declare_minor(callback.from_user.id)
    await state.clear()
    await drop_buttons(callback)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(t.BLOCKED_ADULTS_ONLY, reply_markup=kb.remove)
    if partner is not None:
        await notify_partner_left(callback.bot, partner)


async def ping(message: Message, pool: asyncpg.Pool) -> None:
    try:
        await pool.fetchval("SELECT 1")
    except Exception:  # health probe: report any failure instead of crashing the handler
        await message.answer("🏓 pong — database: ERROR")
        return
    await message.answer("🏓 pong — database: ok")


async def help_(message: Message) -> None:
    await message.answer(t.HELP)


def build_router() -> Router:
    router = Router(name="start")
    router.message.register(start, CommandStart())
    router.callback_query.register(age_yes, F.data == "age:yes")
    router.callback_query.register(age_no, F.data == "age:no")
    router.message.register(ping, Command("ping"))
    router.message.register(help_, Command("help"))
    return router

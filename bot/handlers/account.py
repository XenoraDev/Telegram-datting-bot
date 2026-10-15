"""/deleteme: confirmation flow. The account acted on is ALWAYS the Telegram sender's."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..services import Services
from ..texts import en as t
from .common import drop_buttons, notify_partner_left


async def deleteme(message: Message, services: Services) -> None:
    if not services.limiter.allow("delete_request", message.from_user.id):
        return await message.answer(t.DELETE_RATE_LIMITED)
    await message.answer(t.DELETE_CONFIRM, reply_markup=kb.delete_confirm())


async def confirm(callback: CallbackQuery, services: Services, state: FSMContext, bot: Bot) -> None:
    result = await services.account.delete_account(callback.from_user.id)
    await state.clear()
    await drop_buttons(callback)
    if result.kind == "deleted":
        text = t.DELETE_DONE_RESTRICTED if result.kept_block else t.DELETE_DONE
    elif result.kind == "already_deleted":
        text = t.DELETE_ALREADY
    else:
        text = t.NEED_START
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(text, reply_markup=kb.remove)
    if result.partner_id is not None:
        await notify_partner_left(bot, result.partner_id)


async def cancel(callback: CallbackQuery) -> None:
    await drop_buttons(callback)
    if isinstance(callback.message, Message):
        await callback.message.answer(t.DELETE_CANCELLED)
    await callback.answer()


def build_router() -> Router:
    r = Router(name="account")
    r.message.register(deleteme, Command("deleteme"))
    r.callback_query.register(confirm, F.data == "del:yes")
    r.callback_query.register(cancel, F.data == "del:no")
    return r

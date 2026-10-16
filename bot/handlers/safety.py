"""Report / block. Callback payloads carry session ids only; the target is derived server-side."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..services import Services
from ..texts.i18n import tr
from .common import drop_buttons as _remove_markup
from .common import notify_partner_left
from .filters import ActiveUser


async def report_cmd(message: Message, services: Services, lang: str) -> None:
    chat = await services.matchmaking.current_chat(message.from_user.id)
    if chat is None:
        return await message.answer(tr("not_in_chat", lang))
    await message.answer(tr("report_start", lang), reply_markup=kb.report_reasons(chat[0], lang))


async def block_cmd(message: Message, services: Services, bot: Bot, lang: str) -> None:
    chat = await services.matchmaking.current_chat(message.from_user.id)
    if chat is None:
        return await message.answer(tr("not_in_chat", lang))
    await _do_block(message, services, bot, message.from_user.id, chat[0], lang)


async def _do_block(
    target: Message, services: Services, bot: Bot, uid: int, sid: int, lang: str
) -> None:
    res = await services.moderation.block(uid, sid)
    if res.kind != "blocked":
        return await target.answer(tr("report_not_found", lang))
    await target.answer(tr("block_done", lang), reply_markup=kb.main_menu(lang))
    if res.partner_id is not None:
        await notify_partner_left(bot, res.partner_id, services)


async def report_pick_reason(callback: CallbackQuery, lang: str) -> None:
    parsed = kb.parse_session_cb(callback.data)
    if parsed is None or parsed[0] != "rep" or not isinstance(callback.message, Message):
        return await callback.answer()
    await callback.message.answer(
        tr("report_start", lang), reply_markup=kb.report_reasons(parsed[1], lang)
    )
    await _remove_markup(callback)
    await callback.answer()


async def report_submit(callback: CallbackQuery, services: Services, bot: Bot, lang: str) -> None:
    parsed = kb.parse_reason_cb(callback.data)
    if parsed is None:
        return await callback.answer()
    session_id, reason = parsed
    res = await services.moderation.report(callback.from_user.id, session_id, reason)
    await _remove_markup(callback)
    texts = {
        "created": tr("report_created", lang),
        "duplicate": tr("report_duplicate", lang),
        "rate_limited": tr("report_rate_limited", lang),
    }
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            texts.get(res.kind, tr("report_not_found", lang)),
            reply_markup=kb.main_menu(lang) if res.kind == "created" else None,
        )
    if res.partner_id is not None:
        await notify_partner_left(bot, res.partner_id, services)


async def block_cb(callback: CallbackQuery, services: Services, bot: Bot, lang: str) -> None:
    parsed = kb.parse_session_cb(callback.data)
    if parsed is None or parsed[0] != "blk" or not isinstance(callback.message, Message):
        return await callback.answer()
    await _remove_markup(callback)
    await callback.answer()
    await _do_block(callback.message, services, bot, callback.from_user.id, parsed[1], lang)


async def cancel_cb(callback: CallbackQuery) -> None:
    await _remove_markup(callback)
    await callback.answer()


def build_router() -> Router:
    r = Router(name="safety")
    active = ActiveUser()
    r.message.register(report_cmd, Command("report"), active)
    r.message.register(report_cmd, F.text.in_(kb.labels("btn.report")), active)
    r.message.register(block_cmd, Command("block"), active)
    r.callback_query.register(report_pick_reason, F.data.startswith("rep:"), active)
    r.callback_query.register(report_submit, F.data.startswith("rr:"), active)
    r.callback_query.register(block_cb, F.data.startswith("blk:"), active)
    r.callback_query.register(cancel_cb, F.data == "cancel")
    return r

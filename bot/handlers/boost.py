"""/boost: Profile Boost (Pro / VIP). Thin: BoostService re-checks the entitlement, the cooldown
and the profile on every press (the button proves nothing)."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..services import Services
from ..services.boost import BOOST_MINUTES, COOLDOWN_HOURS, BoostResult, BoostStatus
from ..texts.i18n import tr
from .filters import ActiveUser


def fmt_time(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%d-%m-%Y %H:%M UTC") if moment else ""


def _text(kind: str, ends: datetime | None, nxt: datetime | None, lang: str) -> str | None:
    if kind == "denied":
        return tr("boost.locked", lang)
    if kind == "not_eligible":
        return tr("privacy.no_profile", lang)
    if kind in ("active", "activated", "already_active"):
        return tr("boost.active", lang, ends=fmt_time(ends))
    if kind == "cooldown":
        return tr("boost.cooldown", lang, next=fmt_time(nxt))
    if kind == "idle":
        return tr("boost.intro", lang, minutes=BOOST_MINUTES, hours=COOLDOWN_HOURS)
    return None


async def boost_cmd(message: Message, services: Services, lang: str) -> None:
    uid = message.from_user.id
    if await services.matchmaking.current_chat(uid):
        return await message.answer(tr("browse_in_chat", lang))
    status: BoostStatus = await services.boost.status(uid)
    text = _text(status.kind, status.ends_at, status.next_at, lang)
    markup = kb.boost_kb(lang) if status.kind == "idle" else None
    await message.answer(text or tr("boost.locked", lang), reply_markup=markup)


async def boost_cb(callback: CallbackQuery, services: Services, lang: str) -> None:
    if kb.parse_boost_cb(callback.data) is None or not isinstance(callback.message, Message):
        return await callback.answer()
    uid = callback.from_user.id
    if await services.matchmaking.current_chat(uid):
        return await callback.answer(tr("browse_in_chat", lang), show_alert=True)
    res: BoostResult = await services.boost.activate(uid)
    if res.kind == "rate_limited":
        return await callback.answer(tr("browse_rate_limited", lang))
    text = _text(res.kind, res.ends_at, res.next_at, lang)
    if res.kind == "denied":
        return await callback.answer(tr("boost.locked", lang), show_alert=True)
    await callback.answer()
    await callback.message.answer(text or tr("boost.locked", lang))


def build_router() -> Router:
    r = Router(name="boost")
    active = ActiveUser()
    r.message.register(boost_cmd, Command("boost"), active)
    r.callback_query.register(boost_cb, F.data == "bo:go", active)
    return r

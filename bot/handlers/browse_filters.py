"""/filters and the 🎯 button under a browse card: Pro / VIP advanced filters.

Thin: every press asks FilterService, which re-checks the entitlement and validates the value
(callback data is never trusted for authorization). Nothing here shows a partner's data.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..services import Services
from ..services.filters import FilterResult, FilterState
from ..texts import en as t
from ..texts.i18n import tr
from .filters import ActiveUser


class FilterFlow(StatesGroup):
    city = State()


# A tap on a menu button while the bot waits for a city is not a city.
_MENU_BUTTONS = frozenset().union(
    *(
        kb.labels(key)
        for key in (
            "btn.chat",
            "btn.browse",
            "btn.matches",
            "btn.profile",
            "btn.premium",
            "btn.language",
            "btn.support",
            "btn.next",
            "btn.stop",
            "btn.report",
        )
    )
)


def _any(lang: str) -> str:
    return tr("filters.any", lang)


def menu_text(state: FilterState, lang: str) -> str:
    f = state.filters
    if f.age_min is None and f.age_max is None:
        age = _any(lang)
    else:
        age = kb.age_label(f.age_min or 18, f.age_max or 99)
    rows = [
        tr("filters.header", lang),
        tr("filters.row.age", lang, value=_lock(state, "age", age, lang)),
        tr(
            "filters.row.gender",
            lang,
            value=_lock(
                state, "gender", kb.gender_label(f.gender, lang) if f.gender else None, lang
            ),
        ),
        tr(
            "filters.row.city",
            lang,
            value=_lock(state, "city", t.esc(f.city) if f.city else None, lang),
        ),
        tr(
            "filters.row.looking",
            lang,
            value=kb.looking_label(f.looking_for, lang) if f.looking_for else _any(lang),
        ),
        "",
        tr("filters.note", lang),
    ]
    return "\n".join(rows)


def _lock(state: FilterState, field: str, value: str | None, lang: str) -> str:
    if field in state.locked:
        return tr("filters.field_locked", lang)
    return value or _any(lang)


async def _show(target: Message, state: FilterState, lang: str, *, edit: bool) -> None:
    text, markup = menu_text(state, lang), kb.filters_home(lang)
    if edit:
        with contextlib.suppress(TelegramAPIError):  # e.g. "message is not modified"
            await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def filters_cmd(message: Message, services: Services, state: FSMContext, lang: str) -> None:
    await state.clear()
    if await services.matchmaking.current_chat(message.from_user.id):
        return await message.answer(tr("browse_in_chat", lang))
    current = await services.filters.state(message.from_user.id)
    if not current.allowed:
        return await message.answer(tr("filters.locked", lang))
    await _show(message, current, lang, edit=False)


def _failure(res: FilterResult, lang: str) -> str:
    if res.kind == "denied":
        return tr("filters.locked", lang)
    if res.kind == "locked_field":
        return tr("filters.field_locked_msg", lang)
    if res.kind == "invalid" and res.code:
        try:
            return tr(f"invalid.{res.code}", lang)
        except KeyError:
            pass
    return tr("privacy.no_profile", lang)


async def filters_cb(
    callback: CallbackQuery, services: Services, state: FSMContext, lang: str
) -> None:
    parsed = kb.parse_filter_cb(callback.data)
    msg = callback.message if isinstance(callback.message, Message) else None
    if parsed is None or msg is None:
        return await callback.answer()
    action, value = parsed
    uid = callback.from_user.id
    if await services.matchmaking.current_chat(uid):
        return await callback.answer(tr("browse_in_chat", lang), show_alert=True)
    current = await services.filters.state(uid)
    if not current.allowed:  # not entitled (or no longer): same answer for every button
        await state.clear()
        await callback.answer(tr("filters.locked", lang), show_alert=True)
        return
    res: FilterResult | None = None
    if action == "z":
        await state.clear()
        await callback.answer()
        with contextlib.suppress(TelegramAPIError):
            await msg.edit_reply_markup(reply_markup=None)
        return
    if action == "h":
        await state.clear()
    elif action == "a" and value is None:
        await callback.answer()
        return await _edit(msg, tr("filters.pick_age", lang), kb.filters_age(lang))
    elif action == "g" and value is None:
        await callback.answer()
        return await _edit(msg, tr("filters.pick_gender", lang), kb.filters_gender(lang))
    elif action == "k" and value is None:
        await callback.answer()
        return await _edit(msg, tr("filters.pick_looking", lang), kb.filters_looking(lang))
    elif action == "c":
        await state.set_state(FilterFlow.city)
        await callback.answer()
        return await _edit(msg, tr("filters.ask_city", lang), kb.filters_city(lang))
    elif action == "a":
        res = await _set_age(services, uid, value)
    elif action == "g":
        res = await services.filters.set_gender(uid, None if value == "x" else value)
    elif action == "k":
        res = await services.filters.set_looking_for(uid, None if value == "x" else value)
    elif action == "cx":
        await state.clear()
        res = await services.filters.set_city(uid, None)
    elif action == "r":
        await state.clear()
        res = await services.filters.reset(uid)
    if res is not None and res.kind != "ok":
        await callback.answer(_failure(res, lang), show_alert=True)
        return
    await callback.answer(tr("filters.reset_done", lang) if action == "r" else None)
    fresh = res.state if res is not None and res.state else await services.filters.state(uid)
    await _show(msg, fresh, lang, edit=True)


async def _set_age(services: Services, uid: int, value: str) -> FilterResult | None:
    if value == "x":
        return await services.filters.set_age(uid, None, None)
    if value.isdigit() and int(value) < len(kb.AGE_PRESETS):
        return await services.filters.set_age(uid, *kb.AGE_PRESETS[int(value)])
    return None  # a tampered payload changes nothing


async def _edit(msg: Message, text: str, markup) -> None:
    with contextlib.suppress(TelegramAPIError):
        await msg.edit_text(text, reply_markup=markup)


async def got_city(message: Message, services: Services, state: FSMContext, lang: str) -> None:
    uid = message.from_user.id
    if message.text in _MENU_BUTTONS or await services.matchmaking.current_chat(uid):
        await state.clear()  # not an answer: drop the question and let the real handler run
        raise SkipHandler
    res = await services.filters.set_city(uid, message.text)
    if res.kind == "invalid" or res.kind != "ok":
        if res.kind in ("denied", "locked_field", "no_profile"):
            await state.clear()
        return await message.answer(_failure(res, lang))
    await state.clear()
    await _show(message, res.state, lang, edit=False)


def build_router() -> Router:
    r = Router(name="browse_filters")
    active = ActiveUser()
    r.message.register(filters_cmd, Command("filters"), active)
    r.message.register(
        got_city, StateFilter(FilterFlow.city), F.text & ~F.text.startswith("/"), active
    )
    r.callback_query.register(filters_cb, F.data.startswith("fl:"), active)
    return r

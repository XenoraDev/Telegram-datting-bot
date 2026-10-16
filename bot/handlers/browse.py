"""Browse profiles, like / pass, matches, and report / block of a browsed profile.

Callback payloads carry an opaque profile handle (never a Telegram user id); the target is
resolved and re-checked server-side on every press.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import domain
from .. import keyboards as kb
from ..db.repo_browse import Card
from ..services import Services
from ..services.browse import ACTIONS
from ..texts import en as t
from ..texts.i18n import tr
from .common import drop_buttons, notify_partner_left
from .filters import ActiveUser

log = logging.getLogger(__name__)


def card_text(c: Card, lang: str) -> str:
    return tr("card", lang).format(
        name=t.esc(c.name),
        age=c.age,
        flag=domain.flag(c.country),
        city=t.esc(c.city),
        bio=t.esc(c.bio),
    )


def _short(c: Card, lang: str) -> str:
    return tr("match_card", lang).format(
        name=t.esc(c.name), age=c.age, flag=domain.flag(c.country), city=t.esc(c.city)
    )


async def _send_next(target: Message, services: Services, uid: int, lang: str) -> None:
    card = await services.browse.next_card(uid)
    if card is None:
        await target.answer(tr("browse_empty", lang), reply_markup=kb.main_menu(lang))
    else:
        await target.answer(
            card_text(card, lang), reply_markup=kb.browse_card(card.public_id, lang)
        )


async def browse_cmd(message: Message, services: Services, lang: str) -> None:
    uid = message.from_user.id
    if await services.matchmaking.current_chat(uid):
        return await message.answer(tr("browse_in_chat", lang))
    await _send_next(message, services, uid, lang)


async def browse_cb(callback: CallbackQuery, services: Services, lang: str) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await _send_next(callback.message, services, callback.from_user.id, lang)


async def _send_matches(target: Message, services: Services, uid: int, lang: str) -> None:
    cards, total = await services.browse.matches(uid)
    if not cards:
        return await target.answer(tr("matches_none", lang), reply_markup=kb.main_menu(lang))
    await target.answer(
        tr("matches_header", lang).format(total=total), reply_markup=kb.main_menu(lang)
    )
    for c in cards:
        await target.answer(card_text(c, lang), reply_markup=kb.match_card(c.public_id, lang))
    if total > len(cards):
        await target.answer(tr("matches_more", lang).format(n=total - len(cards)))


async def matches_cmd(message: Message, services: Services, lang: str) -> None:
    await _send_matches(message, services, message.from_user.id, lang)


async def matches_cb(callback: CallbackQuery, services: Services, lang: str) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await _send_matches(callback.message, services, callback.from_user.id, lang)


async def swipe_cb(callback: CallbackQuery, services: Services, bot: Bot, lang: str) -> None:
    parsed = kb.parse_profile_cb(callback.data)
    if parsed is None or parsed[0] not in ACTIONS or not isinstance(callback.message, Message):
        return await callback.answer()
    uid = callback.from_user.id
    await drop_buttons(callback)
    await callback.answer()
    res = await services.browse.swipe(uid, parsed[1], ACTIONS[parsed[0]])
    if res.kind == "match" and res.partner_card and res.my_card and res.partner_id:
        await callback.message.answer(
            tr("match_new", lang).format(card=_short(res.partner_card, lang)),
            reply_markup=kb.match_actions(lang),
        )
        partner_lang = await services.language.get(res.partner_id)  # they read it in their language
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(
                res.partner_id,
                tr("match_new", partner_lang).format(card=_short(res.my_card, partner_lang)),
                reply_markup=kb.match_actions(partner_lang),
            )
        return
    if res.kind in ("self", "not_eligible"):
        return
    if res.kind == "unavailable":
        await callback.message.answer(tr("profile_gone", lang))
    await _send_next(callback.message, services, uid, lang)


async def report_pick_reason(callback: CallbackQuery, lang: str) -> None:
    parsed = kb.parse_profile_cb(callback.data)
    if parsed is None or parsed[0] not in ("rp", "mr") or not isinstance(callback.message, Message):
        return await callback.answer()
    prefix = "rq" if parsed[0] == "rp" else "rm"
    await callback.message.answer(
        tr("report_start", lang), reply_markup=kb.profile_report_reasons(parsed[1], prefix, lang)
    )
    await callback.answer()


async def report_submit(callback: CallbackQuery, services: Services, bot: Bot, lang: str) -> None:
    parsed = kb.parse_profile_reason_cb(callback.data)
    if parsed is None or not isinstance(callback.message, Message):
        return await callback.answer()
    prefix, pid, reason = parsed
    uid = callback.from_user.id
    res = await services.moderation.report_profile(uid, pid, reason)
    await drop_buttons(callback)
    await callback.answer()
    texts = {
        "created": tr("report_created", lang),
        "duplicate": tr("report_duplicate", lang),
        "rate_limited": tr("report_rate_limited", lang),
    }
    await callback.message.answer(
        texts.get(res.kind, tr("profile_gone", lang)), reply_markup=kb.main_menu(lang)
    )
    if res.partner_id is not None:
        await notify_partner_left(bot, res.partner_id, services)
    if res.kind == "created" and prefix == "rq":
        await _send_next(callback.message, services, uid, lang)


async def block_cb(callback: CallbackQuery, services: Services, bot: Bot, lang: str) -> None:
    parsed = kb.parse_profile_cb(callback.data)
    if parsed is None or parsed[0] not in ("bk", "mk") or not isinstance(callback.message, Message):
        return await callback.answer()
    uid = callback.from_user.id
    res = await services.moderation.block_profile(uid, parsed[1])
    await drop_buttons(callback)
    await callback.answer()
    if res.kind != "blocked":
        return await callback.message.answer(tr("profile_gone", lang))
    await callback.message.answer(tr("profile_blocked", lang), reply_markup=kb.main_menu(lang))
    if res.partner_id is not None:
        await notify_partner_left(bot, res.partner_id, services)
    if parsed[0] == "bk":
        await _send_next(callback.message, services, uid, lang)


def build_router() -> Router:
    r = Router(name="browse")
    active = ActiveUser()
    r.message.register(browse_cmd, Command("browse"), active)
    r.message.register(browse_cmd, F.text.in_(kb.labels("btn.browse")), active)
    r.message.register(matches_cmd, Command("matches"), active)
    r.message.register(matches_cmd, F.text.in_(kb.labels("btn.matches")), active)
    r.callback_query.register(browse_cb, F.data == "br", active)
    r.callback_query.register(matches_cb, F.data == "mm", active)
    r.callback_query.register(swipe_cb, F.data.regexp(r"^(lk|ps):"), active)
    r.callback_query.register(report_pick_reason, F.data.regexp(r"^(rp|mr):"), active)
    r.callback_query.register(report_submit, F.data.regexp(r"^(rq|rm):"), active)
    r.callback_query.register(block_cb, F.data.regexp(r"^(bk|mk):"), active)
    return r

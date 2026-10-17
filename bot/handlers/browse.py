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

from .. import cards, domain
from .. import keyboards as kb
from ..db.repo_browse import Card
from ..services import Services
from ..services.browse import ACTIONS
from ..texts import en as t
from ..texts.i18n import tr
from .chat import announce_chat_started
from .common import drop_buttons, notify_partner_left
from .filters import ActiveUser
from .follow_notice import notify_new_follower

log = logging.getLogger(__name__)


def card_text(c: Card, lang: str) -> str:
    text = tr("card", lang).format(
        name=t.esc(c.name),
        age=c.age if c.age is not None else tr("pc.hidden", lang),
        flag=domain.flag(c.country),
        city=t.esc(c.city) if c.city is not None else tr("pc.hidden", lang),
        bio=t.esc(c.bio),
    )
    if c.followers > 0:
        text += "\n" + tr("followers.line", lang, n=c.followers)
    return text


async def _send_next(target: Message, services: Services, uid: int, lang: str) -> None:
    res = await services.browse.next_card(uid)
    if res.kind == "card":
        await target.answer(
            card_text(res.card, lang), reply_markup=kb.browse_card(res.card.public_id, lang)
        )
    elif res.kind == "limit_reached":
        await target.answer(
            tr("limit.browse", lang, limit=res.limit), reply_markup=kb.main_menu(lang)
        )
    elif res.kind == "rate_limited":
        await target.answer(tr("browse_rate_limited", lang))
    else:
        key = "browse_empty_filtered" if res.filtered else "browse_empty"
        await target.answer(tr(key, lang), reply_markup=kb.main_menu(lang))


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
    found, total, hint = await services.cards.matches(uid)
    if not found:
        return await target.answer(tr("matches_none", lang), reply_markup=kb.main_menu(lang))
    await target.answer(
        tr("matches_header", lang).format(total=total), reply_markup=kb.main_menu(lang)
    )
    for c in found:
        await target.answer(
            cards.render(c, lang, anonymous=False), reply_markup=kb.match_card(c.public_id, lang)
        )
    if hint:
        await target.answer(tr("pc.cta", lang))
    if total > len(found):
        await target.answer(tr("matches_more", lang).format(n=total - len(found)))


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
    if res.kind == "match" and res.partner_id:
        # each side sees the other's card through THEIR entitlements and the OTHER's privacy
        mine = await services.cards.for_viewer(uid, res.partner_id)
        theirs = await services.cards.for_viewer(res.partner_id, uid)
        if mine is not None:
            await callback.message.answer(
                tr("match_new", lang).format(card=cards.render(mine, lang, anonymous=False)),
                reply_markup=kb.match_actions(lang),
            )
        partner_lang = await services.language.get(res.partner_id)  # they read it in their language
        if theirs is not None:
            with contextlib.suppress(TelegramAPIError):
                await bot.send_message(
                    res.partner_id,
                    tr("match_new", partner_lang).format(
                        card=cards.render(theirs, partner_lang, anonymous=False)
                    ),
                    reply_markup=kb.match_actions(partner_lang),
                )
        return
    if res.kind == "liked" and res.target_id is not None:
        await notify_new_follower(bot, services, res.target_id, uid)
    if res.kind in ("self", "not_eligible"):
        return
    if res.kind == "limit_reached":
        return await callback.message.answer(
            tr("limit.like", lang, limit=res.limit), reply_markup=kb.main_menu(lang)
        )
    if res.kind == "rate_limited":
        return await callback.message.answer(tr("browse_rate_limited", lang))
    if res.kind == "unavailable":
        await callback.message.answer(tr("profile_gone", lang))
    await _send_next(callback.message, services, uid, lang)


async def match_chat_cb(callback: CallbackQuery, services: Services, bot: Bot, lang: str) -> None:
    """💬 Chat on a card in /matches. Everything is decided on the server from the presser's own
    id and the profile handle: who may chat with whom is never taken from the button."""
    parsed = kb.parse_profile_cb(callback.data)
    msg = callback.message if isinstance(callback.message, Message) else None
    if parsed is None or parsed[0] != "mc" or msg is None:
        return await callback.answer()
    uid = callback.from_user.id
    res = await services.matchmaking.chat_with_match(uid, parsed[1])
    await callback.answer()
    if res.kind == "started" and res.partner and res.session_id is not None:
        delivered = await announce_chat_started(
            bot,
            services,
            res.session_id,
            uid,
            res.partner.user_id,
            lang,
            header="match_chat.header",
        )
        if not delivered:  # the match cannot be reached (blocked the bot): do not leave a dead chat
            await services.matchmaking.stop(uid, "partner_unreachable")
            await msg.answer(tr("match_chat.unavailable", lang), reply_markup=kb.main_menu(lang))
        return
    if res.kind == "already":
        return await msg.answer(tr("match_chat.already", lang), reply_markup=kb.chat_menu(lang))
    notice = {
        "busy": "match_chat.busy",
        "partner_busy": "match_chat.partner_busy",
        "rate_limited": "match_chat.rate",
        "cooldown": "cooldown",
        "not_eligible": "not_eligible",
    }
    await msg.answer(tr(notice.get(res.kind, "match_chat.unavailable"), lang))


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
    r.message.register(matches_cmd, Command("friends", "matches"), active)  # /matches: old name
    r.message.register(
        matches_cmd, F.text.in_(kb.labels("btn.matches") | kb.LEGACY_MATCHES_LABELS), active
    )
    r.callback_query.register(browse_cb, F.data == "br", active)
    r.callback_query.register(matches_cb, F.data == "mm", active)
    r.callback_query.register(swipe_cb, F.data.regexp(r"^(lk|ps):"), active)
    r.callback_query.register(match_chat_cb, F.data.regexp(r"^mc:"), active)
    r.callback_query.register(report_pick_reason, F.data.regexp(r"^(rp|mr):"), active)
    r.callback_query.register(report_submit, F.data.regexp(r"^(rq|rm):"), active)
    r.callback_query.register(block_cb, F.data.regexp(r"^(bk|mk):"), active)
    return r

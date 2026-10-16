"""Random chat: search, next/stop and the message relay (text + stickers only)."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import LinkPreviewOptions, Message

from .. import cards
from .. import keyboards as kb
from ..services import Services
from ..services.chat_relay import Incoming
from ..texts.i18n import tr
from .filters import ActiveUser

log = logging.getLogger(__name__)


async def _safe_send(bot: Bot, chat_id: int, text: str, **kw) -> bool:
    try:
        await bot.send_message(chat_id, text, **kw)
        return True
    except TelegramAPIError:
        log.info("could not deliver a bot message to a user")
        return False


async def _announce_end(
    bot: Bot, services: Services, me: int, partner: int, session_id: int, lang: str
) -> None:
    """Each side is told in their own language."""
    partner_lang = await services.language.get(partner)
    for uid, lg, key in (
        (me, lang, "chat_ended_me"),
        (partner, partner_lang, "chat_ended_partner"),
    ):
        await _safe_send(bot, uid, tr(key, lg), reply_markup=kb.main_menu(lg))
        await _safe_send(bot, uid, tr("after_chat", lg), reply_markup=kb.after_chat(session_id, lg))


async def _search(message: Message, services: Services, bot: Bot, lang: str) -> None:
    res = await services.matchmaking.search(message.from_user.id)
    if res.kind == "matched" and res.me and res.partner:
        partner_lang = await services.language.get(res.partner.user_id)
        # each side gets the other's card, filtered by THEIR entitlements and the OTHER's privacy
        for viewer, other, viewer_lang in (
            (res.me.user_id, res.partner.user_id, lang),
            (res.partner.user_id, res.me.user_id, partner_lang),
        ):
            card = await services.cards.for_viewer(viewer, other)
            if card is not None:
                await _safe_send(
                    bot,
                    viewer,
                    cards.chat_started(card, viewer_lang),
                    reply_markup=kb.chat_menu(viewer_lang),
                )
        return
    replies = {
        "queued": (tr("searching", lang), kb.chat_menu(lang)),
        "already_queued": (tr("already_searching", lang), kb.chat_menu(lang)),
        "already_in_chat": (tr("already_in_chat", lang), kb.chat_menu(lang)),
        "cooldown": (tr("cooldown", lang), kb.main_menu(lang)),
        "rate_limited": (tr("search_rate_limited", lang), None),
        "limit_reached": (tr("limit.chat", lang, limit=res.limit), kb.main_menu(lang)),
    }
    text, markup = replies.get(res.kind, (tr("not_eligible", lang), kb.remove))
    await message.answer(text, reply_markup=markup)


async def chat_cmd(message: Message, services: Services, bot: Bot, lang: str) -> None:
    await _search(message, services, bot, lang)


async def stop_cmd(message: Message, services: Services, bot: Bot, lang: str) -> None:
    res = await services.matchmaking.stop(message.from_user.id)
    if res.kind == "ended":
        await _announce_end(
            bot, services, message.from_user.id, res.partner_id, res.session_id, lang
        )
    elif res.kind == "left_queue":
        await message.answer(tr("search_cancelled", lang), reply_markup=kb.main_menu(lang))
    else:
        await message.answer(tr("not_in_chat", lang), reply_markup=kb.main_menu(lang))


async def next_cmd(message: Message, services: Services, bot: Bot, lang: str) -> None:
    stopped = await services.matchmaking.stop(message.from_user.id, "user_next")
    if stopped.kind == "ended":
        await _announce_end(
            bot, services, message.from_user.id, stopped.partner_id, stopped.session_id, lang
        )
    await _search(message, services, bot, lang)


def incoming_from(message: Message) -> Incoming:
    """Translate a Telegram message into the neutral structure the relay rules work on."""
    forwarded = message.forward_origin is not None
    via_bot = message.via_bot is not None
    if message.text is not None:
        types = tuple(e.type for e in (message.entities or []))
        return Incoming(
            "text", text=message.text, forwarded=forwarded, via_bot=via_bot, entity_types=types
        )
    if message.sticker is not None:
        return Incoming(
            "sticker", sticker_id=message.sticker.file_id, forwarded=forwarded, via_bot=via_bot
        )
    return Incoming("other", forwarded=forwarded, via_bot=via_bot)


# relay status -> catalog key of the notice shown to the sender
_STATUS_KEY = {
    "unsupported": "unsupported",
    "link": "link_blocked",
    "contact": "contact_blocked",
    "too_long": "too_long",
    "flood": "flood",
    "rate_limited": "chat_rate_limited",
    "muted": "muted",
    "not_in_chat": "not_in_chat",
}


async def relay(message: Message, services: Services, bot: Bot, lang: str) -> None:
    uid = message.from_user.id
    res = await services.relay.relay(uid, incoming_from(message))
    if res.status == "ok":
        try:
            if res.sticker_id:
                await bot.send_sticker(res.partner_id, res.sticker_id, protect_content=True)
            else:
                # parse_mode=None: user text must never be interpreted as HTML; plain re-send also
                # drops entities (hidden links), "forwarded from" metadata and formatting.
                await bot.send_message(
                    res.partner_id,
                    res.text,
                    parse_mode=None,
                    protect_content=True,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
        except (TelegramForbiddenError, TelegramAPIError):
            ended = await services.matchmaking.stop(uid, "partner_unreachable")
            await message.answer(tr("partner_unreachable", lang), reply_markup=kb.main_menu(lang))
            if ended.kind == "ended":
                await _safe_send(
                    bot,
                    uid,
                    tr("after_chat", lang),
                    reply_markup=kb.after_chat(ended.session_id, lang),
                )
        return
    if res.status == "empty":
        return
    if services.limiter.allow("notice", uid):  # don't let rejected spam trigger reply spam
        await message.answer(tr(_STATUS_KEY.get(res.status, "unsupported"), lang))


def build_router() -> Router:
    r = Router(name="chat")
    active = ActiveUser()
    r.message.register(chat_cmd, Command("chat"), active)
    r.message.register(chat_cmd, F.text.in_(kb.labels("btn.chat")), active)
    r.message.register(stop_cmd, Command("stop"), active)
    r.message.register(stop_cmd, F.text.in_(kb.labels("btn.stop")), active)
    r.message.register(next_cmd, Command("next"), active)
    r.message.register(next_cmd, F.text.in_(kb.labels("btn.next")), active)
    # Everything else from an active user (text, stickers, photos, files, ...) goes through the
    # relay rules; they decide what may pass. Slash-commands never reach it.
    r.message.register(relay, active, ~F.text.startswith("/"))
    return r

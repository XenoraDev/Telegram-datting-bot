"""Random chat: search, next/stop and the message relay (text + stickers only)."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import LinkPreviewOptions, Message

from .. import domain
from .. import keyboards as kb
from ..db.repo_chat import Peer
from ..services import Services
from ..services.chat_relay import Incoming
from ..texts import en as t
from .filters import ActiveUser

log = logging.getLogger(__name__)


def _peer_text(peer: Peer) -> str:
    return t.MATCHED.format(
        age=peer.age, gender=domain.GENDER_LABEL[peer.gender], city=t.esc(peer.city)
    )


async def _safe_send(bot: Bot, chat_id: int, text: str, **kw) -> bool:
    try:
        await bot.send_message(chat_id, text, **kw)
        return True
    except TelegramAPIError:
        log.info("could not deliver a bot message to a user")
        return False


async def _announce_end(bot: Bot, me: int, partner: int, session_id: int) -> None:
    await _safe_send(bot, me, t.CHAT_ENDED_ME, reply_markup=kb.main_menu)
    await _safe_send(bot, me, t.AFTER_CHAT, reply_markup=kb.after_chat(session_id))
    await _safe_send(bot, partner, t.CHAT_ENDED_PARTNER, reply_markup=kb.main_menu)
    await _safe_send(bot, partner, t.AFTER_CHAT, reply_markup=kb.after_chat(session_id))


async def _search(message: Message, services: Services, bot: Bot) -> None:
    res = await services.matchmaking.search(message.from_user.id)
    if res.kind == "matched" and res.me and res.partner:
        await _safe_send(bot, res.me.user_id, _peer_text(res.partner), reply_markup=kb.chat_menu)
        await _safe_send(bot, res.partner.user_id, _peer_text(res.me), reply_markup=kb.chat_menu)
        return
    replies = {
        "queued": (t.SEARCHING, kb.chat_menu),
        "already_queued": (t.ALREADY_SEARCHING, kb.chat_menu),
        "already_in_chat": (t.ALREADY_IN_CHAT, kb.chat_menu),
        "cooldown": (t.COOLDOWN, kb.main_menu),
        "rate_limited": (t.SEARCH_RATE_LIMITED, None),
    }
    text, markup = replies.get(res.kind, (t.NOT_ELIGIBLE, kb.remove))
    await message.answer(text, reply_markup=markup)


async def chat_cmd(message: Message, services: Services, bot: Bot) -> None:
    await _search(message, services, bot)


async def stop_cmd(message: Message, services: Services, bot: Bot) -> None:
    res = await services.matchmaking.stop(message.from_user.id)
    if res.kind == "ended":
        await _announce_end(bot, message.from_user.id, res.partner_id, res.session_id)
    elif res.kind == "left_queue":
        await message.answer(t.SEARCH_CANCELLED, reply_markup=kb.main_menu)
    else:
        await message.answer(t.NOT_IN_CHAT, reply_markup=kb.main_menu)


async def next_cmd(message: Message, services: Services, bot: Bot) -> None:
    stopped = await services.matchmaking.stop(message.from_user.id, "user_next")
    if stopped.kind == "ended":
        await _announce_end(bot, message.from_user.id, stopped.partner_id, stopped.session_id)
    await _search(message, services, bot)


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


_STATUS_TEXT = {
    "unsupported": t.UNSUPPORTED,
    "link": t.LINK_BLOCKED,
    "contact": t.CONTACT_BLOCKED,
    "too_long": t.TOO_LONG,
    "flood": t.FLOOD,
    "rate_limited": t.CHAT_RATE_LIMITED,
    "muted": t.MUTED,
    "not_in_chat": t.NOT_IN_CHAT,
}


async def relay(message: Message, services: Services, bot: Bot) -> None:
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
            await message.answer(t.PARTNER_UNREACHABLE, reply_markup=kb.main_menu)
            if ended.kind == "ended":
                await _safe_send(
                    bot, uid, t.AFTER_CHAT, reply_markup=kb.after_chat(ended.session_id)
                )
        return
    if res.status == "empty":
        return
    if services.limiter.allow("notice", uid):  # don't let rejected spam trigger reply spam
        await message.answer(_STATUS_TEXT.get(res.status, t.UNSUPPORTED))


def build_router() -> Router:
    r = Router(name="chat")
    active = ActiveUser()
    r.message.register(chat_cmd, Command("chat"), active)
    r.message.register(chat_cmd, F.text == kb.BTN_CHAT, active)
    r.message.register(stop_cmd, Command("stop"), active)
    r.message.register(stop_cmd, F.text == kb.BTN_STOP, active)
    r.message.register(next_cmd, Command("next"), active)
    r.message.register(next_cmd, F.text == kb.BTN_NEXT, active)
    # Everything else from an active user (text, stickers, photos, files, ...) goes through the
    # relay rules; they decide what may pass. Slash-commands never reach it.
    r.message.register(relay, active, ~F.text.startswith("/"))
    return r

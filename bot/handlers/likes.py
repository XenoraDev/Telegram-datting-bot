"""/likes: who liked me. Everyone sees how many; the Pro / VIP entitlement shows who."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from .. import cards
from .. import keyboards as kb
from ..services import Services
from ..texts.i18n import tr
from .filters import ActiveUser


async def likes_cmd(message: Message, services: Services, lang: str) -> None:
    uid = message.from_user.id
    if await services.matchmaking.current_chat(uid):
        return await message.answer(tr("browse_in_chat", lang))
    total, found = await services.cards.who_liked_me(uid)
    if not total:
        return await message.answer(tr("likes.none", lang), reply_markup=kb.main_menu(lang))
    if found is None:
        return await message.answer(tr("likes.locked", lang, n=total))
    await message.answer(tr("likes.header", lang, n=total))
    for card in found:
        await message.answer(
            cards.render(card, lang, anonymous=False),
            reply_markup=kb.browse_card(card.public_id, lang, filters_button=False),
        )
    if total > len(found):
        await message.answer(tr("matches_more", lang).format(n=total - len(found)))


def build_router() -> Router:
    r = Router(name="likes")
    r.message.register(likes_cmd, Command("likes"), ActiveUser())
    return r

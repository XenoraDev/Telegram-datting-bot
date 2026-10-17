"""👁 Show age & gender: the button under a masked chat card (pc:r:<session id>).

The callback data is only the chat session id; nothing protected is ever in it. Every press is
decided afresh on the server:

    not the presser's current chat (forged id, someone else's, ended) -> "chat ended", no data
    owner hid the field                                               -> shown as hidden
    viewer entitled and owner allows                                  -> the real value
    viewer not entitled                                               -> alert + plans, no value

Values come from PartnerCardService.for_viewer, i.e. the same SQL-gated path as the card itself,
so the owner's privacy always wins and a non-entitled viewer's query never selects the value.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from .. import cards
from .. import keyboards as kb
from ..services import Services
from ..texts.i18n import tr
from .common import drop_buttons
from .filters import ActiveUser
from .premium import plans_cta


async def reveal_cb(callback: CallbackQuery, services: Services, lang: str) -> None:
    session_id = kb.parse_reveal_cb(callback.data)
    msg = callback.message if isinstance(callback.message, Message) else None
    if session_id is None or msg is None:
        return await callback.answer()
    uid = callback.from_user.id
    if not services.limiter.allow("reveal", uid):
        return await callback.answer(tr("premium.rate_limited", lang), show_alert=True)
    current = await services.matchmaking.current_chat(uid)
    if current is None or current[0] != session_id:
        await drop_buttons(callback)
        return await callback.answer(tr("pc.reveal_ended", lang), show_alert=True)
    card = await services.cards.for_viewer(uid, current[1], nudge=False)
    if card is None:  # the partner's account is gone
        await drop_buttons(callback)
        return await callback.answer(tr("pc.reveal_ended", lang), show_alert=True)
    if cards.needs_reveal_button(card):
        await callback.answer(tr("pc.reveal_locked", lang), show_alert=True)
        plans = await plans_cta(services, uid, lang)
        if plans is not None:
            await msg.answer(plans)
        return
    await callback.answer()
    with contextlib.suppress(TelegramAPIError):
        await msg.edit_text(cards.render(card, lang, anonymous=True), reply_markup=None)


def build_router() -> Router:
    r = Router(name="card_reveal")
    r.callback_query.register(reveal_cb, F.data.startswith("pc:r:"), ActiveUser())
    return r

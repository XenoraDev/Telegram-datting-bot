"""The "someone followed you" push.

Sent to the person who was followed, right after a follow that is not mutual (a mutual follow
makes friends and is announced to both by browse.swipe_cb). What it shows depends on the
receiver's plan, exactly as /followers does: Free get a COUNT and no identity, Pro / VIP (the
`see_who_liked` entitlement) get the follower's card, filtered by their entitlements and the
follower's privacy, with a "Follow back" button. Rate limited per receiver, skipped while the
receiver is in a chat (they find it in /followers), and never able to break the follower's flow.
"""

from __future__ import annotations

import logging

from aiogram import Bot

from .. import cards
from .. import keyboards as kb
from ..services import Services
from ..telegram_send import deliver
from ..texts.i18n import tr

log = logging.getLogger(__name__)


async def notify_new_follower(bot: Bot, services: Services, target: int, follower: int) -> bool:
    """True if a push was sent. Never raises."""
    try:
        if await services.matchmaking.current_chat(target):
            return False
        sees_who = (await services.entitlements.get(target, "see_who_liked")).enabled
        policy = "follow_notice_pro" if sees_who else "follow_notice_free"
        if not services.limiter.allow(policy, target):
            return False
        lang = await services.language.get(target)
        if sees_who:
            card = await services.cards.for_viewer(target, follower, nudge=False)
            if card is None:
                return False
            text = tr("follow.new_pro", lang, card=cards.render(card, lang, anonymous=False))
            markup = kb.follow_back_card(card.public_id, lang)
        else:
            waiting, _ = await services.cards.who_liked_me(target)
            text = tr("follow.new_free", lang, n=max(waiting, 1))
            markup = None
        return await deliver(bot, target, text, alerts=services.alerts, reply_markup=markup)
    except Exception:  # a failed push must never break the follower's browsing
        log.exception("could not send a new-follower notice")
        return False

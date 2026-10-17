"""Last router: anything nobody else handled."""

from __future__ import annotations

import asyncpg
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from .. import domain
from .. import keyboards as kb
from ..services import Services
from ..texts.i18n import tr
from .start import resume_onboarding


async def fallback(
    message: Message,
    services: Services,
    state: FSMContext,
    lang: str,
    user: asyncpg.Record | None = None,
) -> None:
    if not services.limiter.allow("notice", message.from_user.id):
        return
    if user is None:
        await message.answer(tr("need_start", lang))
    elif user["status"] == domain.ONBOARDING:
        # A menu button although the sign-up is not finished and no conversation is open: the bot
        # was restarted (conversation state is in memory). Pick the sign-up up again instead of
        # a dead end. Other text keeps the plain "finish setup" answer.
        if (
            message.text in kb.MENU_BUTTON_LABELS
            and await state.get_state() is None
            and await resume_onboarding(message, services, state, lang)
        ):
            return
        await message.answer(tr("finish_setup", lang))
    else:
        await message.answer(tr("unknown", lang))


def build_router() -> Router:
    r = Router(name="fallback")
    r.message.register(fallback)
    return r

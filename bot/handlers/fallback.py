"""Last router: anything nobody else handled."""

from __future__ import annotations

import asyncpg
from aiogram import Router
from aiogram.types import Message

from .. import domain
from ..services import Services
from ..texts.i18n import tr


async def fallback(
    message: Message, services: Services, lang: str, user: asyncpg.Record | None = None
) -> None:
    if not services.limiter.allow("notice", message.from_user.id):
        return
    if user is None:
        await message.answer(tr("need_start", lang))
    elif user["status"] == domain.ONBOARDING:
        await message.answer(tr("finish_setup", lang))
    else:
        await message.answer(tr("unknown", lang))


def build_router() -> Router:
    r = Router(name="fallback")
    r.message.register(fallback)
    return r

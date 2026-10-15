"""Last router: anything nobody else handled."""

from __future__ import annotations

import asyncpg
from aiogram import Router
from aiogram.types import Message

from .. import domain
from ..services import Services
from ..texts import en as t


async def fallback(
    message: Message, services: Services, user: asyncpg.Record | None = None
) -> None:
    if not services.limiter.allow("notice", message.from_user.id):
        return
    if user is None:
        await message.answer(t.NEED_START)
    elif user["status"] == domain.ONBOARDING:
        await message.answer(t.FINISH_SETUP)
    else:
        await message.answer(t.UNKNOWN)


def build_router() -> Router:
    r = Router(name="fallback")
    r.message.register(fallback)
    return r

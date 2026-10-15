from __future__ import annotations

import asyncpg
from aiogram.filters import Filter
from aiogram.types import TelegramObject

from .. import domain


class ActiveUser(Filter):
    """True only for fully onboarded adults. Services re-check this in SQL as well."""

    async def __call__(self, event: TelegramObject, user: asyncpg.Record | None = None) -> bool:
        return bool(
            user and user["status"] == domain.ACTIVE and user["age_confirmed_at"] is not None
        )

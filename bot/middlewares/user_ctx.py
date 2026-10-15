"""Resolve the acting user from the *Telegram update* (never from message content)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, TelegramObject

from ..db import repo_users


class UserContextMiddleware(BaseMiddleware):
    """Ignores everything except private chats with real users, then loads `user`."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        chat = data.get("event_chat")
        if tg_user is None or tg_user.is_bot or chat is None or chat.type != "private":
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except TelegramAPIError:
                    pass
            return None
        data["user"] = await repo_users.get(self._pool, tg_user.id)
        return await handler(event, data)

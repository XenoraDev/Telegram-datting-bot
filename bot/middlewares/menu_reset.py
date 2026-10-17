"""A menu button always wins over a half-finished conversation.

Conversation state (FSM) lives in memory. After a restart it is gone, and a conversation can also
be left behind (a user wandering off in the middle of a form). Either way a tap on a menu button
must work, so it ends whatever conversation is open before the handlers look at the message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

FORM_PREFIX = "Reg:"  # the sign-up form group (handlers/profile.py)


class MenuResetMiddleware(BaseMiddleware):
    def __init__(self, labels: frozenset[str]) -> None:
        self._labels = labels

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        if isinstance(event, Message) and event.text in self._labels and state is not None:
            current = await state.get_state()
            # The sign-up form (Reg) is the exception: a live form answers a menu tap with its
            # own "please answer the question" hint; an interrupted one is picked up again by the
            # fallback handler.
            if current is not None and not current.startswith(FORM_PREFIX):
                await state.clear()
                data["raw_state"] = None  # filters read the state once per update: it ended
        return await handler(event, data)

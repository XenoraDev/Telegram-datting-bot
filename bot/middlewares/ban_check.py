"""Account-status gate: blocked / banned / deleted users cannot use the bot normally."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

from .. import domain
from ..keyboards import remove
from ..texts.i18n import tr

PUBLIC_COMMANDS = frozenset({"terms", "about"})
_COMMAND = re.compile(r"^/(\w+)(?:@\w+)?(?:\s|$)")


def command_of(text: str | None) -> str | None:
    m = _COMMAND.match(text or "")
    return m.group(1).lower() if m else None


class AccessMiddleware(BaseMiddleware):
    """Runs after UserContextMiddleware (needs data['user']).

    * /terms and /about    -> anyone, whatever the status
    * no account yet      -> only /start
    * deleted             -> only /start (which starts a brand-new onboarding)
    * blocked_underage / banned -> nothing, except /deleteme (privacy right)
    * onboarding / active -> pass through; feature routers check `active` themselves
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("user")
        lang = data.get("lang", "en")
        status = user["status"] if user else None
        if status in (domain.ONBOARDING, domain.ACTIVE):
            return await handler(event, data)

        cmd = command_of(event.text) if isinstance(event, Message) else None
        cb = event.data if isinstance(event, CallbackQuery) else None

        if cmd in PUBLIC_COMMANDS:  # the rules and the privacy summary are readable by anyone
            return await handler(event, data)

        if status is None or status == domain.DELETED:
            if cmd == "start":
                return await handler(event, data)
            await self._refuse(
                event, tr("need_start" if status is None else "account_deleted", lang)
            )
            return None

        # blocked_underage / banned
        if cmd == "deleteme" or (cb or "").startswith("del:"):
            return await handler(event, data)
        await self._refuse(
            event,
            tr(
                "blocked_adults_only" if status == domain.BLOCKED_UNDERAGE else "blocked_banned",
                lang,
            ),
        )
        return None

    @staticmethod
    async def _refuse(event: TelegramObject, text: str) -> None:
        try:
            if isinstance(event, Message):
                await event.answer(text, reply_markup=remove)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
        except TelegramAPIError:
            pass

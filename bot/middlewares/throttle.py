"""Per-user rate limiting for every incoming message / button press."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..services.moderation import ModerationService
from ..services.rate_limit import MUTE_AFTER_VIOLATIONS, RateLimiter
from ..texts import en as t

log = logging.getLogger(__name__)


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, limiter: RateLimiter, moderation: ModerationService, policy: str) -> None:
        self._limiter = limiter
        self._moderation = moderation
        self._policy = policy  # "message" | "callback"

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or self._limiter.allow(self._policy, tg_user.id):
            return await handler(event, data)

        # Over the limit: drop the update. Persistent spammers get a temporary mute.
        if self._limiter.record_violation(tg_user.id) >= MUTE_AFTER_VIOLATIONS:
            self._limiter.clear_violations(tg_user.id)
            await self._moderation.mute_for_abuse(tg_user.id)
            log.info("user muted for rate-limit abuse")
        if self._limiter.allow("slow_notice", tg_user.id):
            try:
                if isinstance(event, Message):
                    await event.answer(t.SLOW_DOWN)
                elif isinstance(event, CallbackQuery):
                    await event.answer(t.SLOW_DOWN)
            except TelegramAPIError:
                pass
        return None

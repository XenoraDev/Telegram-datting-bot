"""Everything the request handlers share, built once at start-up (or injected by tests)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from aiogram import Bot
from fastapi.templating import Jinja2Templates

from bot.handlers.common import notify_partner_left, notify_user
from bot.services import Services, build_services

from .auth import LoginVerifier, SessionStore
from .config import WebSettings
from .ratelimit import LoginThrottle

APP_DIR = Path(__file__).resolve().parent


@dataclass(slots=True)
class WebState:
    settings: WebSettings
    pool: asyncpg.Pool
    services: Services
    verifier: LoginVerifier
    sessions: SessionStore
    throttle: LoginThrottle
    templates: Jinja2Templates
    bot: Bot | None  # only used to tell a chat partner the chat ended after a ban

    async def notify_partner(self, partner_id: int | None) -> None:
        if partner_id is not None and self.bot is not None:
            await notify_partner_left(self.bot, partner_id, self.services)

    async def notify(
        self, uid: int | None, key: str, *, live_ticket: int | None = None, **fmt: object
    ) -> None:
        """Tell a user something (e.g. their payment was reviewed), in their own language."""
        if uid is not None and self.bot is not None:
            await notify_user(self.bot, uid, key, self.services, live_ticket=live_ticket, **fmt)


def make_state(
    settings: WebSettings,
    pool: asyncpg.Pool,
    bot: Bot | None,
    wall_clock: Callable[[], float] = time.time,
    mono_clock: Callable[[], float] = time.monotonic,
) -> WebState:
    return WebState(
        settings=settings,
        pool=pool,
        services=build_services(
            pool, wall_clock, mono_clock, settings.admin_ids, loadtest_settings=settings.loadtest
        ),
        verifier=LoginVerifier(
            settings.bot.bot_token, settings.admin_ids, settings.login_max_age, wall_clock
        ),
        sessions=SessionStore(
            settings.session_ttl, settings.idle_ttl, settings.admin_ids, mono_clock
        ),
        throttle=LoginThrottle(mono_clock),
        templates=Jinja2Templates(directory=APP_DIR / "templates"),
        bot=bot,
    )

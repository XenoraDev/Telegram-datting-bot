"""Process entry point: config -> logging -> DB pool -> migrations -> long polling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.utils.token import TokenValidationError

from . import keyboards as kb
from .config import ConfigError, Settings, load_settings
from .db.migrate import MigrationError, migrate
from .db.pool import create_pool
from .handlers import account, chat, fallback, profile, safety, start
from .health import heartbeat_loop
from .middlewares.ban_check import AccessMiddleware
from .middlewares.throttle import ThrottleMiddleware
from .middlewares.user_ctx import UserContextMiddleware
from .services import Services, build_services
from .texts import en as t

log = logging.getLogger(__name__)


def build_dispatcher(
    pool: asyncpg.Pool, settings: Settings, services: Services | None = None
) -> Dispatcher:
    """Wire middlewares and routers. pool / settings / services are injected into handlers."""
    services = services or build_services(pool)
    dp = Dispatcher(pool=pool, settings=settings, services=services)
    # Order matters: throttle first (cheap, in-memory), then identify the user, then the
    # account-status gate. Only private chats with real users get past UserContextMiddleware.
    dp.message.outer_middleware(
        ThrottleMiddleware(services.limiter, services.moderation, "message")
    )
    dp.callback_query.outer_middleware(
        ThrottleMiddleware(services.limiter, services.moderation, "callback")
    )
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(UserContextMiddleware(pool))
        observer.outer_middleware(AccessMiddleware())
    # Order matters: the chat relay is a catch-all for active users and fallback is last.
    dp.include_routers(
        start.build_router(),
        account.build_router(),
        profile.build_router(),
        safety.build_router(),
        chat.build_router(),
        fallback.build_router(),
    )
    return dp


async def run_maintenance(services: Services, bot: Bot) -> None:
    """Periodic housekeeping: expire report contexts, RAM buffers, stale queue entries."""
    purged = await services.moderation.purge_expired_contexts()
    services.buffer.prune()
    services.limiter.prune()
    services.relay.prune()
    expired = await services.matchmaking.expire_stale_queue()
    for uid in expired:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(uid, t.SEARCH_EXPIRED, reply_markup=kb.main_menu)
    if purged or expired:
        log.info(
            "maintenance: %d report contexts purged, %d searches expired", purged, len(expired)
        )


async def maintenance_loop(services: Services, bot: Bot, interval: float = 600.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await run_maintenance(services, bot)
        except Exception:  # never let housekeeping kill the bot
            log.exception("maintenance run failed")


async def run(settings: Settings) -> None:
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    pool: asyncpg.Pool | None = None
    heartbeat: asyncio.Task[None] | None = None
    maintenance: asyncio.Task[None] | None = None
    try:
        # Migrations get their own connection without a command timeout: a long migration must
        # not be cancelled by the 30s limit that protects normal bot queries.
        migration_pool = await create_pool(
            settings.database_url, min_size=1, max_size=1, command_timeout=None
        )
        try:
            await migrate(migration_pool)
        finally:
            await migration_pool.close()

        pool = await create_pool(settings.database_url)
        heartbeat = asyncio.create_task(
            heartbeat_loop(settings.heartbeat_file, probe=bot.get_me), name="heartbeat"
        )
        services = build_services(pool)
        await run_maintenance(services, bot)  # also drops report contexts that expired while down
        maintenance = asyncio.create_task(maintenance_loop(services, bot), name="maintenance")
        dp = build_dispatcher(pool, settings, services)
        # A webhook set on this token would make long polling fail forever (409).
        await bot.delete_webhook()
        log.info("starting long polling")
        await dp.start_polling(  # handles SIGINT/SIGTERM and returns on shutdown
            bot, allowed_updates=dp.resolve_used_update_types()
        )
    finally:
        try:
            for task in (heartbeat, maintenance):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        finally:
            try:
                await bot.session.close()
            finally:
                if pool is not None:
                    await pool.close()


def main() -> int:
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        asyncio.run(run(settings))
    except TokenValidationError as exc:
        print(f"configuration error: BOT_TOKEN is invalid ({exc})", file=sys.stderr)
        return 2
    except TelegramUnauthorizedError:
        print("configuration error: Telegram rejected BOT_TOKEN", file=sys.stderr)
        return 2
    except TelegramNetworkError as exc:
        log.error("cannot reach Telegram: %s", exc)
        return 1
    except (MigrationError, ConnectionError, TimeoutError) as exc:
        log.error("startup failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError

from .config import ConfigError, Settings, load_settings
from .db.migrate import MigrationError, migrate
from .db.pool import create_pool
from .handlers import start
from .health import heartbeat_loop

log = logging.getLogger(__name__)


def build_dispatcher(pool: asyncpg.Pool, settings: Settings) -> Dispatcher:
    """Wire routers. ``pool`` and ``settings`` are injected into handlers by name."""
    dp = Dispatcher(pool=pool, settings=settings)
    dp.include_router(start.build_router())
    return dp


async def run(settings: Settings) -> None:
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    pool: asyncpg.Pool | None = None
    heartbeat: asyncio.Task[None] | None = None
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
        dp = build_dispatcher(pool, settings)
        # A webhook set on this token would make long polling fail forever (409).
        await bot.delete_webhook()
        log.info("starting long polling")
        await dp.start_polling(bot)  # handles SIGINT/SIGTERM and returns on shutdown
    finally:
        try:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
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

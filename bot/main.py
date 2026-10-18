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
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.utils.token import TokenValidationError

from . import keyboards as kb
from .config import ConfigError, Settings, load_settings
from .db.migrate import MigrationError, migrate
from .db.pool import create_pool
from .handlers import (
    account,
    admin,
    boost,
    browse,
    browse_filters,
    card_reveal,
    chat,
    errors,
    fallback,
    language,
    likes,
    loadtest,
    premium,
    privacy,
    profile,
    safety,
    start,
    support,
)
from .health import heartbeat_loop
from .loadtest.safety import LoadTestSettings
from .middlewares.ban_check import AccessMiddleware
from .middlewares.menu_reset import MenuResetMiddleware
from .middlewares.throttle import ThrottleMiddleware
from .middlewares.user_ctx import UserContextMiddleware
from .services import Services, build_services
from .telegram_send import deliver
from .texts import en as texts_en
from .texts.i18n import tr

log = logging.getLogger(__name__)


def build_dispatcher(
    pool: asyncpg.Pool, settings: Settings, services: Services | None = None
) -> Dispatcher:
    """Wire middlewares and routers. pool / settings / services are injected into handlers."""
    services = services or build_services(pool, admin_ids=settings.admin_ids)
    dp = Dispatcher(pool=pool, settings=settings, services=services)
    # Order matters: throttle first (cheap, in-memory), then identify the user, then the
    # account-status gate. Only private chats with real users get past UserContextMiddleware.
    dp.message.outer_middleware(
        ThrottleMiddleware(services.limiter, services.moderation, "message", services.language.get)
    )
    dp.callback_query.outer_middleware(
        ThrottleMiddleware(services.limiter, services.moderation, "callback", services.language.get)
    )
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(UserContextMiddleware(pool))
        observer.outer_middleware(AccessMiddleware())
    dp.message.outer_middleware(MenuResetMiddleware(kb.MENU_BUTTON_LABELS))
    # Order matters: the chat relay is a catch-all for active users and fallback is last.
    dp.include_routers(
        # first: while a support conversation is open, a command or menu button must end it
        # before any other router handles it (state is read once per update)
        support.build_router(),
        start.build_router(),
        account.build_router(),
        profile.build_router(),
        safety.build_router(),
        admin.build_router(),
        loadtest.build_router(),
        language.build_router(),
        premium.build_router(),
        privacy.build_router(),
        likes.build_router(),
        browse_filters.build_router(),
        boost.build_router(),
        browse.build_router(),
        card_reveal.build_router(),
        chat.build_router(),
        fallback.build_router(),
    )
    dp.errors.register(errors.on_error)
    return dp


async def run_maintenance(services: Services, bot: Bot) -> None:
    """Periodic housekeeping: expire report contexts, RAM buffers, stale queue entries."""
    purged = await services.moderation.purge_expired_contexts()
    services.buffer.prune()
    services.limiter.prune()
    services.relay.prune()
    await services.entitlements.purge_old_usage()
    await services.payments.maintain()  # stale orders, old screenshots, expired subscriptions
    expired = await services.matchmaking.expire_stale_queue()
    for uid in expired:
        lang = await services.language.get(uid)
        await deliver(
            bot,
            uid,
            tr("search_expired", lang),
            alerts=services.alerts,
            reply_markup=kb.main_menu(lang),
        )
    await send_expiry_reminders(services, bot)
    if purged or expired:
        log.info(
            "maintenance: %d report contexts purged, %d searches expired", purged, len(expired)
        )


async def send_expiry_reminders(services: Services, bot: Bot) -> int:
    """Tell users whose plan ends within 3 days, once per end date, in their own language."""
    from .handlers.common import notify_user
    from .handlers.premium import days_left, fmt_date

    sent = 0
    for r in await services.payments.expiry_reminders():
        await notify_user(
            bot,
            r["user_id"],
            "reminder.expiring",
            services,
            plan=texts_en.esc(r["plan_name"] or ""),
            date=fmt_date(r["period_end"]),
            days=days_left(r["period_end"]),
        )
        sent += 1
    if sent:
        log.info("maintenance: %d plan-expiry reminders sent", sent)
    return sent


async def maintenance_loop(services: Services, bot: Bot, interval: float = 600.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await run_maintenance(services, bot)
        except Exception:  # never let housekeeping kill the bot
            log.exception("maintenance run failed")


async def close_idle_chats(services: Services, bot: Bot) -> int:
    """Close chats that have been silent for the configured time and tell both people. Each chat
    is closed (and announced) by exactly one call, even if two bot processes ran this."""
    from .handlers.chat import announce_idle_end

    minutes = services.matchmaking.chat_inactivity_minutes
    closed = await services.matchmaking.end_idle_chats()
    for session_id, user_a, user_b in closed:
        await announce_idle_end(bot, services, session_id, (user_a, user_b), minutes)
    if closed:
        log.info("chat inactivity: %d silent chats closed", len(closed))
    return len(closed)


async def chat_inactivity_loop(services: Services, bot: Bot, interval: float = 60.0) -> None:
    """A short loop of its own (the 10-minute maintenance loop would close a chat up to 10
    minutes late). One cheap indexed query per tick."""
    while True:
        await asyncio.sleep(interval)
        try:
            await close_idle_chats(services, bot)
        except Exception:  # never let housekeeping kill the bot
            log.exception("chat inactivity run failed")


async def run(settings: Settings) -> None:
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    pool: asyncpg.Pool | None = None
    heartbeat: asyncio.Task[None] | None = None
    maintenance: asyncio.Task[None] | None = None
    inactivity: asyncio.Task[None] | None = None
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
        services = build_services(
            pool,
            admin_ids=settings.admin_ids,
            loadtest_settings=LoadTestSettings.from_env(os.environ),
            queue_timeout_minutes=settings.queue_timeout_minutes,
            chat_inactivity_minutes=settings.chat_inactivity_timeout_minutes,
        )
        await run_maintenance(services, bot)  # also drops report contexts that expired while down
        try:
            await services.payments.ensure_default_plans()  # Premium / Pro / VIP, idempotent
        except Exception:
            log.exception("could not set up the default tier plans (the admin page can retry)")
        maintenance = asyncio.create_task(maintenance_loop(services, bot), name="maintenance")
        inactivity = asyncio.create_task(
            chat_inactivity_loop(services, bot), name="chat-inactivity"
        )
        dp = build_dispatcher(pool, settings, services)
        # A webhook set on this token would make long polling fail forever (409).
        await bot.delete_webhook()
        log.info("starting long polling")
        await dp.start_polling(  # handles SIGINT/SIGTERM and returns on shutdown
            bot, allowed_updates=dp.resolve_used_update_types()
        )
    finally:
        try:
            for task in (heartbeat, maintenance, inactivity):
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

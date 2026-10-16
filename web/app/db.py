"""Database access for the web admin: the same PostgreSQL and the same asyncpg layer as the bot.

The bot owns migrations. The web service never changes the schema; it only waits until the
database is at the version this code was built for.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, discover
from bot.db.pool import create_pool

log = logging.getLogger(__name__)


async def open_pool(dsn: str) -> asyncpg.Pool:
    return await create_pool(dsn, min_size=1, max_size=5, application_name="dating-bot-web")


async def wait_for_schema(pool: asyncpg.Pool, attempts: int = 60, delay: float = 2.0) -> None:
    """Block until every migration shipped in this image is recorded (the bot applies them)."""
    expected = {m.version for m in discover(DEFAULT_MIGRATIONS_DIR)}
    for attempt in range(1, attempts + 1):
        try:
            rows = await pool.fetch("SELECT version FROM schema_migrations")
            if expected <= {r["version"] for r in rows}:
                return
        except asyncpg.UndefinedTableError:
            pass
        log.warning("waiting for the bot to apply migrations (%s/%s)", attempt, attempts)
        await asyncio.sleep(delay)
    raise RuntimeError("database schema is not up to date; start the bot service first")

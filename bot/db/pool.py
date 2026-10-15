"""asyncpg connection pool creation."""

from __future__ import annotations

import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)


async def create_pool(
    dsn: str,
    *,
    attempts: int = 10,
    delay: float = 1.0,
    min_size: int = 1,
    max_size: int = 10,
    command_timeout: float | None = 30,
) -> asyncpg.Pool:
    """Create a pool, retrying while the database is still starting up.

    docker compose already waits for the db healthcheck; the retry covers restarts
    of the db container while the bot is running.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.create_pool(
                dsn, min_size=min_size, max_size=max_size, command_timeout=command_timeout
            )
        except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
            last_error = exc
            log.warning("database not ready (attempt %s/%s): %s", attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(delay)
    raise ConnectionError(
        f"could not connect to the database after {attempts} attempts"
    ) from last_error

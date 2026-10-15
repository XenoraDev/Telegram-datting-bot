from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from bot.db.pool import create_pool


async def test_create_pool_connects(dsn):
    pool = await create_pool(dsn, attempts=2, delay=0.01)
    try:
        assert await pool.fetchval("SELECT 1") == 1
    finally:
        await pool.close()


async def test_create_pool_gives_up_after_attempts():
    # Port 1 is closed on any normal machine: connection refused every time.
    with pytest.raises(ConnectionError, match="3 attempts"):
        await create_pool("postgresql://u:p@127.0.0.1:1/x", attempts=3, delay=0.01)


async def test_create_pool_retries_then_succeeds_after_transient_errors():
    fake_pool = object()
    create = AsyncMock(
        side_effect=[
            asyncpg.CannotConnectNowError("the database system is starting up"),
            OSError("connection refused"),
            fake_pool,
        ]
    )
    with patch("bot.db.pool.asyncpg.create_pool", create):
        result = await create_pool("postgresql://u:p@h/d", attempts=5, delay=0)
    assert result is fake_pool
    assert create.await_count == 3


async def test_create_pool_makes_exactly_the_requested_number_of_attempts():
    create = AsyncMock(side_effect=OSError("down"))
    with (
        patch("bot.db.pool.asyncpg.create_pool", create),
        pytest.raises(ConnectionError, match="4 attempts"),
    ):
        await create_pool("postgresql://u:p@h/d", attempts=4, delay=0)
    assert create.await_count == 4


async def test_create_pool_does_not_swallow_programming_errors():
    create = AsyncMock(side_effect=ValueError("bad dsn"))
    with patch("bot.db.pool.asyncpg.create_pool", create), pytest.raises(ValueError):
        await create_pool("postgresql://u:p@h/d", attempts=3, delay=0)
    assert create.await_count == 1


async def test_command_timeout_is_configurable(dsn):
    pool = await create_pool(dsn, command_timeout=None, max_size=1)
    try:
        assert pool._connect_kwargs.get("command_timeout") is None
    finally:
        await pool.close()

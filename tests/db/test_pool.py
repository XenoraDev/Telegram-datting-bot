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

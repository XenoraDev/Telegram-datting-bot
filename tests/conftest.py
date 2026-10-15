"""Shared fixtures.

DB tests need a real PostgreSQL. Point ``TEST_DATABASE_URL`` at any database on a server where
the user may CREATE DATABASE (e.g. ``postgresql://postgres@127.0.0.1:5432/postgres``). Every test
gets its own freshly created database, dropped afterwards, so migrations always run from scratch.
Without ``TEST_DATABASE_URL`` the DB tests are skipped.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()


def _with_database(dsn: str, dbname: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@pytest.fixture
async def dsn() -> AsyncIterator[str]:
    """DSN of a brand-new empty database, created for this test only."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    name = f"test_{uuid.uuid4().hex[:16]}"
    admin = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        yield _with_database(TEST_DATABASE_URL, name)
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
async def pool(dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def app_pool(pool: asyncpg.Pool) -> asyncpg.Pool:
    """A pool on a fresh database with ALL migrations applied (what the running bot sees)."""
    from bot.db.migrate import migrate

    await migrate(pool)
    return pool


@pytest.fixture
async def tg(app_pool: asyncpg.Pool):
    """Fake Telegram wired to the real Dispatcher + services + a fresh migrated database."""
    from tests.handlers.harness import FakeTelegram

    async with FakeTelegram(app_pool) as fake:
        yield fake


@pytest.fixture
def clock():
    from tests.handlers.harness import FakeClock

    return FakeClock()


@pytest.fixture
def svc(app_pool: asyncpg.Pool, clock):
    """Service graph on a controllable clock (no Telegram involved)."""
    from bot.services import build_services

    return build_services(app_pool, clock, clock)

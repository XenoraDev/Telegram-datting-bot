"""Fixtures: a service on a migrated database that PROVES it is a load-test environment."""

from __future__ import annotations

import itertools
from urllib.parse import urlsplit

import pytest

from bot.loadtest.safety import LoadTestSettings
from bot.services.loadtest import LoadTestService
from bot.services.rate_limit import RateLimiter

ADMIN = 9000001
OTHER_ADMIN = 9000002
STRANGER = 5551234
ENV_ID = "env-testing-001"
BOT_ID = "7777777"


def env_for(dsn: str, **over: str) -> dict[str, str]:
    name = urlsplit(dsn).path.lstrip("/")
    env = {
        "BOT_TOKEN": f"{BOT_ID}:TEST-token-not-real",
        "DATABASE_URL": dsn,
        "ADMIN_IDS": f"{ADMIN},{OTHER_ADMIN}",
        "APP_ENV": "testing",
        "LOAD_TEST_ENABLED": "1",
        "LOAD_TEST_ENV_ID": ENV_ID,
        "LOAD_TEST_DB_NAME": name,
        "LOAD_TEST_BOT_ID": BOT_ID,
        "ADMIN_DOMAIN": "loadtest.example.com",
        "LOAD_TEST_ALLOWED_DOMAINS": "loadtest.example.com",
    }
    return env | over


def slow_clock_limiter() -> RateLimiter:
    """A limiter that never throttles the tests (every call is 100 s after the last)."""
    ticks = itertools.count()
    return RateLimiter(lambda: next(ticks) * 100.0)


async def mark_database(pool, dsn: str, env_id: str = ENV_ID) -> None:
    name = urlsplit(dsn).path.lstrip("/")
    await pool.execute(f"COMMENT ON DATABASE \"{name}\" IS 'load-test-environment:{env_id}'")  # noqa: S608


@pytest.fixture
async def lt(app_pool, dsn) -> LoadTestService:
    """Service in a correctly declared and marked test environment."""
    await mark_database(app_pool, dsn)
    settings = LoadTestSettings.from_env(env_for(dsn))
    return LoadTestService(
        app_pool, slow_clock_limiter(), frozenset({ADMIN, OTHER_ADMIN}), settings
    )


@pytest.fixture
def unsafe(app_pool, dsn):
    """Builder for a service whose environment is wrong in exactly the given way."""

    def build(**over: str) -> LoadTestService:
        settings = LoadTestSettings.from_env(env_for(dsn, **over))
        return LoadTestService(app_pool, slow_clock_limiter(), frozenset({ADMIN}), settings)

    return build


FORM = {
    "profile": "custom",
    "chats": "50",
    "msgs_per_chat_min": "40",
    "duration_min": "1",
    "ramp_s": "10",
}

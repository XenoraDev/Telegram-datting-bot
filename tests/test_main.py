"""Startup/shutdown behaviour of bot.main (Telegram is never contacted)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe

from bot import main as bot_main
from bot.config import Settings
from bot.db.migrate import MigrationError


def settings_for(dsn: str, heartbeat: str) -> Settings:
    return Settings(bot_token="1:x", database_url=dsn, heartbeat_file=heartbeat)


# ---------- main(): exit codes ----------


def test_main_exits_2_on_missing_config(monkeypatch, capsys):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert bot_main.main() == 2
    assert "configuration error" in capsys.readouterr().err


@pytest.fixture
def valid_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:ABCdef")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")


def test_main_exits_2_on_invalid_token_format(monkeypatch, capsys):
    monkeypatch.setenv("BOT_TOKEN", "garbage")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
    assert bot_main.main() == 2
    assert "BOT_TOKEN is invalid" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TelegramUnauthorizedError(GetMe(), "Unauthorized"), 2),
        (TelegramNetworkError(GetMe(), "down"), 1),
        (MigrationError("boom"), 1),
        (ConnectionError("no db"), 1),
    ],
)
def test_main_maps_startup_errors_to_exit_codes(valid_env, error, code):
    with patch.object(bot_main, "run", AsyncMock(side_effect=error)):
        assert bot_main.main() == code


def test_main_returns_0_on_clean_shutdown(valid_env):
    with patch.object(bot_main, "run", AsyncMock(return_value=None)):
        assert bot_main.main() == 0


def test_main_does_not_swallow_unexpected_errors(valid_env):
    with (
        patch.object(bot_main, "run", AsyncMock(side_effect=RuntimeError("bug"))),
        pytest.raises(RuntimeError),
    ):
        bot_main.main()


# ---------- run(): ordering and cleanup against a real database ----------


async def test_run_migrates_and_beats_before_polling_then_cleans_up(dsn, tmp_path):
    hb = tmp_path / "hb"
    seen: dict[str, object] = {}

    async def fake_polling(bot):
        conn = await asyncpg.connect(dsn)
        try:
            seen["migrated"] = await conn.fetchval("SELECT count(*) FROM schema_migrations")
        finally:
            await conn.close()
        await asyncio.sleep(0.05)  # let the heartbeat task run once
        seen["heartbeat"] = hb.exists()

    dp = MagicMock()
    dp.start_polling = fake_polling
    with patch.object(bot_main, "build_dispatcher", return_value=dp):
        await bot_main.run(settings_for(dsn, str(hb)))

    assert seen["migrated"] >= 1
    assert seen["heartbeat"] is True
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "heartbeat"]


async def test_run_cleans_up_when_polling_fails(dsn, tmp_path):
    dp = MagicMock()
    dp.start_polling = AsyncMock(side_effect=TelegramNetworkError(GetMe(), "down"))
    with (
        patch.object(bot_main, "build_dispatcher", return_value=dp),
        pytest.raises(TelegramNetworkError),
    ):
        await bot_main.run(settings_for(dsn, str(tmp_path / "hb")))
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "heartbeat"]


async def test_run_does_not_start_polling_when_migration_fails(dsn, tmp_path):
    dp = MagicMock()
    dp.start_polling = AsyncMock()
    with (
        patch.object(bot_main, "build_dispatcher", return_value=dp),
        patch.object(bot_main, "migrate", AsyncMock(side_effect=MigrationError("bad"))),
        pytest.raises(MigrationError),
    ):
        await bot_main.run(settings_for(dsn, str(tmp_path / "hb")))
    dp.start_polling.assert_not_called()

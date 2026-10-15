"""Handler wiring tests: feed real Updates through the Dispatcher with the Telegram API mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from bot.config import Settings
from bot.main import build_dispatcher

SETTINGS = Settings(bot_token="1:x", database_url="postgresql://u:p@h/d")


class FakePool:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    async def fetchval(self, query: str):
        self.calls.append(query)
        if self.fail:
            raise OSError("db down")
        return 1


def make_update(text: str) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=10, type="private"),
            from_user=User(id=10, is_bot=False, first_name="T"),
            text=text,
            entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
        ),
    )


async def feed(text: str, pool: FakePool) -> list[SendMessage]:
    """Dispatch one message; return every SendMessage the bot tried to send."""
    sent: list[SendMessage] = []

    async def fake_call(self, method, *args, **kwargs):
        sent.append(method)
        return Message(
            message_id=2, date=datetime.now(), chat=Chat(id=10, type="private"), text=method.text
        )

    bot = Bot("1:x")
    dp = build_dispatcher(pool, SETTINGS)
    with patch.object(Bot, "__call__", new=fake_call):
        await dp.feed_update(bot, make_update(text))
    await bot.session.close()
    return sent


async def test_start_replies_alive():
    sent = await feed("/start", FakePool())
    assert len(sent) == 1
    assert "running" in sent[0].text
    assert sent[0].chat_id == 10


async def test_ping_reports_database_ok():
    pool = FakePool()
    sent = await feed("/ping", pool)
    assert pool.calls == ["SELECT 1"]
    assert "database: ok" in sent[0].text


async def test_ping_reports_database_error_without_crashing():
    sent = await feed("/ping", FakePool(fail=True))
    assert "database: ERROR" in sent[0].text


async def test_unknown_command_gets_no_reply():
    assert await feed("/nope", FakePool()) == []


async def test_ping_against_real_pool(pool):
    sent = await feed("/ping", pool)
    assert "database: ok" in sent[0].text


@pytest.mark.parametrize("text", ["/start", "/ping"])
async def test_replies_never_leak_secrets(text):
    sent = await feed(text, FakePool())
    assert all("postgresql://" not in m.text and "1:x" not in m.text for m in sent)

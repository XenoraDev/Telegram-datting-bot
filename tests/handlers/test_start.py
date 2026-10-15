"""P0 health handlers still work behind the P1 middleware stack."""

from __future__ import annotations

from tests.handlers.harness import FakeTelegram


async def test_ping_reports_database_ok(app_pool):
    async with FakeTelegram(app_pool) as tg:
        await tg.onboard(1)
        out = await tg.send(1, "/ping")
    assert "database: ok" in out[0].text


async def test_ping_reports_database_error_without_crashing(app_pool):
    class BrokenPool:
        async def fetchval(self, *a):
            raise OSError("db down")

    from bot.handlers.start import ping

    sent = []

    class Msg:
        async def answer(self, text, **kw):
            sent.append(text)

    await ping(Msg(), BrokenPool())
    assert "database: ERROR" in sent[0]


async def test_help_lists_commands_and_chat_restrictions(app_pool):
    async with FakeTelegram(app_pool) as tg:
        await tg.onboard(1)
        out = await tg.send(1, "/help")
    assert "/deleteme" in out[0].text and "text and stickers" in out[0].text


async def test_replies_never_leak_secrets(app_pool):
    async with FakeTelegram(app_pool) as tg:
        await tg.onboard(1)
        for cmd in ("/start", "/ping", "/help", "/profile"):
            await tg.send(1, cmd)
        texts = tg.texts(1)
    assert all("postgresql://" not in t and "1:x" not in t for t in texts)

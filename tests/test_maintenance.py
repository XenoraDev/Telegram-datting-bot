"""Background housekeeping: queue expiry notices and report-context purge."""

from unittest.mock import AsyncMock, patch

from aiogram import Bot

from bot.main import run_maintenance
from bot.texts import en as t
from tests.factories import make_user


async def test_run_maintenance_expires_stale_searches_and_tells_the_user(app_pool, svc):
    await make_user(app_pool, 1)
    await svc.matchmaking.search(1)
    await app_pool.execute("UPDATE chat_queue SET joined_at = now() - interval '31 minutes'")
    send = AsyncMock()
    with patch.object(Bot, "send_message", send):
        await run_maintenance(svc, Bot("1:x"))
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 0
    assert send.await_args.args[0] == 1 and send.await_args.args[1] == t.SEARCH_EXPIRED


async def test_run_maintenance_purges_expired_report_contexts_and_prunes_memory(
    app_pool, svc, clock
):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    sid = await app_pool.fetchval(
        "INSERT INTO chat_sessions (user_a, user_b) VALUES (1, 2) RETURNING id"
    )
    await app_pool.execute(
        "INSERT INTO reports (reporter_id, reported_id, session_id, source, reason, context, "
        "context_expires_at) VALUES (1, 2, $1, 'chat', 'spam', '[{\"text\":\"x\"}]', "
        "now() - interval '1 day')",
        sid,
    )
    svc.buffer.add(77, 1, "old")
    clock.advance(10_000)
    await run_maintenance(svc, Bot("1:x"))
    assert await app_pool.fetchval("SELECT context FROM reports") is None
    assert len(svc.buffer) == 0


async def test_a_blocked_bot_does_not_break_maintenance(app_pool, svc):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    await make_user(app_pool, 1)
    await svc.matchmaking.search(1)
    await app_pool.execute("UPDATE chat_queue SET joined_at = now() - interval '31 minutes'")
    boom = AsyncMock(
        side_effect=TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "blocked")
    )
    with patch.object(Bot, "send_message", boom):
        await run_maintenance(svc, Bot("1:x"))  # must not raise
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 0

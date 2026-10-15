"""P0 health handlers. Real onboarding arrives in P1."""

from __future__ import annotations

import asyncpg
from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message


async def start(message: Message) -> None:
    await message.answer("✅ Bot is running. Profiles and chat are coming soon.")


async def ping(message: Message, pool: asyncpg.Pool) -> None:
    try:
        await pool.fetchval("SELECT 1")
    except Exception:  # health probe: report any failure instead of crashing the handler
        await message.answer("🏓 pong — database: ERROR")
        return
    await message.answer("🏓 pong — database: ok")


def build_router() -> Router:
    # A factory, not a module global: aiogram routers attach to one Dispatcher only, and
    # tests (and any future multi-dispatcher setup) need a fresh one each time.
    router = Router(name="start")
    router.message.register(start, CommandStart())
    router.message.register(ping, Command("ping"))
    return router

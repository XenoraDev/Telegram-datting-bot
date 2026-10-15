"""Per-user language preference (persisted in users.language, never inferred from Telegram)."""

from __future__ import annotations

import asyncpg

from ..db import repo_users
from ..texts import i18n


class LanguageService:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def set(self, uid: int, code: str) -> bool:
        """Save `code` for `uid`. False for unsupported codes or accounts that can't use the bot."""
        if code not in i18n.SUPPORTED:
            return False
        return await repo_users.set_language(self._pool, uid, code)

    async def get(self, uid: int) -> str:
        row = await repo_users.get(self._pool, uid)
        return i18n.normalize(row["language"] if row else None)

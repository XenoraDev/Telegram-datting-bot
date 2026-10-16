"""The profile owner's visibility switches (show age / gender / city to partners)."""

from __future__ import annotations

import asyncpg

from ..db import repo_profiles


class PrivacyService:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get(self, uid: int) -> dict[str, bool] | None:
        row = await repo_profiles.get_privacy(self._pool, uid)
        if row is None:
            return None
        return {"age": row["show_age"], "gender": row["show_gender"], "city": row["show_city"]}

    async def toggle(self, uid: int, field: str) -> dict[str, bool] | None:
        """Flip one switch and return all three, or None (unknown field / no profile)."""
        if field not in repo_profiles.PRIVACY_FIELDS:
            return None
        if await repo_profiles.toggle_privacy(self._pool, uid, field) is None:
            return None
        return await self.get(uid)

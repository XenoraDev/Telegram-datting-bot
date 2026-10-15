"""Random chat search / stop / next on top of the PostgreSQL queue + session model."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from .. import domain
from ..db import repo_chat
from ..db.repo_chat import Peer
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SearchResult:
    kind: str
    # matched | queued | already_queued | already_in_chat | not_eligible | cooldown | rate_limited
    session_id: int | None = None
    partner: Peer | None = None
    me: Peer | None = None
    cooldown_until: object | None = None


@dataclass(frozen=True, slots=True)
class StopResult:
    kind: str  # ended | left_queue | nothing
    session_id: int | None = None
    partner_id: int | None = None


class MatchmakingService:
    def __init__(self, pool: asyncpg.Pool, limiter: RateLimiter) -> None:
        self._pool = pool
        self._limiter = limiter

    async def search(self, uid: int) -> SearchResult:
        if not self._limiter.allow("chat_start", uid):
            return SearchResult("rate_limited")
        out = await repo_chat.find_or_queue(self._pool, uid)
        return SearchResult(out.kind, out.session_id, out.partner, out.me, out.cooldown_until)

    async def stop(self, uid: int, reason: str = "user_stop") -> StopResult:
        ended = await repo_chat.end_chat(self._pool, uid, reason)
        if ended:
            return StopResult("ended", ended[0], ended[1])
        if await repo_chat.leave_queue(self._pool, uid):
            return StopResult("left_queue")
        return StopResult("nothing")

    async def next(self, uid: int) -> tuple[StopResult, SearchResult]:
        stopped = await self.stop(uid, "user_next")
        return stopped, await self.search(uid)

    async def expire_stale_queue(self) -> list[int]:
        """Remove users who waited too long; returns their ids so they can be told."""
        return await repo_chat.pop_stale_queue(self._pool, domain.QUEUE_TIMEOUT_MINUTES)

    async def current_chat(self, uid: int) -> tuple[int, int] | None:
        """(session_id, partner_id) of the user's active chat, if any."""
        return await repo_chat.get_partner(self._pool, uid)

"""Random chat search / stop / next on top of the PostgreSQL queue + session model."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from .. import domain
from ..db import repo_browse, repo_chat
from ..db.repo_chat import Peer
from .entitlements import EntitlementService
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SearchResult:
    kind: str
    # matched | queued | already_queued | already_in_chat | not_eligible | cooldown | rate_limited
    # | limit_reached (today's random-chat allowance of the user's tier is used up)
    # and for 💬 Chat on a match: started | already | busy | partner_busy | unavailable
    session_id: int | None = None
    partner: Peer | None = None
    me: Peer | None = None
    cooldown_until: object | None = None
    limit: int | None = None  # the daily limit that was hit (limit_reached)


@dataclass(frozen=True, slots=True)
class StopResult:
    kind: str  # ended | left_queue | nothing
    session_id: int | None = None
    partner_id: int | None = None


class MatchmakingService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        limiter: RateLimiter,
        entitlements: EntitlementService,
        queue_timeout_minutes: int = domain.QUEUE_TIMEOUT_MINUTES,
    ) -> None:
        self._pool = pool
        self._limiter = limiter
        self._entitlements = entitlements
        self.queue_timeout_minutes = queue_timeout_minutes

    async def search(self, uid: int) -> SearchResult:
        """Start looking for a partner. Costs one of the tier's daily random-chat starts; a search
        that does not actually start one (not eligible, cooldown, already queued / in chat) is
        given back. Unlimited tiers still pass the per-second `chat_start` limiter."""
        if not self._limiter.allow("chat_start", uid):
            return SearchResult("rate_limited")
        used = await self._entitlements.consume(uid, "random_chat")
        if not used.allowed:
            return SearchResult("limit_reached", limit=used.limit)
        priority = (await self._entitlements.get(uid, "match_priority")).level
        try:
            out = await repo_chat.find_or_queue(self._pool, uid, priority)
        except BaseException:
            await self._entitlements.refund(uid, "random_chat")
            raise
        if out.kind not in ("matched", "queued"):
            await self._entitlements.refund(uid, "random_chat")
        return SearchResult(out.kind, out.session_id, out.partner, out.me, out.cooldown_until)

    async def chat_with_match(self, uid: int, public_id: str) -> SearchResult:
        """💬 Chat on a card in /matches: open (or find) the chat with that mutual match. It uses
        the same session tables and rules as random chat, but does not use up a random-chat start
        of the day and does not go through the queue. See repo_chat.start_match_chat."""
        handle = repo_browse.parse_public_id(public_id)
        if handle is None:
            return SearchResult("unavailable")
        if not self._limiter.allow("match_chat", uid):
            return SearchResult("rate_limited")
        out = await repo_chat.start_match_chat(self._pool, uid, handle)
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
        return await repo_chat.pop_stale_queue(self._pool, self.queue_timeout_minutes)

    async def current_chat(self, uid: int) -> tuple[int, int] | None:
        """(session_id, partner_id) of the user's active chat, if any."""
        return await repo_chat.get_partner(self._pool, uid)

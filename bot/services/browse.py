"""Browse profiles, like / pass, mutual matches. Safety (blocks, eligibility) is enforced in SQL."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from ..db import repo_browse
from ..db.repo_browse import Card
from .entitlements import EntitlementService
from .filters import FilterService
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)

MATCHES_PAGE = 10
ACTIONS = {"lk": "like", "ps": "pass"}


@dataclass(frozen=True, slots=True)
class NextCard:
    kind: str  # card | empty | limit_reached | rate_limited | not_eligible
    card: Card | None = None
    limit: int | None = None  # the daily limit that was hit (limit_reached)
    filtered: bool = False  # advanced filters were applied (explains an empty result)


@dataclass(frozen=True, slots=True)
class SwipeResult:
    # liked | passed | match | duplicate | unavailable | self | not_eligible
    # | limit_reached (today's likes used up) | rate_limited
    kind: str
    limit: int | None = None
    partner_id: int | None = None  # set for a new match: who has to be notified


class BrowseService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        limiter: RateLimiter,
        entitlements: EntitlementService,
        filters: FilterService,
    ) -> None:
        self._pool = pool
        self._limiter = limiter
        self._entitlements = entitlements
        self._filters = filters

    async def next_card(self, uid: int) -> NextCard:
        """The next profile to look at. Each shown profile costs one of the tier's daily browse
        views (given back if there was nothing to show); unlimited tiers are still rate limited."""
        if not await repo_browse.viewer_is_eligible(self._pool, uid):
            return NextCard("not_eligible")
        if not self._limiter.allow("browse_view", uid):
            return NextCard("rate_limited")
        used = await self._entitlements.consume(uid, "browse")
        if not used.allowed:
            return NextCard("limit_reached", limit=used.limit)
        active = await self._filters.effective(uid)  # re-checks the entitlement every time
        card = await repo_browse.next_candidate(self._pool, uid, active)
        if card is None:
            await self._entitlements.refund(uid, "browse")
            return NextCard("empty", filtered=active is not None)
        return NextCard("card", card, filtered=active is not None)

    async def swipe(self, uid: int, public_id: str, action: str) -> SwipeResult:
        """Record a like / pass on the profile behind `public_id`.

        A like costs one of the tier's daily likes (taken BEFORE the transaction: the usage counter
        needs its own connection, and holding one while asking for another can exhaust the pool);
        it is given back whenever no like ends up recorded. A pass is free.
        """
        if action not in ("like", "pass"):
            return SwipeResult("unavailable")
        if action == "like":
            if not self._limiter.allow("like", uid):
                return SwipeResult("rate_limited")
            used = await self._entitlements.consume(uid, "swipe")
            if not used.allowed:
                return SwipeResult("limit_reached", used.limit)
        try:
            res = await self._record(uid, public_id, action)
        except BaseException:
            if action == "like":
                await self._entitlements.refund(uid, "swipe")
            raise
        if action == "like" and res.kind not in ("liked", "match"):
            await self._entitlements.refund(uid, "swipe")
        return res

    async def _record(self, uid: int, public_id: str, action: str) -> SwipeResult:
        """One transaction under a per-pair advisory lock: the swipe row is unique per
        (swiper, target), and a mutual like creates exactly one canonical match row, even if both
        users press Like at the same instant."""
        pid = repo_browse.parse_public_id(public_id)
        if pid is None:
            return SwipeResult("unavailable")
        async with self._pool.acquire() as conn, conn.transaction():
            if await repo_browse.is_self(conn, uid, pid):
                return SwipeResult("self")
            target = await repo_browse.user_id_of(conn, pid)
            if target is None:
                return SwipeResult("unavailable")
            await repo_browse.lock_pair(conn, uid, target)
            if not await repo_browse.viewer_is_eligible(conn, uid):
                return SwipeResult("not_eligible")
            if await repo_browse.resolve_target(conn, uid, pid) != target:
                return SwipeResult("unavailable")  # not eligible any more, or blocked
            if not await repo_browse.insert_swipe(conn, uid, target, action):
                return SwipeResult("duplicate")
            if action == "pass":
                return SwipeResult("passed")
            if not await repo_browse.has_liked(conn, target, uid):
                return SwipeResult("liked")
            if not await repo_browse.create_match(conn, uid, target):
                return SwipeResult("liked")  # pair already matched: nothing new to announce
        log.info("new match created")
        return SwipeResult("match", partner_id=target)

    async def matches(self, uid: int) -> tuple[list[Card], int]:
        return await repo_browse.list_matches(self._pool, uid, MATCHES_PAGE)

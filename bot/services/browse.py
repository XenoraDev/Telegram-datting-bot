"""Browse profiles, like / pass, mutual matches. Safety (blocks, eligibility) is enforced in SQL."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from ..db import repo_browse
from ..db.repo_browse import Card

log = logging.getLogger(__name__)

MATCHES_PAGE = 10
ACTIONS = {"lk": "like", "ps": "pass"}


@dataclass(frozen=True, slots=True)
class SwipeResult:
    # liked | passed | match | duplicate | unavailable | self | not_eligible
    kind: str
    partner_id: int | None = None  # set for a new match: who has to be notified
    partner_card: Card | None = None
    my_card: Card | None = None


class BrowseService:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def next_card(self, uid: int) -> Card | None:
        if not await repo_browse.viewer_is_eligible(self._pool, uid):
            return None
        return await repo_browse.next_candidate(self._pool, uid)

    async def swipe(self, uid: int, public_id: str, action: str) -> SwipeResult:
        """Record a like / pass on the profile behind `public_id`.

        One transaction under a per-pair advisory lock: the swipe row is unique per
        (swiper, target), and a mutual like creates exactly one canonical match row, even if both
        users press Like at the same instant.
        """
        if action not in ("like", "pass"):
            return SwipeResult("unavailable")
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
            theirs = await repo_browse.card_of(conn, target)
            mine = await repo_browse.card_of(conn, uid)
        log.info("new match created")
        return SwipeResult("match", target, theirs, mine)

    async def matches(self, uid: int) -> tuple[list[Card], int]:
        return await repo_browse.list_matches(self._pool, uid, MATCHES_PAGE)

"""Profile boost (entitlement `profile_boost`: Pro / VIP).

A boost lifts the user's profile in browse (and random-chat pairing) for BOOST_MINUTES.
Rules, all enforced in PostgreSQL / here, never by the plan name:
  * the user must hold the `profile_boost` entitlement NOW (asked of the EntitlementService on
    every press; a stale button proves nothing);
  * one boost at a time: `profile_boosts` has one row per user;
  * after a boost starts the next one can start COOLDOWN_HOURS later (measured from the start, so
    at most two boosts a day); trying earlier is refused and tells when it will be possible;
  * a boost counts only while ends_at > now() (PostgreSQL clock): it expires by itself, no job;
  * boosting only re-orders eligible profiles; it never bypasses blocks, privacy, bans or limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from ..db import repo_boost
from .entitlements import EntitlementService
from .rate_limit import RateLimiter

BOOST_MINUTES = 60
COOLDOWN_HOURS = 12


@dataclass(frozen=True, slots=True)
class BoostStatus:
    kind: str  # denied | not_eligible | idle | active | cooldown
    ends_at: datetime | None = None  # active: when the boost ends
    next_at: datetime | None = None  # cooldown / active: when a new boost may start
    seconds_left: int = 0  # active: time until the boost ends


@dataclass(frozen=True, slots=True)
class BoostResult:
    kind: str  # activated | already_active | cooldown | denied | not_eligible | rate_limited
    ends_at: datetime | None = None
    next_at: datetime | None = None
    seconds_left: int = 0


def _status(row: repo_boost.BoostRow | None) -> BoostStatus:
    if row is None or not row.cooling_down:
        return BoostStatus("idle")
    if row.active:
        return BoostStatus("active", row.ends_at, row.cooldown_until, row.seconds_left)
    return BoostStatus("cooldown", None, row.cooldown_until)


class BoostService:
    def __init__(
        self, pool: asyncpg.Pool, limiter: RateLimiter, entitlements: EntitlementService
    ) -> None:
        self._pool = pool
        self._limiter = limiter
        self._entitlements = entitlements

    async def _entitled(self, uid: int) -> bool:
        return (await self._entitlements.get(uid, "profile_boost")).enabled

    async def status(self, uid: int) -> BoostStatus:
        """What the /boost screen shows. Also tells an entitled user whose profile is not
        eligible (e.g. banned) apart from a normal idle one."""
        if not await self._entitled(uid):
            return BoostStatus("denied")
        if not await repo_boost.viewer_is_eligible(self._pool, uid):
            return BoostStatus("not_eligible")
        return _status(await repo_boost.get(self._pool, uid))

    async def activate(self, uid: int) -> BoostResult:
        if not self._limiter.allow("boost", uid):
            return BoostResult("rate_limited")
        return await self._activate_unthrottled(uid)

    async def _activate_unthrottled(self, uid: int) -> BoostResult:
        """Everything except the burst limiter; the database alone guarantees one boost."""
        if not await self._entitled(uid):
            return BoostResult("denied")
        row = await repo_boost.activate(self._pool, uid, BOOST_MINUTES, COOLDOWN_HOURS)
        if row is not None:
            return BoostResult("activated", row.ends_at, row.cooldown_until, row.seconds_left)
        if not await repo_boost.viewer_is_eligible(self._pool, uid):
            return BoostResult("not_eligible")
        current = _status(await repo_boost.get(self._pool, uid))
        if current.kind == "active":
            return BoostResult(
                "already_active", current.ends_at, current.next_at, current.seconds_left
            )
        return BoostResult("cooldown", None, current.next_at)

    async def is_boosted(self, uid: int) -> bool:
        row = await repo_boost.get(self._pool, uid)
        return bool(row and row.active)

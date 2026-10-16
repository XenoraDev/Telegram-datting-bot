"""The ONE place that decides what a user may use and how often (P4).

    ent = await entitlements.get(uid, "swipe")        # Entitlement(enabled, daily_limit, source)
    if await entitlements.can_use(uid, "swipe"): ...  # read-only check
    res = await entitlements.consume(uid, "swipe")    # atomic check-and-count; res.allowed

Handlers never look at plans or payments; they ask this service. An entitlement value is a JSON
object {"enabled": bool, "daily_limit": int | null, "level": int} (missing / null limit =
unlimited; "level" ranks a feature such as match_priority / plan_badge, highest wins).

Resolution = the default "free" plan + plans of live subscriptions + live grants, merged
most-generous-wins: enabled is OR-ed, an unlimited source beats a numeric limit, otherwise the
highest limit wins. A feature nobody grants is disabled. Nothing here talks to a payment
provider; subscriptions and grants are plain rows another phase (or an admin) can write.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import asyncpg

from ..db import repo_entitlements as repo

log = logging.getLogger(__name__)

GRANT_SOURCES = ("subscription", "boost", "admin", "promo")
USAGE_RETENTION_DAYS = 35
MAX_LIMIT = 1_000_000
MAX_LEVEL = 100
_VALUE_KEYS = {"enabled", "daily_limit", "level"}


class EntitlementError(ValueError):
    """Invalid feature key / value / argument passed to the entitlement API."""


@dataclass(frozen=True, slots=True)
class Entitlement:
    feature: str
    enabled: bool
    daily_limit: int | None  # None = unlimited
    source: str  # none | free | subscription | grant (the most generous contributing source)
    level: int = 0  # highest "level" among the contributing sources (ranked features)


@dataclass(frozen=True, slots=True)
class ConsumeResult:
    allowed: bool
    used: int  # today's count after this call (unchanged when denied)
    remaining: int | None  # None = unlimited
    reason: str = "ok"  # ok | disabled | limit_reached | invalid_amount
    limit: int | None = None  # the daily limit that applied (None = unlimited)


def normalize_value(value: object) -> tuple[bool, int | None]:
    """(enabled, daily_limit) from a stored/granted JSON object; raises EntitlementError."""
    enabled, limit, _level = parse_value(value)
    return enabled, limit


def parse_value(value: object) -> tuple[bool, int | None, int]:
    """(enabled, daily_limit, level) from a stored/granted JSON object; raises EntitlementError."""
    if not isinstance(value, dict) or not set(value) <= _VALUE_KEYS:
        raise EntitlementError("value must be an object with 'enabled', 'daily_limit', 'level'")
    enabled = value.get("enabled", True)
    limit = value.get("daily_limit")
    level = value.get("level", 0)
    if not isinstance(enabled, bool):
        raise EntitlementError("'enabled' must be true or false")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_LIMIT
    ):
        raise EntitlementError("'daily_limit' must be null or an integer 0..1000000")
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= MAX_LEVEL:
        raise EntitlementError("'level' must be an integer 0..100")
    return enabled, limit, level


def _check_feature(feature: str) -> None:
    ok = (
        isinstance(feature, str)
        and 1 <= len(feature) <= 48
        and feature.isascii()
        and feature[0].isalpha()
        and feature.islower()
        and all(c.isalnum() or c == "_" for c in feature)
    )
    if not ok:
        raise EntitlementError("invalid feature key")


def merge(feature: str, rows: list[tuple[str, object]]) -> Entitlement:
    """Most-generous-wins merge of (source, json value) rows. Pure function."""
    best: Entitlement | None = None
    order = {"free": 0, "subscription": 1, "grant": 2}
    for source, raw in rows:
        try:
            enabled, limit, level = parse_value(json.loads(raw) if isinstance(raw, str) else raw)
        except (EntitlementError, ValueError):
            log.warning("ignoring a malformed entitlement value for %s", feature)
            continue
        if not enabled:
            continue
        if best is None:
            best = Entitlement(feature, True, limit, source, level)
            continue
        merged_limit = (
            None if best.daily_limit is None or limit is None else max(best.daily_limit, limit)
        )
        src = source if order[source] > order[best.source] else best.source
        best = Entitlement(feature, True, merged_limit, src, max(best.level, level))
    return best or Entitlement(feature, False, 0, "none")


class EntitlementService:
    def __init__(self, pool: asyncpg.Pool, clock: Callable[[], float] = time.time) -> None:
        self._pool = pool
        self._clock = clock

    def _today(self) -> date:
        return datetime.fromtimestamp(self._clock(), UTC).date()

    # ---------------------------------------------------------------- reading

    async def get(self, uid: int, feature: str) -> Entitlement:
        _check_feature(feature)
        rows = await repo.effective_rows(self._pool, uid, feature)
        return merge(feature, [(r["src"], r["value"]) for r in rows])

    async def used_today(self, uid: int, feature: str) -> int:
        _check_feature(feature)
        return await repo.usage(self._pool, uid, feature, self._today())

    async def can_use(self, uid: int, feature: str) -> bool:
        """Read-only: enabled and today's usage is below the limit. Use consume() to act."""
        ent = await self.get(uid, feature)
        if not ent.enabled:
            return False
        return ent.daily_limit is None or await self.used_today(uid, feature) < ent.daily_limit

    # ---------------------------------------------------------------- consuming

    async def consume(self, uid: int, feature: str, amount: int = 1) -> ConsumeResult:
        """Atomically count `amount` uses today if the entitlement allows it.

        The limit check and the increment are ONE SQL statement, so any number of concurrent
        calls can never push the counter past the limit.
        """
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            return ConsumeResult(False, 0, None, "invalid_amount")
        ent = await self.get(uid, feature)
        used = await repo.usage(self._pool, uid, feature, self._today())
        if not ent.enabled:
            return ConsumeResult(False, used, 0, "disabled")
        limit = ent.daily_limit
        if limit is not None and amount > limit:
            return ConsumeResult(False, used, max(limit - used, 0), "limit_reached", limit)
        new = await repo.add_usage(self._pool, uid, feature, self._today(), amount, limit)
        if new is None:
            used = await repo.usage(self._pool, uid, feature, self._today())
            return ConsumeResult(False, used, max((limit or 0) - used, 0), "limit_reached", limit)
        return ConsumeResult(True, new, None if limit is None else limit - new, "ok", limit)

    async def refund(self, uid: int, feature: str, amount: int = 1) -> None:
        """Give back a use that turned out not to happen (never below zero)."""
        _check_feature(feature)
        if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 1:
            await repo.subtract_usage(self._pool, uid, feature, self._today(), amount)

    async def get_many(self, uid: int, features: list[str]) -> dict[str, Entitlement]:
        """Several features in ONE query (partner cards need four at once)."""
        for feature in features:
            _check_feature(feature)
        rows = await repo.effective_rows_many(self._pool, uid, features)
        per: dict[str, list[tuple[str, object]]] = {f: [] for f in features}
        for r in rows:
            per[r["feature_key"]].append((r["src"], r["value"]))
        return {f: merge(f, per[f]) for f in features}

    # ---------------------------------------------------------------- grants / subscriptions
    # Plumbing for later phases (admin tools, payments). Nothing in the bot calls these yet.

    async def grant(
        self,
        uid: int,
        feature: str,
        value: dict,
        *,
        source: str = "admin",
        expires_at: datetime | None = None,
    ) -> int:
        _check_feature(feature)
        normalize_value(value)
        if source not in GRANT_SOURCES:
            raise EntitlementError("unknown grant source")
        return await repo.insert_grant(self._pool, uid, feature, value, source, expires_at)

    async def revoke_grant(self, grant_id: int) -> bool:
        return await repo.revoke_grant(self._pool, grant_id)

    async def start_subscription(
        self,
        uid: int,
        plan_code: str,
        *,
        period_end: datetime | None = None,
        provider: str | None = None,
        provider_ref: str | None = None,
    ) -> int:
        plan = await repo.plan_id(self._pool, plan_code)
        if plan is None:
            raise EntitlementError("unknown or inactive plan")
        return await repo.insert_subscription(
            self._pool, uid, plan, period_end, provider, provider_ref
        )

    async def set_subscription_status(self, sub_id: int, status: str) -> bool:
        if status not in ("active", "past_due", "canceled", "expired"):
            raise EntitlementError("unknown subscription status")
        return await repo.set_subscription_status(self._pool, sub_id, status)

    # ---------------------------------------------------------------- maintenance

    async def purge_old_usage(self) -> int:
        """Counters older than ~5 weeks serve no purpose: drop them."""
        cutoff = self._today() - timedelta(days=USAGE_RETENTION_DAYS)
        return await repo.purge_usage_before(self._pool, cutoff)

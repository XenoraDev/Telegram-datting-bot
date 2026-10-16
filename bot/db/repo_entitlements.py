"""plans, entitlements, subscriptions, grants and usage counters (SQL only, all parameterised)."""

from __future__ import annotations

import json
from datetime import date, datetime

import asyncpg

from .repo_users import Db

FREE_PLAN = "free"


async def effective_rows(db: Db, uid: int, feature: str) -> list[asyncpg.Record]:
    """Every entitlement value that currently applies to (user, feature), with its source:
    the free plan, plans of live subscriptions, and live grants. Merging happens in the service."""
    return await db.fetch(
        """
        SELECT 'free' AS src, pe.value::text AS value
        FROM plans p JOIN plan_entitlements pe ON pe.plan_id = p.id
        WHERE p.code = $3 AND p.is_active AND pe.feature_key = $2
        UNION ALL
        SELECT 'subscription', pe.value::text
        FROM subscriptions s
        JOIN plans p ON p.id = s.plan_id AND p.is_active
        JOIN plan_entitlements pe ON pe.plan_id = p.id AND pe.feature_key = $2
        WHERE s.user_id = $1 AND s.status = 'active' AND s.started_at <= now()
          AND (s.current_period_end IS NULL OR s.current_period_end > now())
        UNION ALL
        SELECT 'grant', g.value::text
        FROM entitlement_grants g
        WHERE g.user_id = $1 AND g.feature_key = $2 AND g.revoked_at IS NULL
          AND (g.expires_at IS NULL OR g.expires_at > now())
        """,
        uid,
        feature,
        FREE_PLAN,
    )


async def effective_rows_many(db: Db, uid: int, features: list[str]) -> list[asyncpg.Record]:
    """effective_rows() for several features at once: (feature_key, src, value) rows."""
    return await db.fetch(
        """
        SELECT pe.feature_key, 'free' AS src, pe.value::text AS value
        FROM plans p JOIN plan_entitlements pe ON pe.plan_id = p.id
        WHERE p.code = $3 AND p.is_active AND pe.feature_key = ANY($2::text[])
        UNION ALL
        SELECT pe.feature_key, 'subscription', pe.value::text
        FROM subscriptions s
        JOIN plans p ON p.id = s.plan_id AND p.is_active
        JOIN plan_entitlements pe ON pe.plan_id = p.id AND pe.feature_key = ANY($2::text[])
        WHERE s.user_id = $1 AND s.status = 'active' AND s.started_at <= now()
          AND (s.current_period_end IS NULL OR s.current_period_end > now())
        UNION ALL
        SELECT g.feature_key, 'grant', g.value::text
        FROM entitlement_grants g
        WHERE g.user_id = $1 AND g.feature_key = ANY($2::text[]) AND g.revoked_at IS NULL
          AND (g.expires_at IS NULL OR g.expires_at > now())
        """,
        uid,
        features,
        FREE_PLAN,
    )


async def subtract_usage(db: Db, uid: int, feature: str, day: date, amount: int) -> None:
    await db.execute(
        "UPDATE usage_counters SET count = greatest(count - $4, 0) "
        "WHERE user_id = $1 AND feature_key = $2 AND period_start = $3",
        uid,
        feature,
        day,
        amount,
    )


async def usage(db: Db, uid: int, feature: str, day: date) -> int:
    return (
        await db.fetchval(
            "SELECT count FROM usage_counters "
            "WHERE user_id = $1 AND feature_key = $2 AND period_start = $3",
            uid,
            feature,
            day,
        )
        or 0
    )


async def add_usage(
    db: Db, uid: int, feature: str, day: date, amount: int, limit: int | None
) -> int | None:
    """Atomically add `amount` to today's counter. Returns the new count, or None when that
    would exceed `limit` (None = unlimited). One statement, so concurrent callers can't both
    slip under the limit: the row lock taken by ON CONFLICT DO UPDATE serialises them."""
    return await db.fetchval(
        """
        INSERT INTO usage_counters (user_id, feature_key, period_start, count)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, feature_key, period_start) DO UPDATE
            SET count = usage_counters.count + EXCLUDED.count
            WHERE $5::int IS NULL OR usage_counters.count + EXCLUDED.count <= $5::int
        RETURNING count
        """,
        uid,
        feature,
        day,
        amount,
        limit,
    )


async def purge_usage_before(db: Db, day: date) -> int:
    status = await db.execute("DELETE FROM usage_counters WHERE period_start < $1", day)
    return int(status.split()[-1])


async def delete_usage_for(db: Db, uid: int) -> None:
    await db.execute("DELETE FROM usage_counters WHERE user_id = $1", uid)


async def insert_grant(
    db: Db, uid: int, feature: str, value: dict, source: str, expires_at: datetime | None
) -> int:
    return await db.fetchval(
        "INSERT INTO entitlement_grants (user_id, feature_key, value, source, expires_at) "
        "VALUES ($1, $2, $3::jsonb, $4, $5) RETURNING id",
        uid,
        feature,
        json.dumps(value),
        source,
        expires_at,
    )


async def revoke_grant(db: Db, grant_id: int) -> bool:
    return bool(
        await db.fetchval(
            "UPDATE entitlement_grants SET revoked_at = now() "
            "WHERE id = $1 AND revoked_at IS NULL RETURNING 1",
            grant_id,
        )
    )


async def plan_id(db: Db, code: str) -> int | None:
    return await db.fetchval("SELECT id FROM plans WHERE code = $1 AND is_active", code)


async def insert_subscription(
    db: Db,
    uid: int,
    plan: int,
    period_end: datetime | None,
    provider: str | None,
    provider_ref: str | None,
) -> int:
    return await db.fetchval(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end, provider, provider_ref) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        uid,
        plan,
        period_end,
        provider,
        provider_ref,
    )


async def set_subscription_status(db: Db, sub_id: int, status: str) -> bool:
    return bool(
        await db.fetchval(
            "UPDATE subscriptions SET status = $2 WHERE id = $1 RETURNING 1", sub_id, status
        )
    )

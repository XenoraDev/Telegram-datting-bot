"""Admin-side subscription changes (S2): cancel one subscription, revoke a payment's subscription.

Nothing here deletes a row. A subscription is only ever moved from `active` to `canceled`; its
dates, plan and provider data stay exactly as they were, and approved payment rows are never
touched (they are frozen by a trigger anyway).
"""

from __future__ import annotations

import asyncpg

from .repo_users import Db

# running = started and not over; queued = starts later. Decided by PostgreSQL's clock.
_PHASE = """CASE WHEN s.status = 'active' AND s.started_at <= now()
                     AND (s.current_period_end IS NULL OR s.current_period_end > now())
                    THEN 'running'
                WHEN s.status = 'active' AND s.started_at > now() THEN 'queued'
                ELSE 'ended' END"""

_COLUMNS = f"""
    s.id, s.user_id, s.status, s.started_at, s.current_period_end, s.provider, s.provider_ref,
    pl.name AS plan_name,
    coalesce((SELECT max((pe.value ->> 'level')::int) FROM plan_entitlements pe
               WHERE pe.plan_id = s.plan_id AND pe.feature_key = 'plan_badge'), 0) AS level,
    {_PHASE} AS phase
"""  # noqa: S608 - constant fragments


async def get_subscription(db: Db, sub_id: int, *, lock: bool = False) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"SELECT {_COLUMNS} FROM subscriptions s JOIN plans pl ON pl.id = s.plan_id "  # noqa: S608
        "WHERE s.id = $1" + (" FOR UPDATE OF s" if lock else ""),
        sub_id,
    )


async def cancel(db: Db, sub_id: int, user_id: int) -> bool:
    """active -> canceled, only for the right owner and only while it is running or queued.
    False when it was already ended / canceled (so a double submit changes nothing twice)."""
    return bool(
        await db.fetchval(
            "UPDATE subscriptions SET status = 'canceled' "
            "WHERE id = $1 AND user_id = $2 AND status = 'active' "
            "AND (current_period_end IS NULL OR current_period_end > now()) RETURNING 1",
            sub_id,
            user_id,
        )
    )


async def live_others(db: Db, user_id: int, sub_id: int) -> int:
    """How many OTHER running / queued subscriptions the user has (shown before cancelling)."""
    return await db.fetchval(
        "SELECT count(*) FROM subscriptions WHERE user_id = $1 AND id <> $2 AND status = 'active' "
        "AND (current_period_end IS NULL OR current_period_end > now())",
        user_id,
        sub_id,
    )


async def approved_payments_pointing_at(db: Db, sub_id: int) -> int:
    """Approved payments whose subscription is this one (more than one = it was extended)."""
    return await db.fetchval(
        "SELECT count(*) FROM payments WHERE subscription_id = $1 AND status = 'approved'", sub_id
    )


async def payment_with_plan(db: Db, payment_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        "SELECT p.id, p.reference, p.user_id, p.status, p.subscription_id, pl.name AS plan_name "
        "FROM payments p JOIN plans pl ON pl.id = p.plan_id WHERE p.id = $1",
        payment_id,
    )


async def revoke_audit(db: Db, payment_id: int) -> asyncpg.Record | None:
    """The audit entry of an earlier revoke of this payment's approval, if any."""
    return await db.fetchrow(
        "SELECT admin_id, created_at FROM admin_actions "
        "WHERE action = 'sub_revoke' AND payment_id = $1 ORDER BY id DESC LIMIT 1",
        payment_id,
    )


async def audit(
    db: Db,
    admin_id: int,
    action: str,
    *,
    user_id: int,
    subscription_id: int,
    payment_id: int | None,
    reason: str,
    detail: str,
) -> None:
    await db.execute(
        "INSERT INTO admin_actions (admin_id, action, target_user_id, subscription_id, "
        "payment_id, reason, detail) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        admin_id,
        action,
        user_id,
        subscription_id,
        payment_id,
        reason,
        detail[:200],
    )

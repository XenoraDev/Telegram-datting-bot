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


# ------------------------------------------------------------------ S3: shorten / grant


async def lock_user(db: Db, uid: int) -> str | None:
    """Row-lock the account (and return its status). Approvals share-lock the same row, so an
    admin change and an approval for one user can never run at the same moment."""
    return await db.fetchval("SELECT status FROM users WHERE id = $1 FOR UPDATE", uid)


async def lock_live(db: Db, uid: int, *, lock: bool = True) -> list[asyncpg.Record]:
    """Every running or queued subscription of the user (whatever its plan's state is), locked
    (unless lock=False, for a read-only preview).
    Same order as the approval path (end DESC, id DESC), so the two can not deadlock."""
    return await db.fetch(
        """
        SELECT s.id, s.plan_id, s.started_at, s.current_period_end, s.provider,
               pl.price_minor, pl.duration_days, pl.name AS plan_name,
               coalesce((SELECT max((pe.value ->> 'level')::int) FROM plan_entitlements pe
                          WHERE pe.plan_id = s.plan_id AND pe.feature_key = 'plan_badge'), 0)
                   AS level
        FROM subscriptions s JOIN plans pl ON pl.id = s.plan_id
        WHERE s.user_id = $1 AND s.status = 'active'
          AND (s.current_period_end IS NULL OR s.current_period_end > now())
        ORDER BY s.current_period_end DESC NULLS FIRST, s.id DESC
        """  # noqa: S608 - the only variable part is a constant locking clause
        + ("FOR UPDATE OF s" if lock else ""),
        uid,
    )


async def db_now(db: Db):
    return await db.fetchval("SELECT now()")


async def minus_days(db: Db, ts, days: int):
    """`ts` minus whole days with the SAME interval arithmetic the extension used to add them."""
    return await db.fetchval("SELECT $1::timestamptz - make_interval(days => $2)", ts, days)


async def set_end_minus(db: Db, sub_id: int, days: int):
    return await db.fetchval(
        "UPDATE subscriptions "
        "SET current_period_end = current_period_end - make_interval(days => $2) "
        "WHERE id = $1 AND status = 'active' RETURNING current_period_end",
        sub_id,
        days,
    )


async def shift_back(db: Db, ids: list[int], days: int) -> None:
    """Queued subscriptions behind a shortened one move earlier by the same days."""
    await db.execute(
        "UPDATE subscriptions SET started_at = started_at - make_interval(days => $2), "
        "current_period_end = current_period_end - make_interval(days => $2) "
        "WHERE id = ANY($1::bigint[]) AND status = 'active'",
        ids,
        days,
    )


async def removal_audit(db: Db, payment_id: int) -> asyncpg.Record | None:
    """The audit entry of an earlier removal of this payment's extension, if any."""
    return await db.fetchrow(
        "SELECT admin_id, created_at FROM admin_actions "
        "WHERE action = 'sub_shorten' AND payment_id = $1 ORDER BY id DESC LIMIT 1",
        payment_id,
    )


async def changed_by_admin(db: Db, sub_id: int) -> bool:
    """Has an admin already granted time to, or taken time off, this subscription? Then what a
    single payment contributed is no longer provable."""
    return bool(
        await db.fetchval(
            "SELECT 1 FROM admin_actions WHERE subscription_id = $1 "
            "AND action IN ('sub_grant', 'sub_shorten') LIMIT 1",
            sub_id,
        )
    )


async def insert_granted(
    db: Db, uid: int, plan_id: int, days: int, reference: str, start=None, end_base=None
):
    """A subscription given by an admin: provider 'admin_grant', no payment row."""
    return await db.fetchrow(
        "INSERT INTO subscriptions (user_id, plan_id, started_at, current_period_end, provider, "
        "provider_ref) VALUES ($1, $2, coalesce($5::timestamptz, now()), "
        "coalesce($6::timestamptz, now()) + make_interval(days => $3), 'admin_grant', $4) "
        "RETURNING id, started_at, current_period_end",
        uid,
        plan_id,
        days,
        reference,
        start,
        end_base,
    )


async def user_payment_links(db: Db, uid: int) -> list[asyncpg.Record]:
    """Approved payments behind each of the user's subscriptions, for the admin page. Only ids,
    references and the days each one added: nothing about amount, UTR or screenshot."""
    return await db.fetch(
        "SELECT p.id, p.reference, p.subscription_id, p.duration_days, "
        "(p.reference = s.provider_ref) AS created_it, "
        "EXISTS (SELECT 1 FROM admin_actions a WHERE a.action = 'sub_shorten' "
        "        AND a.payment_id = p.id) AS removed "
        "FROM payments p JOIN subscriptions s ON s.id = p.subscription_id "
        "WHERE p.user_id = $1 AND s.user_id = $1 AND p.status = 'approved' "
        "ORDER BY p.id",
        uid,
    )

"""SQL for manual UPI payments (P6): settings, purchasable plans, orders, evidence.

Everything is parameterised. Status changes always go through `... WHERE status = <expected>`
inside the caller's transaction, and the database trigger `payments_guard` refuses any illegal
move as a second line of defence.
"""

from __future__ import annotations

import hashlib
import json

import asyncpg

from . import repo_tiers
from .repo_users import Db

PLAN_COLUMNS = "id, code, name, duration_days, price_minor, currency, is_active, tier"
PAYMENT_COLUMNS = (
    "p.id, p.reference, p.user_id, p.plan_id, pl.name AS plan_name, p.amount_minor, p.currency, "
    "p.duration_days, p.pay_to, p.status, p.utr, p.submitted_at, p.reviewed_by, p.reviewed_at, "
    "p.rejection_reason, p.subscription_id, p.created_at, p.approval_mode"
)


# ------------------------------------------------------------------ settings
async def get_settings(db: Db) -> asyncpg.Record:
    return await db.fetchrow(
        "SELECT upi_id, payee_name, instructions, is_active, updated_by, updated_at, "
        "auto_approve, auto_approve_changed_by, auto_approve_changed_at FROM payment_settings"
    )


async def set_auto_approve(db: Db, on: bool, by: int) -> None:
    await db.execute(
        "UPDATE payment_settings SET auto_approve = $1, auto_approve_changed_by = $2, "
        "auto_approve_changed_at = now()",
        on,
        by,
    )


async def save_settings(
    db: Db, upi_id: str | None, payee: str | None, instructions: str | None, active: bool, by: int
) -> None:
    await db.execute(
        "UPDATE payment_settings SET upi_id = $1, payee_name = $2, instructions = $3, "
        "is_active = $4, updated_by = $5, updated_at = now()",
        upi_id,
        payee,
        instructions,
        active,
        by,
    )


# ------------------------------------------------------------------ plans
async def purchasable_plans(db: Db) -> list[asyncpg.Record]:
    return await db.fetch(
        f"SELECT {PLAN_COLUMNS} FROM plans "  # noqa: S608 - constant column list
        "WHERE is_active AND price_minor IS NOT NULL AND currency = 'INR' "
        "ORDER BY price_minor, id"
    )


async def plan_feature_rows(db: Db, plan_ids: list[int]) -> list[asyncpg.Record]:
    """Entitlement rows of the given plans plus the Free baseline (plan_id NULL), for the
    benefit lines on /premium: (plan_id, feature_key, value as JSON text)."""
    return await db.fetch(
        "SELECT CASE WHEN p.code = 'free' THEN NULL ELSE p.id END AS plan_id, "
        "pe.feature_key, pe.value::text AS value "
        "FROM plans p JOIN plan_entitlements pe ON pe.plan_id = p.id "
        "WHERE (p.code = 'free' AND p.is_active) OR p.id = ANY($1::bigint[])",
        plan_ids,
    )


async def get_plan(db: Db, plan_id: int, *, lock: bool = False) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"SELECT {PLAN_COLUMNS} FROM plans WHERE id = $1" + (" FOR SHARE" if lock else ""),  # noqa: S608
        plan_id,
    )


async def all_paid_plans(db: Db) -> list[asyncpg.Record]:
    """Every plan that has (or is meant to have) a price: for the admin Plans page."""
    return await db.fetch(
        f"SELECT {PLAN_COLUMNS} FROM plans WHERE code <> 'free' ORDER BY id"  # noqa: S608
    )


async def apply_tier(db: Db, plan_id: int, tier: str) -> None:
    """Make the plan's entitlements exactly the tier's template: bot/tiers.py with the admin's
    changes from the Tiers page on top (a feature switched off is simply not written)."""
    await db.execute("DELETE FROM plan_entitlements WHERE plan_id = $1", plan_id)
    for feature, value in (await repo_tiers.effective_template(db, tier)).items():
        if value.get("enabled") is False:
            continue
        await db.execute(
            "INSERT INTO plan_entitlements (plan_id, feature_key, value) "
            "VALUES ($1, $2, $3::jsonb)",
            plan_id,
            feature,
            json.dumps(value),
        )
    await db.execute("UPDATE plans SET tier = $2 WHERE id = $1", plan_id, tier)


TIER_PLAN_NAMES = {"premium": "Premium", "pro": "Pro", "vip": "VIP"}


async def lock_plan_defaults(conn: asyncpg.Connection) -> None:
    """Serialise concurrent setups (bot start-up vs. the admin button), transaction-scoped."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('plan-defaults', 0))")


async def plan_by_code(db: Db, code: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"SELECT {PLAN_COLUMNS} FROM plans WHERE code = $1 FOR UPDATE",  # noqa: S608
        code,
    )


async def entitlement_count(db: Db, plan_id: int) -> int:
    return await db.fetchval("SELECT count(*) FROM plan_entitlements WHERE plan_id = $1", plan_id)


async def insert_default_plan(db: Db, code: str, name: str, tier: str) -> int:
    """A tier plan that is ready but NOT for sale: inactive, no price, no length (the admin sets
    those; see payments.ensure_default_plans)."""
    plan_id = await db.fetchval(
        "INSERT INTO plans (code, name, billing_interval, is_active) "
        "VALUES ($1, $2, 'none', false) RETURNING id",
        code,
        name,
    )
    await apply_tier(db, plan_id, tier)
    return plan_id


async def add_missing_entitlements(db: Db, plan_id: int, tier: str) -> int:
    """Insert the tier's feature keys the plan lacks. Existing values are never changed."""
    added = 0
    for feature, value in (await repo_tiers.effective_template(db, tier)).items():
        if value.get("enabled") is False:
            continue
        inserted = await db.fetchval(
            "INSERT INTO plan_entitlements (plan_id, feature_key, value) "
            "VALUES ($1, $2, $3::jsonb) ON CONFLICT (plan_id, feature_key) DO NOTHING RETURNING 1",
            plan_id,
            feature,
            json.dumps(value),
        )
        added += 1 if inserted else 0
    return added


async def insert_plan(
    db: Db, code: str, name: str, days: int, price_minor: int, active: bool, tier: str
) -> int:
    plan_id = await db.fetchval(
        "INSERT INTO plans (code, name, billing_interval, is_active, duration_days, price_minor) "
        "VALUES ($1, $2, 'none', $3, $4, $5) RETURNING id",
        code,
        name,
        active,
        days,
        price_minor,
    )
    await apply_tier(db, plan_id, tier)
    return plan_id


async def update_plan(
    db: Db, plan_id: int, name: str, days: int, price_minor: int, active: bool
) -> bool:
    return bool(
        await db.fetchval(
            "UPDATE plans SET name = $2, duration_days = $3, price_minor = $4, is_active = $5 "
            "WHERE id = $1 AND code <> 'free' RETURNING 1",
            plan_id,
            name,
            days,
            price_minor,
            active,
        )
    )


# ------------------------------------------------------------------ orders
async def open_payment(db: Db, uid: int, *, lock: bool = False) -> asyncpg.Record | None:
    """The user's single open order (created or pending), if any."""
    return await db.fetchrow(
        f"SELECT {PAYMENT_COLUMNS} FROM payments p JOIN plans pl ON pl.id = p.plan_id "  # noqa: S608
        "WHERE p.user_id = $1 AND p.status IN ('created', 'pending')"
        + (" FOR UPDATE OF p" if lock else ""),
        uid,
    )


async def cancel_created(db: Db, uid: int) -> int:
    status = await db.execute(
        "UPDATE payments SET status = 'cancelled' WHERE user_id = $1 AND status = 'created'", uid
    )
    return int(status.split()[-1])


async def insert_payment(
    db: Db, reference: str, uid: int, plan: asyncpg.Record, pay_to: str
) -> int:
    return await db.fetchval(
        "INSERT INTO payments (reference, user_id, plan_id, amount_minor, currency, "
        "duration_days, pay_to) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        reference,
        uid,
        plan["id"],
        plan["price_minor"],
        plan["currency"],
        plan["duration_days"],
        pay_to,
    )


async def get_payment(db: Db, payment_id: int, *, lock: bool = False) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"SELECT {PAYMENT_COLUMNS} FROM payments p JOIN plans pl ON pl.id = p.plan_id "  # noqa: S608
        "WHERE p.id = $1" + (" FOR UPDATE OF p" if lock else ""),
        payment_id,
    )


async def latest_for_user(db: Db, uid: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"SELECT {PAYMENT_COLUMNS} FROM payments p JOIN plans pl ON pl.id = p.plan_id "  # noqa: S608
        "WHERE p.user_id = $1 AND p.status <> 'cancelled' ORDER BY p.id DESC LIMIT 1",
        uid,
    )


async def list_for_user(db: Db, uid: int, limit: int = 10) -> list[asyncpg.Record]:
    """The user's own submitted payments, newest first. Orders never submitted (created) and
    cancelled ones are not history. The user id is part of the WHERE: nobody else's rows."""
    return await db.fetch(
        f"SELECT {PAYMENT_COLUMNS} FROM payments p JOIN plans pl ON pl.id = p.plan_id "  # noqa: S608
        "WHERE p.user_id = $1 AND p.status IN ('pending', 'approved', 'rejected') "
        "ORDER BY p.id DESC LIMIT $2",
        uid,
        limit,
    )


async def set_status(db: Db, payment_id: int, expected: str, new: str) -> bool:
    return bool(
        await db.fetchval(
            "UPDATE payments SET status = $3 WHERE id = $1 AND status = $2 RETURNING 1",
            payment_id,
            expected,
            new,
        )
    )


async def submit(db: Db, payment_id: int, utr: str | None) -> bool:
    return bool(
        await db.fetchval(
            "UPDATE payments SET status = 'pending', utr = $2, submitted_at = now() "
            "WHERE id = $1 AND status = 'created' RETURNING 1",
            payment_id,
            utr,
        )
    )


async def set_utr_if_missing(db: Db, payment_id: int, utr: str) -> bool:
    """Add the UTR of a payment that is waiting for review and has none yet."""
    return bool(
        await db.fetchval(
            "UPDATE payments SET utr = $2 WHERE id = $1 AND status = 'pending' "
            "AND utr IS NULL RETURNING 1",
            payment_id,
            utr,
        )
    )


async def utr_in_use(db: Db, utr: str, except_payment: int) -> bool:
    """Another payment that is waiting for review or already approved carries this UTR."""
    return bool(
        await db.fetchval(
            "SELECT 1 FROM payments WHERE utr = $1 AND id <> $2 "
            "AND status IN ('pending', 'approved') LIMIT 1",
            utr,
            except_payment,
        )
    )


async def approve(
    db: Db, payment_id: int, admin_id: int, subscription_id: int, mode: str = "manual"
) -> bool:
    return bool(
        await db.fetchval(
            "UPDATE payments SET status = 'approved', reviewed_by = $2, reviewed_at = now(), "
            "subscription_id = $3, approval_mode = $4 "
            "WHERE id = $1 AND status = 'pending' RETURNING 1",
            payment_id,
            admin_id,
            subscription_id,
            mode,
        )
    )


async def reject(db: Db, payment_id: int, admin_id: int, reason: str) -> bool:
    return bool(
        await db.fetchval(
            "UPDATE payments SET status = 'rejected', reviewed_by = $2, reviewed_at = now(), "
            "rejection_reason = $3 WHERE id = $1 AND status = 'pending' RETURNING 1",
            payment_id,
            admin_id,
            reason,
        )
    )


async def stale_created(db: Db, hours: int) -> int:
    status = await db.execute(
        "UPDATE payments SET status = 'cancelled' "
        "WHERE status = 'created' AND created_at < now() - make_interval(hours => $1)",
        hours,
    )
    return int(status.split()[-1])


# ------------------------------------------------------------------ subscriptions (P4 tables)
async def live_subscriptions_for_update(db: Db, uid: int) -> list[asyncpg.Record]:
    """All of the user's subscriptions that are running or queued (and whose plan is on), with the
    plan's tier level, row-locked so concurrent approvals for one user serialise."""
    return await db.fetch(
        """
        SELECT s.id, s.plan_id, s.started_at, s.current_period_end,
               coalesce((SELECT max((pe.value ->> 'level')::int) FROM plan_entitlements pe
                          WHERE pe.plan_id = s.plan_id AND pe.feature_key = 'plan_badge'), 0)
                   AS level
        FROM subscriptions s
        JOIN plans pl ON pl.id = s.plan_id AND pl.is_active
        WHERE s.user_id = $1 AND s.status = 'active' AND s.current_period_end > now()
        ORDER BY s.current_period_end DESC, s.id DESC
        FOR UPDATE OF s
        """,
        uid,
    )


async def plan_level(db: Db, plan_id: int) -> int:
    return (
        await db.fetchval(
            "SELECT max((value ->> 'level')::int) FROM plan_entitlements "
            "WHERE plan_id = $1 AND feature_key = 'plan_badge'",
            plan_id,
        )
        or 0
    )


async def db_now(db: Db):
    return await db.fetchval("SELECT now()")


async def extend_subscription(db: Db, sub_id: int, days: int):
    return await db.fetchval(
        "UPDATE subscriptions SET "
        "current_period_end = current_period_end + make_interval(days => $2) "
        "WHERE id = $1 RETURNING current_period_end",
        sub_id,
        days,
    )


async def shift_subscriptions(db: Db, ids: list[int], days: int) -> None:
    """Push queued subscriptions back (the one in front of them was extended)."""
    await db.execute(
        "UPDATE subscriptions SET started_at = started_at + make_interval(days => $2), "
        "current_period_end = current_period_end + make_interval(days => $2) "
        "WHERE id = ANY($1::bigint[])",
        ids,
        days,
    )


async def end_subscriptions_now(db: Db, ids: list[int]) -> None:
    """Upgrade: the old subscriptions stop now (their time is carried into the new one)."""
    await db.execute(
        "UPDATE subscriptions SET status = 'canceled', "
        "current_period_end = least(current_period_end, greatest(now(), started_at)) "
        "WHERE id = ANY($1::bigint[])",
        ids,
    )


async def new_subscription(
    db: Db, uid: int, plan_id: int, days: int, reference: str, start=None, end_base=None
):
    """A subscription from `start` (default now) for `days` after `end_base` (default now)."""
    return await db.fetchrow(
        "INSERT INTO subscriptions (user_id, plan_id, started_at, current_period_end, provider, "
        "provider_ref) VALUES ($1, $2, coalesce($5::timestamptz, now()), "
        "coalesce($6::timestamptz, now()) + make_interval(days => $3), 'manual_upi', $4) "
        "RETURNING id, started_at, current_period_end",
        uid,
        plan_id,
        days,
        reference,
        start,
        end_base,
    )


async def premium_until(db: Db, uid: int):
    """Latest end of any live subscription of a purchasable plan."""
    return await db.fetchval(
        "SELECT max(s.current_period_end) FROM subscriptions s "  # includes queued ones
        "JOIN plans p ON p.id = s.plan_id AND p.is_active AND p.price_minor IS NOT NULL "
        "WHERE s.user_id = $1 AND s.status = 'active' AND s.current_period_end > now()",
        uid,
    )


async def my_subscriptions(db: Db, uid: int) -> list[asyncpg.Record]:
    """The user's running and queued subscriptions of plans that are on, running first (highest
    tier, then latest end), then the queued ones in the order they start."""
    return await db.fetch(
        """
        SELECT p.name, s.started_at, s.current_period_end,
               coalesce((SELECT max((pe.value ->> 'level')::int) FROM plan_entitlements pe
                          WHERE pe.plan_id = s.plan_id AND pe.feature_key = 'plan_badge'), 0)
                   AS level
        FROM subscriptions s JOIN plans p ON p.id = s.plan_id AND p.is_active
        WHERE s.user_id = $1 AND s.status = 'active' AND s.current_period_end > now()
        ORDER BY (s.started_at > now()), s.started_at, level DESC, s.current_period_end DESC
        """,
        uid,
    )


async def claim_expiry_reminders(db: Db, days: int) -> list[asyncpg.Record]:
    """Users whose LAST live subscription ends within `days` days and who were not reminded about
    that end date yet. Claiming (the insert) and selecting are one statement, so two bot processes
    can never both remind the same user. Rows older than 30 days are tidied up first."""
    await db.execute("DELETE FROM expiry_reminders WHERE period_end < now() - interval '30 days'")
    return await db.fetch(
        """
        WITH due AS (
            SELECT s.user_id, max(s.current_period_end) AS ends
            FROM subscriptions s
            JOIN plans p ON p.id = s.plan_id AND p.is_active AND p.price_minor IS NOT NULL
            JOIN users u ON u.id = s.user_id AND u.status = 'active'
            WHERE s.status = 'active' AND s.current_period_end > now()
            GROUP BY s.user_id
            HAVING max(s.current_period_end) <= now() + make_interval(days => $1)
        ), claimed AS (
            INSERT INTO expiry_reminders (user_id, period_end)
            SELECT user_id, ends FROM due
            ON CONFLICT DO NOTHING
            RETURNING user_id, period_end
        )
        SELECT c.user_id, c.period_end,
               (SELECT p.name FROM subscriptions s JOIN plans p ON p.id = s.plan_id
                 WHERE s.user_id = c.user_id AND s.status = 'active'
                   AND s.current_period_end = c.period_end
                 ORDER BY s.id DESC LIMIT 1) AS plan_name
        FROM claimed c
        """,
        days,
    )


async def delete_reminders_for(db: Db, uid: int) -> None:
    await db.execute("DELETE FROM expiry_reminders WHERE user_id = $1", uid)


async def expire_subscriptions(db: Db) -> int:
    """Tidy-up only: reads already ignore a subscription once its end has passed."""
    status = await db.execute(
        "UPDATE subscriptions SET status = 'expired' "
        "WHERE status = 'active' AND current_period_end IS NOT NULL AND current_period_end <= now()"
    )
    return int(status.split()[-1])


async def user_status_shared(db: Db, uid: int) -> str | None:
    """Account status, share-locked so the account can't be banned/deleted mid-approval."""
    return await db.fetchval("SELECT status FROM users WHERE id = $1 FOR SHARE", uid)


async def user_status_plain(db: Db, uid: int) -> str | None:
    return await db.fetchval("SELECT status FROM users WHERE id = $1", uid)


# ------------------------------------------------------------------ evidence
async def put_evidence(db: Db, payment_id: int, content_type: str, data: bytes) -> str:
    """Store (or replace) the screenshot; returns its sha256. A replaced image loses the analysis
    of the old one in the same statement batch (the caller's transaction)."""
    await db.execute("DELETE FROM payment_evidence_analysis WHERE payment_id = $1", payment_id)
    sha = hashlib.sha256(data).hexdigest()
    await db.execute(
        "INSERT INTO payment_evidence (payment_id, content_type, size_bytes, sha256, data) "
        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (payment_id) DO UPDATE SET "
        "content_type = EXCLUDED.content_type, size_bytes = EXCLUDED.size_bytes, "
        "sha256 = EXCLUDED.sha256, data = EXCLUDED.data, created_at = now()",
        payment_id,
        content_type,
        len(data),
        sha,
        data,
    )
    return sha


async def put_analysis(db: Db, payment_id: int, sha: str, a) -> bool:
    """Save the advisory analysis of the stored image, but only if that image is still the one
    that was analysed (a replacement may have arrived meanwhile)."""
    return bool(
        await db.fetchval(
            "INSERT INTO payment_evidence_analysis (payment_id, sha256, verdict, engine, "
            "amount_minor, payee, utr, paid_at, success_marker, findings) "
            "SELECT e.payment_id, e.sha256, $3, $4, $5, $6, $7, $8, $9, $10::jsonb "
            "FROM payment_evidence e WHERE e.payment_id = $1 AND e.sha256 = $2 "
            "ON CONFLICT (payment_id) DO UPDATE SET sha256 = EXCLUDED.sha256, "
            "verdict = EXCLUDED.verdict, engine = EXCLUDED.engine, "
            "amount_minor = EXCLUDED.amount_minor, payee = EXCLUDED.payee, utr = EXCLUDED.utr, "
            "paid_at = EXCLUDED.paid_at, success_marker = EXCLUDED.success_marker, "
            "findings = EXCLUDED.findings, analyzed_at = now() RETURNING 1",
            payment_id,
            sha,
            a.verdict,
            a.engine,
            a.amount_minor,
            a.payee,
            a.utr,
            a.paid_at,
            a.success_marker,
            json.dumps(list(a.findings)),
        )
    )


async def get_analysis(db: Db, payment_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        "SELECT verdict, engine, amount_minor, payee, utr, paid_at, success_marker, findings::text "
        "AS findings, analyzed_at FROM payment_evidence_analysis WHERE payment_id = $1",
        payment_id,
    )


async def has_evidence(db: Db, payment_id: int) -> bool:
    return bool(
        await db.fetchval("SELECT 1 FROM payment_evidence WHERE payment_id = $1", payment_id)
    )


async def get_evidence(db: Db, payment_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        "SELECT content_type, data FROM payment_evidence WHERE payment_id = $1", payment_id
    )


async def same_image_elsewhere(db: Db, payment_id: int) -> list[asyncpg.Record]:
    """Other payments whose screenshot is byte-identical (a reused image is worth a look)."""
    return await db.fetch(
        "SELECT p.id, p.reference, p.status FROM payment_evidence a "
        "JOIN payment_evidence b ON b.sha256 = a.sha256 AND b.payment_id <> a.payment_id "
        "JOIN payments p ON p.id = b.payment_id WHERE a.payment_id = $1 ORDER BY p.id",
        payment_id,
    )


async def same_utr_elsewhere(db: Db, payment_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        "SELECT o.id, o.reference, o.status FROM payments p "
        "JOIN payments o ON o.utr = p.utr AND o.id <> p.id "
        "WHERE p.id = $1 AND p.utr IS NOT NULL ORDER BY o.id",
        payment_id,
    )


async def purge_reviewed_evidence(db: Db, days: int) -> int:
    status = await db.execute(
        "DELETE FROM payment_evidence e USING payments p WHERE p.id = e.payment_id "
        "AND p.status IN ('approved', 'rejected', 'cancelled') "
        "AND coalesce(p.reviewed_at, p.updated_at) < now() - make_interval(days => $1)",
        days,
    )
    return int(status.split()[-1])


# ------------------------------------------------------------------ admin listing
async def list_payments(
    db: Db, *, status: str | None, limit: int, offset: int
) -> list[asyncpg.Record]:
    return await db.fetch(
        f"SELECT {PAYMENT_COLUMNS}, (SELECT a.verdict FROM payment_evidence_analysis a "  # noqa: S608
        "WHERE a.payment_id = p.id) AS evidence_verdict, "
        "EXISTS (SELECT 1 FROM payment_evidence e WHERE e.payment_id = p.id) AS has_evidence "
        "FROM payments p JOIN plans pl ON pl.id = p.plan_id "
        "WHERE p.status <> 'created' AND ($1::text IS NULL OR p.status = $1) "
        "ORDER BY (p.status = 'pending') DESC, "
        "coalesce(p.submitted_at, p.created_at) DESC, p.id DESC "
        "LIMIT $2 OFFSET $3",
        status,
        limit,
        offset,
    )


async def count_payments(db: Db, *, status: str | None) -> int:
    return await db.fetchval(
        "SELECT count(*) FROM payments WHERE status <> 'created' "
        "AND ($1::text IS NULL OR status = $1)",
        status,
    )


async def payment_counts(db: Db) -> asyncpg.Record:
    return await db.fetchrow(
        "SELECT count(*) FILTER (WHERE status = 'pending')  AS pending, "
        "count(*) FILTER (WHERE status = 'approved') AS approved, "
        "count(*) FILTER (WHERE status = 'rejected') AS rejected FROM payments"
    )


async def audit(
    db: Db, admin_id: int, action: str, *, target: int | None = None, detail: str | None = None
) -> None:
    await db.execute(
        "INSERT INTO admin_actions (admin_id, action, target_user_id, detail) "
        "VALUES ($1, $2, $3, $4)",
        admin_id,
        action,
        target,
        detail,
    )

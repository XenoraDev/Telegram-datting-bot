"""Direct-SQL builders so service / DB tests don't have to click through onboarding."""

from __future__ import annotations

import asyncpg


async def make_user(
    pool: asyncpg.Pool,
    uid: int,
    *,
    gender: str = "m",
    looking: str = "any",
    age: int = 25,
    city: str = "Berlin",
    status: str = "active",
    confirmed: bool = True,
    with_profile: bool = True,
) -> int:
    await pool.execute(
        "INSERT INTO users (id, username, status, age_confirmed_at) "
        "VALUES ($1, $2, $3, CASE WHEN $4 THEN now() END)",
        uid,
        f"user{uid}",
        status,
        confirmed,
    )
    if with_profile:
        await pool.execute(
            "INSERT INTO profiles (user_id, name, age, gender, looking_for, city, bio) "
            "VALUES ($1, $2, $3, $4, $5, $6, 'bio')",
            uid,
            f"Name{uid}",
            age,
            gender,
            looking,
            city,
        )
    return uid


async def start_chat(svc, a: int, b: int) -> int:
    """Put a and b into an active chat through the real matchmaking; returns the session id."""
    first = await svc.matchmaking.search(a)
    assert first.kind == "queued", first
    second = await svc.matchmaking.search(b)
    assert second.kind == "matched", second
    return second.session_id


async def ended_session(pool: asyncpg.Pool, a: int, b: int, *, ended_ago: str = "1 minute") -> int:
    """Insert an already-ended session (fast path for tests that need many sessions)."""
    sid = await pool.fetchval(
        "INSERT INTO chat_sessions (user_a, user_b, status, started_at, ended_at, ended_by) "
        "VALUES ($1, $2, 'ended', now() - $3::text::interval - interval '5 minutes', "
        "now() - $3::text::interval, $1) RETURNING id",
        a,
        b,
        ended_ago,
    )
    await pool.execute(
        "INSERT INTO chat_session_members (session_id, user_id, active) "
        "VALUES ($1, $2, false), ($1, $3, false)",
        sid,
        a,
        b,
    )
    return sid


async def enable_payments(pool: asyncpg.Pool, upi: str = "shop@okbank") -> None:
    """Switch manual UPI payments on (what an admin does on the Payment settings page)."""
    await pool.execute(
        "UPDATE payment_settings SET upi_id = $1, payee_name = 'Test Payee', "
        "instructions = 'Pay then upload the screenshot.', is_active = true",
        upi,
    )


async def make_plan(
    pool: asyncpg.Pool,
    code: str = "premium_30",
    *,
    name: str = "Premium 30 Days",
    days: int = 30,
    price_minor: int = 9900,
    active: bool = True,
    tier: str = "premium",
) -> int:
    """A purchasable plan with the given tier's entitlements (what the admin Plans page makes)."""
    from bot.db import repo_payments

    plan_id = await pool.fetchval(
        "INSERT INTO plans (code, name, is_active, duration_days, price_minor) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        code,
        name,
        active,
        days,
        price_minor,
    )
    await repo_payments.apply_tier(pool, plan_id, tier)
    return plan_id


async def give_tier(pool: asyncpg.Pool, uid: int, tier: str, *, days: int = 30) -> int:
    """Put the user on a live subscription of the given tier (creates the plan on first use)."""
    plan = await pool.fetchval("SELECT id FROM plans WHERE code = $1", f"t_{tier}")
    if plan is None:
        plan = await make_plan(pool, f"t_{tier}", name=tier.title(), tier=tier)
    return await pool.fetchval(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "VALUES ($1, $2, now() + make_interval(days => $3)) RETURNING id",
        uid,
        plan,
        days,
    )

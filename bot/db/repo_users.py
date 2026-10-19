"""users table access. All SQL is parameterised; callers pass Telegram ids from trusted updates."""

from __future__ import annotations

import asyncpg

Db = asyncpg.Pool | asyncpg.Connection


async def get(db: Db, uid: int) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT * FROM users WHERE id = $1", uid)


async def tombstone_reason(db: Db, uid: int) -> str | None:
    return await db.fetchval("SELECT reason FROM deletion_tombstones WHERE user_id = $1", uid)


async def upsert_seen(db: Db, uid: int, username: str | None) -> asyncpg.Record:
    """Create the user on first contact; refresh username / last_seen otherwise.

    Never stores a username for deleted or underage-blocked accounts. The extra `inserted` column
    is true only for the call that CREATED the row (used once, for first-touch attribution).
    """
    return await db.fetchrow(
        """
        INSERT INTO users (id, username) VALUES ($1, $2)
        ON CONFLICT (id) DO UPDATE SET
            username = CASE WHEN users.status IN ('deleted', 'blocked_underage')
                            THEN NULL ELSE EXCLUDED.username END,
            last_seen_at = now()
        RETURNING *, (xmax = 0) AS inserted
        """,
        uid,
        username,
    )


async def confirm_adult(db: Db, uid: int) -> bool:
    """Record the 18+ declaration. Only possible while onboarding; idempotent."""
    return bool(
        await db.fetchval(
            """
            UPDATE users SET age_confirmed_at = COALESCE(age_confirmed_at, now())
            WHERE id = $1 AND status = 'onboarding'
            RETURNING 1
            """,
            uid,
        )
    )


async def activate(db: Db, uid: int) -> bool:
    """Onboarding -> active, only with a confirmed 18+ declaration AND an adult profile."""
    return bool(
        await db.fetchval(
            """
            UPDATE users SET status = 'active'
            WHERE id = $1 AND status IN ('onboarding', 'active')
              AND age_confirmed_at IS NOT NULL
              AND EXISTS (SELECT 1 FROM profiles WHERE user_id = $1 AND age >= 18)
            RETURNING 1
            """,
            uid,
        )
    )


async def mark_underage(db: Db, uid: int, reason: str) -> None:
    """Hard block. Wipes profile + username, queue entry, and writes the tombstone.

    Callers run this inside a transaction together with ending any active chat.
    """
    await db.execute(
        """
        UPDATE users SET status = 'blocked_underage', status_reason = $2, username = NULL
        WHERE id = $1
        """,
        uid,
        reason,
    )
    await db.execute("DELETE FROM profiles WHERE user_id = $1", uid)
    await db.execute("DELETE FROM chat_queue WHERE user_id = $1", uid)
    await db.execute(
        "INSERT INTO deletion_tombstones (user_id, reason) VALUES ($1, 'underage') "
        "ON CONFLICT (user_id) DO NOTHING",
        uid,
    )


async def reset_deleted(db: Db, uid: int, username: str | None) -> asyncpg.Record | None:
    """A deleted account starts over from scratch (needs a fresh 18+ confirmation)."""
    return await db.fetchrow(
        """
        UPDATE users SET status = 'onboarding', status_reason = NULL,
                         age_confirmed_at = NULL, username = $2, last_seen_at = now()
        WHERE id = $1 AND status = 'deleted'
        RETURNING *
        """,
        uid,
        username,
    )


async def scrub(db: Db, uid: int) -> None:
    """Remove personal data from the users row. Banned / underage keep their status."""
    await db.execute(
        """
        UPDATE users SET
            username = NULL, language = 'en', age_confirmed_at = NULL,
            status = CASE WHEN status IN ('banned', 'blocked_underage')
                          THEN status ELSE 'deleted' END,
            status_reason = CASE WHEN status IN ('banned', 'blocked_underage')
                                 THEN status_reason ELSE 'user_request' END
        WHERE id = $1
        """,
        uid,
    )


async def set_language(db: Db, uid: int, language: str) -> bool:
    """Persist the user's chosen language. Only accounts that can use the bot may change it."""
    return bool(
        await db.fetchval(
            "UPDATE users SET language = $2 WHERE id = $1 "
            "AND status IN ('onboarding', 'active') RETURNING 1",
            uid,
            language,
        )
    )

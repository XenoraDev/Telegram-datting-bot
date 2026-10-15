"""blocks, reports, restrictions."""

from __future__ import annotations

import json
from datetime import timedelta

import asyncpg

from .repo_users import Db


async def add_block(db: Db, blocker: int, blocked: int) -> None:
    await db.execute(
        "INSERT INTO blocks (blocker_id, blocked_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        blocker,
        blocked,
    )


async def is_blocked_either_way(db: Db, a: int, b: int) -> bool:
    return bool(
        await db.fetchval(
            "SELECT 1 FROM blocks WHERE (blocker_id = $1 AND blocked_id = $2) "
            "OR (blocker_id = $2 AND blocked_id = $1)",
            a,
            b,
        )
    )


async def delete_blocks_by(db: Db, blocker: int) -> None:
    """Remove the user's OWN block list. Blocks against them stay (protects the blockers)."""
    await db.execute("DELETE FROM blocks WHERE blocker_id = $1", blocker)


async def lock_reporter(conn: asyncpg.Connection, reporter: int) -> None:
    """Serialise one reporter's rate-limit check + insert (transaction-scoped)."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('report:' || ($1::bigint)::text, 0))",
        reporter,
    )


async def count_reports_by(db: Db, reporter: int, since: timedelta) -> int:
    return await db.fetchval(
        "SELECT count(*) FROM reports WHERE reporter_id = $1 AND created_at > now() - $2::interval",
        reporter,
        since,
    )


async def add_report(
    db: Db,
    *,
    reporter: int,
    reported: int,
    session_id: int,
    reason: str,
    context: list[dict] | None,
) -> int | None:
    """Insert a chat report. None if this reporter already has an open report for the session."""
    return await db.fetchval(
        """
        INSERT INTO reports (reporter_id, reported_id, session_id, source, reason, context)
        VALUES ($1, $2, $3, 'chat', $4, $5::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        reporter,
        reported,
        session_id,
        reason,
        json.dumps(context) if context else None,
    )


async def distinct_reporters(db: Db, reported: int, since: timedelta) -> int:
    return await db.fetchval(
        "SELECT count(DISTINCT reporter_id) FROM reports "
        "WHERE reported_id = $1 AND created_at > now() - $2::interval",
        reported,
        since,
    )


async def purge_expired_contexts(db: Db) -> int:
    status = await db.execute(
        "UPDATE reports SET context = NULL "
        "WHERE context IS NOT NULL AND context_expires_at <= now()"
    )
    return int(status.split()[-1])


async def clear_contexts_for(db: Db, uid: int) -> None:
    await db.execute(
        "UPDATE reports SET context = NULL WHERE reporter_id = $1 OR reported_id = $1", uid
    )


async def add_restriction(db: Db, uid: int, kind: str, duration: timedelta, reason: str) -> bool:
    """Add a restriction unless one of that kind with more than half the time left is active.

    (Repeated triggers within the same window must not pile up; a nearly expired one is renewed.)
    """
    return bool(
        await db.fetchval(
            """
            INSERT INTO restrictions (user_id, kind, until, reason)
            SELECT u.id, $2, now() + $3::interval, $4 FROM users u
            WHERE u.id = $1
              AND NOT EXISTS (SELECT 1 FROM restrictions r
                              WHERE r.user_id = $1 AND r.kind = $2
                                AND r.until > now() + ($3::interval / 2))
            RETURNING 1
            """,
            uid,
            kind,
            duration,
            reason,
        )
    )


async def active_restriction(db: Db, uid: int, kind: str):
    return await db.fetchval(
        "SELECT max(until) FROM restrictions WHERE user_id = $1 AND kind = $2 AND until > now()",
        uid,
        kind,
    )

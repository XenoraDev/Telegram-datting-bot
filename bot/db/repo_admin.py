"""SQL for the in-bot admin: statistics, user lookup, reports queue, ban / unban, audit log.

Every statement is parameterised; admin input only ever reaches SQL as bound integers.
"""

from __future__ import annotations

import asyncpg

from .repo_users import Db


async def stats(db: Db) -> asyncpg.Record:
    return await db.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM users)                                   AS total,
            (SELECT count(*) FROM users WHERE status = 'active')           AS active,
            (SELECT count(*) FROM users WHERE status = 'onboarding')       AS onboarding,
            (SELECT count(*) FROM users WHERE status = 'banned')           AS banned,
            (SELECT count(*) FROM users WHERE status = 'blocked_underage') AS underage,
            (SELECT count(*) FROM reports WHERE status = 'open')           AS open_reports,
            (SELECT count(*) FROM chat_sessions WHERE status = 'active')   AS active_chats,
            (SELECT count(*) FROM chat_queue)                              AS queued,
            (SELECT count(*) FROM matches)                                 AS matches
        """
    )


async def user_overview(db: Db, uid: int) -> asyncpg.Record | None:
    """Moderation view of one user. Deliberately no chat content and no contact secrets."""
    return await db.fetchrow(
        """
        SELECT u.id, u.username, u.status, u.status_reason, u.age_confirmed_at,
               u.created_at, u.last_seen_at,
               p.name, p.age, p.gender, p.country, p.city, p.bio,
               (p.user_id IS NOT NULL) AS has_profile,
               (SELECT count(*) FROM reports r
                 WHERE r.reported_id = u.id AND r.status = 'open')          AS open_reports,
               (SELECT count(*) FROM reports r WHERE r.reported_id = u.id)  AS total_reports,
               (SELECT count(*) FROM reports r WHERE r.reporter_id = u.id)  AS reports_filed,
               (SELECT count(*) FROM blocks b WHERE b.blocked_id = u.id)    AS blocked_by,
               EXISTS (SELECT 1 FROM restrictions x
                        WHERE x.user_id = u.id AND x.until > now())         AS restricted,
               EXISTS (SELECT 1 FROM chat_session_members m
                        WHERE m.user_id = u.id AND m.active)                AS in_chat
        FROM users u LEFT JOIN profiles p ON p.user_id = u.id
        WHERE u.id = $1
        """,
        uid,
    )


async def count_open_reports(db: Db) -> int:
    return await db.fetchval("SELECT count(*) FROM reports WHERE status = 'open'")


async def list_open_reports(db: Db, limit: int, offset: int) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, reporter_id, reported_id, source, reason, created_at
        FROM reports WHERE status = 'open'
        ORDER BY created_at DESC, id DESC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )


async def get_report(db: Db, report_id: int) -> asyncpg.Record | None:
    """One report with the retained context (only while it has not expired / been purged)."""
    return await db.fetchrow(
        """
        SELECT r.id, r.reporter_id, r.reported_id, r.source, r.reason, r.status, r.created_at,
               CASE WHEN r.context_expires_at > now() THEN r.context END AS context,
               u.status AS reported_status,
               (SELECT count(*) FROM reports o
                 WHERE o.reported_id = r.reported_id AND o.status = 'open') AS open_against
        FROM reports r JOIN users u ON u.id = r.reported_id
        WHERE r.id = $1
        """,
        report_id,
    )


async def set_report_status(db: Db, report_id: int, status: str) -> bool:
    """open -> actioned | dismissed. False if it was not open any more (already handled)."""
    return bool(
        await db.fetchval(
            "UPDATE reports SET status = $2 WHERE id = $1 AND status = 'open' RETURNING 1",
            report_id,
            status,
        )
    )


async def action_open_reports_against(db: Db, uid: int) -> int:
    status = await db.execute(
        "UPDATE reports SET status = 'actioned' WHERE reported_id = $1 AND status = 'open'", uid
    )
    return int(status.split()[-1])


async def ban(db: Db, uid: int) -> bool:
    """Reversible admin ban (no tombstone). Only onboarding / active accounts can be banned."""
    return bool(
        await db.fetchval(
            """
            UPDATE users SET status = 'banned', status_reason = 'admin_ban', banned_until = NULL
            WHERE id = $1 AND status IN ('onboarding', 'active')
            RETURNING 1
            """,
            uid,
        )
    )


async def unban(db: Db, uid: int) -> str | None:
    """banned -> active (adult-confirmed with an adult profile) or onboarding. None otherwise."""
    return await db.fetchval(
        """
        UPDATE users SET status_reason = NULL, banned_until = NULL,
            status = CASE WHEN age_confirmed_at IS NOT NULL
                           AND EXISTS (SELECT 1 FROM profiles WHERE user_id = $1 AND age >= 18)
                          THEN 'active' ELSE 'onboarding' END
        WHERE id = $1 AND status = 'banned'
        RETURNING status
        """,
        uid,
    )


async def drop_ban_tombstone(db: Db, uid: int) -> None:
    await db.execute(
        "DELETE FROM deletion_tombstones WHERE user_id = $1 AND reason = 'banned'", uid
    )


async def audit(
    db: Db,
    admin_id: int,
    action: str,
    *,
    target: int | None = None,
    report_id: int | None = None,
    detail: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO admin_actions (admin_id, action, target_user_id, report_id, detail) "
        "VALUES ($1, $2, $3, $4, $5)",
        admin_id,
        action,
        target,
        report_id,
        detail,
    )

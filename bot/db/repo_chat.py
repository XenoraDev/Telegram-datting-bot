"""Random chat queue + sessions. chat_session_members is the single source of truth for pairing.

Every function that changes pairing must run inside the global matchmaking advisory lock
(`lock_matchmaking`) so matching, ending chats and account deletion are serialised.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from .. import domain
from .repo_users import Db

MATCH_LOCK_KEY = 0x6D61746368  # "match"

# A user may take part in chat only when active, adult-confirmed and with an adult profile.
_ELIGIBLE = "u.status = 'active' AND u.age_confirmed_at IS NOT NULL AND p.age >= 18"


async def lock_matchmaking(conn: asyncpg.Connection) -> None:
    """Transaction-scoped global lock: auto-released on COMMIT/ROLLBACK."""
    await conn.execute("SELECT pg_advisory_xact_lock($1)", MATCH_LOCK_KEY)


@dataclass(frozen=True, slots=True)
class Peer:
    """Who was paired. Deliberately just the id: what a user may SEE of a partner is decided by
    the partner-card service (privacy + entitlements), never carried through the matching code."""

    user_id: int


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    kind: str  # matched | queued | already_queued | already_in_chat | not_eligible | cooldown
    session_id: int | None = None
    partner: Peer | None = None
    me: Peer | None = None
    cooldown_until: object | None = None


async def active_restriction(db: Db, uid: int, kind: str):
    return await db.fetchval(
        "SELECT max(until) FROM restrictions WHERE user_id = $1 AND kind = $2 AND until > now()",
        uid,
        kind,
    )


async def find_or_queue(pool: asyncpg.Pool, uid: int, priority: int = 0) -> SearchOutcome:
    """Pair `uid` with the best waiter (highest priority, then longest waiting) or queue them."""
    async with pool.acquire() as conn, conn.transaction():
        await lock_matchmaking(conn)

        me = await conn.fetchrow(
            f"""SELECT u.id
                FROM users u JOIN profiles p ON p.user_id = u.id
                WHERE u.id = $1 AND {_ELIGIBLE}""",  # noqa: S608 - constant fragment, no user input
            uid,
        )
        if me is None:
            return SearchOutcome("not_eligible")

        until = await active_restriction(conn, uid, "chat_cooldown")
        if until is not None:
            return SearchOutcome("cooldown", cooldown_until=until)

        if await conn.fetchval(
            "SELECT 1 FROM chat_session_members WHERE user_id = $1 AND active", uid
        ):
            return SearchOutcome("already_in_chat")

        cand = await conn.fetchrow(
            f"""
            SELECT q.user_id
            FROM chat_queue q
            JOIN users u ON u.id = q.user_id
            JOIN profiles p ON p.user_id = u.id
            WHERE q.user_id <> $1
              AND {_ELIGIBLE}
              AND NOT EXISTS (SELECT 1 FROM blocks b WHERE
                    (b.blocker_id = $1 AND b.blocked_id = q.user_id)
                 OR (b.blocker_id = q.user_id AND b.blocked_id = $1))
              AND NOT EXISTS (SELECT 1 FROM restrictions r WHERE r.user_id = q.user_id
                    AND r.kind = 'chat_cooldown' AND r.until > now())
              AND NOT EXISTS (SELECT 1 FROM chat_session_members m
                              WHERE m.user_id = q.user_id AND m.active)
              AND NOT EXISTS (SELECT 1 FROM chat_sessions s
                    WHERE s.ended_at > now() - make_interval(mins => $2)
                      AND ((s.user_a = $1 AND s.user_b = q.user_id)
                        OR (s.user_a = q.user_id AND s.user_b = $1)))
            ORDER BY q.priority DESC, q.joined_at
            LIMIT 1
            FOR UPDATE OF q SKIP LOCKED
            """,  # noqa: S608 - constant fragment, no user input
            uid,
            domain.REMATCH_BLOCK_MINUTES,
        )

        if cand is None:
            inserted = await conn.fetchval(
                "INSERT INTO chat_queue (user_id, priority) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING RETURNING 1",
                uid,
                priority,
            )
            return SearchOutcome("queued" if inserted else "already_queued")

        await conn.execute("DELETE FROM chat_queue WHERE user_id IN ($1, $2)", uid, cand["user_id"])
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions (user_a, user_b) VALUES ($1, $2) RETURNING id",
            cand["user_id"],
            uid,
        )
        await conn.execute(
            "INSERT INTO chat_session_members (session_id, user_id) VALUES ($1, $2), ($1, $3)",
            session_id,
            cand["user_id"],
            uid,
        )
        return SearchOutcome(
            "matched",
            session_id=session_id,
            partner=Peer(cand["user_id"]),
            me=Peer(uid),
        )


async def leave_queue(db: Db, uid: int) -> bool:
    return bool(await db.fetchval("DELETE FROM chat_queue WHERE user_id = $1 RETURNING 1", uid))


async def end_active_session(
    conn: asyncpg.Connection, uid: int, reason: str
) -> tuple[int, int] | None:
    """End uid's active session. Returns (session_id, partner_id) or None.

    Must run inside a transaction holding the matchmaking lock (see end_chat()).
    """
    row = await conn.fetchrow(
        """
        UPDATE chat_sessions SET status = 'ended', ended_at = now(), ended_by = $1, end_reason = $2
        WHERE status = 'active' AND id = (
            SELECT session_id FROM chat_session_members WHERE user_id = $1 AND active)
        RETURNING id, user_a, user_b
        """,
        uid,
        reason,
    )
    if row is None:
        return None
    await conn.execute(
        "UPDATE chat_session_members SET active = false WHERE session_id = $1", row["id"]
    )
    partner = row["user_b"] if row["user_a"] == uid else row["user_a"]
    return row["id"], partner


async def end_chat(pool: asyncpg.Pool, uid: int, reason: str) -> tuple[int, int] | None:
    async with pool.acquire() as conn, conn.transaction():
        await lock_matchmaking(conn)
        return await end_active_session(conn, uid, reason)


async def get_partner(db: Db, uid: int) -> tuple[int, int] | None:
    """(session_id, partner_id) of uid's active chat, only if BOTH sides are still active users."""
    row = await db.fetchrow(
        """
        SELECT m1.session_id, m2.user_id AS partner_id
        FROM chat_session_members m1
        JOIN chat_session_members m2 ON m2.session_id = m1.session_id AND m2.user_id <> m1.user_id
        JOIN users u1 ON u1.id = m1.user_id AND u1.status = 'active'
        JOIN users u2 ON u2.id = m2.user_id AND u2.status = 'active'
        WHERE m1.user_id = $1 AND m1.active AND m2.active
        """,
        uid,
    )
    return (row["session_id"], row["partner_id"]) if row else None


async def counterpart_in_session(db: Db, session_id: int, uid: int) -> tuple[int, bool] | None:
    """(other_user, is_active) if uid took part and the session is recent enough to act on."""
    row = await db.fetchrow(
        """
        SELECT m2.user_id AS other, s.status = 'active' AS is_active
        FROM chat_sessions s
        JOIN chat_session_members m1 ON m1.session_id = s.id AND m1.user_id = $2
        JOIN chat_session_members m2 ON m2.session_id = s.id AND m2.user_id <> $2
        WHERE s.id = $1
          AND (s.ended_at IS NULL OR s.ended_at > now() - make_interval(days => $3))
        """,
        session_id,
        uid,
        domain.REPORT_WINDOW_DAYS,
    )
    return (row["other"], row["is_active"]) if row else None


async def user_session_ids(db: Db, uid: int) -> list[int]:
    rows = await db.fetch("SELECT session_id FROM chat_session_members WHERE user_id = $1", uid)
    return [r["session_id"] for r in rows]


async def delete_unreferenced_sessions(conn: asyncpg.Connection, uid: int) -> None:
    """Drop ended-session metadata of uid, except sessions a report points to."""
    await conn.execute(
        """
        DELETE FROM chat_sessions s
        WHERE s.status = 'ended' AND (s.user_a = $1 OR s.user_b = $1)
          AND NOT EXISTS (SELECT 1 FROM reports r WHERE r.session_id = s.id)
        """,
        uid,
    )


async def pop_stale_queue(pool: asyncpg.Pool, minutes: int) -> list[int]:
    rows = await pool.fetch(
        "DELETE FROM chat_queue WHERE joined_at < now() - make_interval(mins => $1) "
        "RETURNING user_id",
        minutes,
    )
    return [r["user_id"] for r in rows]

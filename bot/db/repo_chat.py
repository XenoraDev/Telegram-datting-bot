"""Random chat queue + sessions. chat_session_members is the single source of truth for pairing.

Every function that changes pairing must run inside the global matchmaking advisory lock
(`lock_matchmaking`) so matching, ending chats and account deletion are serialised.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import asyncpg

from .. import domain
from . import repo_browse
from .repo_users import Db

log = logging.getLogger(__name__)

MATCH_LOCK_KEY = 0x6D61746368  # "match"

# Fairness: whoever has waited this long goes first, whatever the boost / tier of the others, so
# a stream of boosted or subscribed users can never keep an ordinary waiter queued indefinitely.
STARVATION_SECONDS = 120

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
    # matched | queued | already_queued | already_in_chat | not_eligible | cooldown, and for a chat
    # with a mutual match: started | already | busy | partner_busy | unavailable
    kind: str
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
    """Pair `uid` with the best waiter or queue them. Order: anyone who has waited past
    STARVATION_SECONDS, then an active profile boost, then the tier priority, then the longest
    waiting. Eligibility, blocks and cooldowns are filters, so ranking can never override them."""
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
            SELECT q.user_id, greatest(0, extract(epoch FROM now() - q.joined_at))::bigint AS waited
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
            ORDER BY q.joined_at <= now() - make_interval(secs => $3) DESC,
                     EXISTS (SELECT 1 FROM profile_boosts bo
                             WHERE bo.user_id = q.user_id AND bo.ends_at > now()) DESC,
                     q.priority DESC, q.joined_at
            LIMIT 1
            FOR UPDATE OF q SKIP LOCKED
            """,  # noqa: S608 - constant fragment, no user input
            uid,
            domain.REMATCH_BLOCK_MINUTES,
            STARVATION_SECONDS,
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
        # the searcher is paired at once, the waiter after `waited` seconds
        await record_queue_event(conn, instant=1, waited=1, wait_seconds=cand["waited"])
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


async def start_match_chat(pool: asyncpg.Pool, uid: int, public_id: uuid.UUID) -> SearchOutcome:
    """Open a chat between `uid` and the owner of the profile handle `public_id`, but only if they
    are a real mutual match and every rule of random chat still holds. One transaction under the
    matchmaking lock (and the pair's like lock), so it cannot race a search, a stop, a block or
    an account deletion. If they are already chatting with each other that chat is returned, never
    a second one. Every "no" about the OTHER person is the same answer, "unavailable", so a
    block, a deleted account or a stale match cannot be told apart.

    kind: started | already (an active chat with this very person) | busy (the caller is in
    another chat) | partner_busy | unavailable | not_eligible | cooldown
    """
    async with pool.acquire() as conn, conn.transaction():
        await lock_matchmaking(conn)
        other = await repo_browse.user_id_of(conn, public_id)
        if other is None or other == uid:
            return SearchOutcome("unavailable")
        await repo_browse.lock_pair(conn, uid, other)

        me = await conn.fetchrow(
            f"""SELECT u.id FROM users u JOIN profiles p ON p.user_id = u.id
                WHERE u.id = $1 AND {_ELIGIBLE}""",  # noqa: S608 - constant fragment
            uid,
        )
        if me is None:
            return SearchOutcome("not_eligible")
        until = await active_restriction(conn, uid, "chat_cooldown")
        if until is not None:
            return SearchOutcome("cooldown", cooldown_until=until)

        mine = await conn.fetchrow(
            "SELECT session_id FROM chat_session_members WHERE user_id = $1 AND active", uid
        )
        if mine is not None:
            with_them = await conn.fetchval(
                "SELECT 1 FROM chat_session_members WHERE session_id = $1 AND user_id = $2 "
                "AND active",
                mine["session_id"],
                other,
            )
            kind = "already" if with_them else "busy"
            return SearchOutcome(kind, session_id=mine["session_id"] if with_them else None)

        ok = await conn.fetchval(
            f"""
            SELECT 1 FROM users u JOIN profiles p ON p.user_id = u.id
            WHERE u.id = $2 AND {_ELIGIBLE}
              AND EXISTS (SELECT 1 FROM matches m
                          WHERE m.user_a_id = least($1::bigint, $2::bigint)
                            AND m.user_b_id = greatest($1::bigint, $2::bigint))
              AND NOT EXISTS (SELECT 1 FROM blocks b WHERE
                    (b.blocker_id = $1 AND b.blocked_id = $2)
                 OR (b.blocker_id = $2 AND b.blocked_id = $1))
              AND NOT EXISTS (SELECT 1 FROM restrictions r WHERE r.user_id = $2
                    AND r.kind = 'chat_cooldown' AND r.until > now())
            """,  # noqa: S608 - constant fragment, no user input
            uid,
            other,
        )
        if not ok:
            return SearchOutcome("unavailable")
        if await conn.fetchval(
            "SELECT 1 FROM chat_session_members WHERE user_id = $1 AND active", other
        ):
            return SearchOutcome("partner_busy")

        await conn.execute("DELETE FROM chat_queue WHERE user_id IN ($1, $2)", uid, other)
        session_id = await conn.fetchval(
            "INSERT INTO chat_sessions (user_a, user_b) VALUES ($1, $2) RETURNING id", other, uid
        )
        await conn.execute(
            "INSERT INTO chat_session_members (session_id, user_id) VALUES ($1, $2), ($1, $3)",
            session_id,
            other,
            uid,
        )
        return SearchOutcome("started", session_id=session_id, partner=Peer(other), me=Peer(uid))


async def record_queue_event(
    db: Db,
    *,
    instant: int = 0,
    waited: int = 0,
    wait_seconds: int = 0,
    expired: int = 0,
    cancelled: int = 0,
) -> None:
    """Add to this hour's counters (migration 014). Statistics must never break matching, so a
    failure here is logged and dropped (inside a savepoint when called in a transaction)."""
    args = (instant, waited, wait_seconds, expired, cancelled)
    try:
        if isinstance(db, asyncpg.Pool):
            async with db.acquire() as conn:
                await _add_queue_counts(conn, *args)
        else:
            await _add_queue_counts(db, *args)
    except Exception:
        log.warning("could not record queue statistics", exc_info=True)


async def _add_queue_counts(
    conn: asyncpg.Connection,
    instant: int,
    waited: int,
    wait_seconds: int,
    expired: int,
    cancelled: int,
) -> None:
    async with conn.transaction():  # a savepoint when already inside a transaction
        await conn.execute(
            """
            INSERT INTO queue_stats
                (day, hour, instant, waited, wait_sum_seconds, expired, cancelled)
            VALUES ((now() AT TIME ZONE 'utc')::date,
                    extract(hour FROM now() AT TIME ZONE 'utc')::int, $1, $2, $3, $4, $5)
            ON CONFLICT (day, hour) DO UPDATE SET
                instant = queue_stats.instant + EXCLUDED.instant,
                waited = queue_stats.waited + EXCLUDED.waited,
                wait_sum_seconds = queue_stats.wait_sum_seconds + EXCLUDED.wait_sum_seconds,
                expired = queue_stats.expired + EXCLUDED.expired,
                cancelled = queue_stats.cancelled + EXCLUDED.cancelled
            """,
            instant,
            waited,
            wait_seconds,
            expired,
            cancelled,
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


# A message only rewrites the activity time when the stored one is older than this: it keeps the
# write rate low and costs at most this much accuracy on the inactivity timer.
ACTIVITY_WRITE_GRACE_SECONDS = 10


async def touch_session(db: Db, session_id: int) -> bool:
    """A message is about to be delivered in this chat: restart its inactivity timer.

    False when the chat is no longer active (it was just closed), in which case the message must
    NOT be delivered. The update takes the row lock, so it is ordered against `end_idle_sessions`:
    either the timer is restarted first (the sweep then no longer matches) or the sweep closed the
    chat first (this returns False)."""
    if await db.fetchval(
        "UPDATE chat_sessions SET last_activity_at = now() WHERE id = $1 AND status = 'active' "
        "AND last_activity_at < now() - make_interval(secs => $2) RETURNING 1",
        session_id,
        float(ACTIVITY_WRITE_GRACE_SECONDS),
    ):
        return True
    # Not written: either it was touched a moment ago (fine) or the chat has ended.
    return bool(
        await db.fetchval(
            "SELECT 1 FROM chat_sessions WHERE id = $1 AND status = 'active'", session_id
        )
    )


async def end_idle_sessions(pool: asyncpg.Pool, minutes: int) -> list[tuple[int, int, int]]:
    """Close every active chat whose last message is older than `minutes`.

    Returns [(session_id, user_a, user_b)] of the chats closed by THIS call, so each is announced
    exactly once. Runs under the matchmaking lock like every other way a chat ends, and the
    condition is checked in the same statement that closes the chat: a message that arrived first
    has already moved the activity time, and the chat is simply not matched."""
    async with pool.acquire() as conn, conn.transaction():
        await lock_matchmaking(conn)
        rows = await conn.fetch(
            """
            UPDATE chat_sessions
            SET status = 'ended', ended_at = now(), ended_by = NULL, end_reason = 'inactivity'
            WHERE status = 'active' AND last_activity_at < now() - make_interval(mins => $1)
              AND user_a > 0 AND user_b > 0   -- never the synthetic chats of a load test
            RETURNING id, user_a, user_b
            """,
            minutes,
        )
        if rows:
            await conn.execute(
                "UPDATE chat_session_members SET active = false WHERE session_id = ANY($1)",
                [r["id"] for r in rows],
            )
    return [(r["id"], r["user_a"], r["user_b"]) for r in rows]


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
    if rows:
        await record_queue_event(pool, expired=len(rows))
    return [r["user_id"] for r in rows]

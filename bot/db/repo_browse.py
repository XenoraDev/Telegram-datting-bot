"""Browse / like / pass / matches. All SQL for P2 lives here.

Eligibility is re-checked in SQL on every call (active + 18+ confirmed + adult profile), so a
stale button can never act on a banned, deleted or underage user.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import asyncpg

from .repo_users import Db

_ELIGIBLE = "u.status = 'active' AND u.age_confirmed_at IS NOT NULL AND p.age >= 18"
_NOT_BLOCKED = """NOT EXISTS (SELECT 1 FROM blocks b WHERE
        (b.blocker_id = {me} AND b.blocked_id = u.id)
     OR (b.blocker_id = u.id AND b.blocked_id = {me}))"""


# Ranking level of a user's live subscription (entitlement `match_priority`, 0 for Free).
_PRIORITY = """coalesce((
    SELECT max((pe.value ->> 'level')::int)
    FROM subscriptions s
    JOIN plans pl ON pl.id = s.plan_id AND pl.is_active
    JOIN plan_entitlements pe ON pe.plan_id = pl.id AND pe.feature_key = 'match_priority'
    WHERE s.user_id = {u}.id AND s.status = 'active' AND s.started_at <= now()
      AND s.current_period_end > now()), 0)"""


@dataclass(frozen=True, slots=True)
class Card:
    """What a viewer may see of another user. No user id, username or exact location."""

    public_id: str
    name: str
    age: int | None  # None = the owner hides it (profiles.show_age); never leaves SQL otherwise
    country: str | None
    city: str | None  # None = the owner hides it (profiles.show_city)
    bio: str


def _card(row: asyncpg.Record) -> Card:
    return Card(
        row["public_id"].hex, row["name"], row["age"], row["country"], row["city"], row["bio"]
    )


def parse_public_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(hex=raw) if len(raw) == 32 else None
    except ValueError:
        return None


# Advanced filters ($2.. are the viewer's filter values, NULL = no constraint). A profile whose
# owner hides a field never satisfies a filter on that field, so a filter can not be used to
# find out a value the owner chose not to show (an absent result says nothing about the value).
_FILTERS = """
          AND ($2::int  IS NULL OR (p.show_age    AND p.age >= $2))
          AND ($3::int  IS NULL OR (p.show_age    AND p.age <= $3))
          AND ($4::text IS NULL OR (p.show_gender AND p.gender = $4))
          AND ($5::text IS NULL OR (p.show_city   AND lower(p.city) = lower($5)))
          AND ($6::text IS NULL OR p.looking_for = $6
                                OR (p.looking_for = 'any' AND $6 <> 'any'))"""

# Active profile boost: ends_at is compared with PostgreSQL's clock, so an expired boost stops
# counting by itself.
_BOOSTED = """EXISTS (SELECT 1 FROM profile_boosts bo
                       WHERE bo.user_id = u.id AND bo.ends_at > now())"""


async def next_candidate(db: Db, uid: int, filters=None) -> Card | None:
    """Next profile for `uid`: eligible, not self, not blocked either way, not swiped yet, and
    (when the viewer is entitled to advanced filters) matching their filters.

    Order: people who already liked `uid`, then an active profile boost, then the subscription
    ranking level, then the most recently active. Boost only reorders what is already eligible;
    it never lifts a block, privacy switch or ban.
    """
    row = await db.fetchrow(
        f"""
        SELECT p.public_id, p.name,
               CASE WHEN p.show_age  THEN p.age  END AS age, p.country,
               CASE WHEN p.show_city THEN p.city END AS city, p.bio
        FROM users u JOIN profiles p ON p.user_id = u.id
        WHERE u.id <> $1 AND {_ELIGIBLE}
          AND {_NOT_BLOCKED.format(me="$1")}
          AND NOT EXISTS (SELECT 1 FROM swipes s WHERE s.swiper_id = $1 AND s.target_id = u.id)
          {_FILTERS}
        ORDER BY EXISTS (SELECT 1 FROM swipes l
                         WHERE l.swiper_id = u.id AND l.target_id = $1 AND l.action = 'like') DESC,
                 {_BOOSTED} DESC,
                 {_PRIORITY.format(u="u")} DESC,
                 u.last_seen_at DESC, u.id
        LIMIT 1
        """,  # noqa: S608 - constant fragments, no user input
        uid,
        *(filters.sql_args() if filters else (None,) * 5),
    )
    return _card(row) if row else None


async def viewer_is_eligible(db: Db, uid: int) -> bool:
    return bool(
        await db.fetchval(
            f"SELECT 1 FROM users u JOIN profiles p ON p.user_id = u.id "  # noqa: S608
            f"WHERE u.id = $1 AND {_ELIGIBLE}",
            uid,
        )
    )


async def user_id_of(db: Db, public_id: uuid.UUID) -> int | None:
    """User behind a profile handle, with no eligibility check (used to block / report)."""
    return await db.fetchval("SELECT user_id FROM profiles WHERE public_id = $1", public_id)


async def resolve_target(db: Db, viewer: int, public_id: uuid.UUID) -> int | None:
    """User id behind a profile handle, only if that user is currently eligible and not blocked
    in either direction. Self resolves to None."""
    return await db.fetchval(
        f"""
        SELECT u.id FROM users u JOIN profiles p ON p.user_id = u.id
        WHERE p.public_id = $2 AND u.id <> $1 AND {_ELIGIBLE}
          AND {_NOT_BLOCKED.format(me="$1")}
        """,  # noqa: S608 - constant fragments, no user input
        viewer,
        public_id,
    )


async def is_self(db: Db, viewer: int, public_id: uuid.UUID) -> bool:
    return bool(
        await db.fetchval(
            "SELECT 1 FROM profiles WHERE public_id = $1 AND user_id = $2", public_id, viewer
        )
    )


async def lock_pair(conn: asyncpg.Connection, a: int, b: int) -> None:
    """Serialise swipes between the same two users (transaction-scoped). Without it, two
    simultaneous likes could each miss the other's uncommitted row and no match would exist."""
    lo, hi = min(a, b), max(a, b)
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'swipe:' || ($1::bigint)::text || ':' || ($2::bigint)::text, 0))",
        lo,
        hi,
    )


async def insert_swipe(db: Db, swiper: int, target: int, action: str) -> bool:
    """True if a new row was created; an existing swipe is never changed or duplicated."""
    return bool(
        await db.fetchval(
            "INSERT INTO swipes (swiper_id, target_id, action) VALUES ($1, $2, $3) "
            "ON CONFLICT (swiper_id, target_id) DO NOTHING RETURNING 1",
            swiper,
            target,
            action,
        )
    )


async def has_liked(db: Db, swiper: int, target: int) -> bool:
    return bool(
        await db.fetchval(
            "SELECT 1 FROM swipes WHERE swiper_id = $1 AND target_id = $2 AND action = 'like'",
            swiper,
            target,
        )
    )


async def create_match(db: Db, a: int, b: int) -> bool:
    """Insert the canonical (low, high) pair. False if the match already existed."""
    return bool(
        await db.fetchval(
            "INSERT INTO matches (user_a_id, user_b_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            min(a, b),
            max(a, b),
        )
    )


async def card_of(db: Db, uid: int) -> Card | None:
    row = await db.fetchrow(
        "SELECT public_id, name, CASE WHEN show_age THEN age END AS age, country, "
        "CASE WHEN show_city THEN city END AS city, bio FROM profiles WHERE user_id = $1",
        uid,
    )
    return _card(row) if row else None


async def list_matches(db: Db, uid: int, limit: int) -> tuple[list[Card], int]:
    """(newest `limit` visible matches, total visible). A match whose other side was deleted,
    banned, made underage or blocked (either way) is not visible."""
    rows = await db.fetch(
        f"""
        SELECT p.public_id, p.name, CASE WHEN p.show_age THEN p.age END AS age, p.country,
               CASE WHEN p.show_city THEN p.city END AS city, p.bio,
               count(*) OVER () AS total
        FROM matches m
        JOIN users u ON u.id = CASE WHEN m.user_a_id = $1 THEN m.user_b_id ELSE m.user_a_id END
        JOIN profiles p ON p.user_id = u.id
        WHERE (m.user_a_id = $1 OR m.user_b_id = $1)
          AND {_ELIGIBLE}
          AND {_NOT_BLOCKED.format(me="$1")}
        ORDER BY m.created_at DESC, m.id DESC
        LIMIT $2
        """,  # noqa: S608 - constant fragments, no user input
        uid,
        limit,
    )
    return [_card(r) for r in rows], (rows[0]["total"] if rows else 0)


async def delete_for_user(db: Db, uid: int) -> None:
    """/deleteme: remove every swipe and match involving the user, in both directions."""
    await db.execute("DELETE FROM swipes WHERE swiper_id = $1 OR target_id = $1", uid)
    await db.execute("DELETE FROM matches WHERE user_a_id = $1 OR user_b_id = $1", uid)
    await db.execute("DELETE FROM browse_filters WHERE user_id = $1", uid)
    await db.execute("DELETE FROM profile_boosts WHERE user_id = $1", uid)

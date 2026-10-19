"""Browse / like / pass / matches. All SQL for P2 lives here.

Eligibility is re-checked in SQL on every call (active + 18+ confirmed + adult profile), so a
stale button can never act on a banned, deleted or underage user.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import asyncpg

from .repo_entitlements import FREE_PLAN
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


# The tier of a profile's OWNER: the highest `level` of the `plan_badge` entitlement (0 for Free,
# 1 premium, 2 pro, 3 vip). Same sources and filters as EntitlementService.get(uid, "plan_badge")
# (free plan + live subscriptions + live grants, disabled values ignored); a test keeps the two in
# step. `{uid}` is the SQL expression of the owner's user id.
TIER_LEVEL = f"""coalesce((
    SELECT max(coalesce((t.v ->> 'level')::int, 0))
    FROM (
        SELECT pe.value AS v
        FROM plans pl JOIN plan_entitlements pe
          ON pe.plan_id = pl.id AND pe.feature_key = 'plan_badge'
        WHERE pl.code = '{FREE_PLAN}' AND pl.is_active
        UNION ALL
        SELECT pe.value
        FROM subscriptions s
        JOIN plans pl ON pl.id = s.plan_id AND pl.is_active
        JOIN plan_entitlements pe ON pe.plan_id = pl.id AND pe.feature_key = 'plan_badge'
        WHERE s.user_id = {{uid}} AND s.status = 'active' AND s.started_at <= now()
          AND (s.current_period_end IS NULL OR s.current_period_end > now())
        UNION ALL
        SELECT g.value
        FROM entitlement_grants g
        WHERE g.user_id = {{uid}} AND g.feature_key = 'plan_badge' AND g.revoked_at IS NULL
          AND (g.expires_at IS NULL OR g.expires_at > now())
    ) t
    WHERE coalesce((t.v ->> 'enabled')::boolean, true)), 0)"""  # noqa: S608 - constants only

# Joined once per profile row as `ot.lvl`: the owner's tier. A viewer whose own tier level is
# STRICTLY higher ("outranks") may see the owner's age / gender / city even if the owner switched
# it off; the same or a lower tier gets exactly the owner's choice. Only these three profile
# fields: never a username or any contact (not selected anywhere in the card queries).
OWNER_TIER_JOIN = f"CROSS JOIN LATERAL (SELECT {TIER_LEVEL.format(uid='p.user_id')} AS lvl) ot"


def outranks(viewer_level: str) -> str:
    """SQL boolean: the viewer's tier level (a `$n` placeholder) is above the owner's."""
    return f"({viewer_level}::int > ot.lvl)"


# How many active users follow (= liked) the profile's owner: a number, never who.
FOLLOWERS = """(SELECT count(*) FROM swipes f JOIN users fu ON fu.id = f.swiper_id
                  AND fu.status = 'active'
                WHERE f.target_id = p.user_id AND f.action = 'like')"""


@dataclass(frozen=True, slots=True)
class Card:
    """What a viewer may see of another user. No user id, username or exact location."""

    public_id: str
    name: str
    age: int | None  # None = the owner hides it (show_age) and the viewer's tier is not above
    country: str | None
    city: str | None  # None = the owner hides it (show_city) and the viewer's tier is not above
    bio: str
    followers: int = 0  # followers of this profile (a count only)


def _card(row: asyncpg.Record) -> Card:
    return Card(
        row["public_id"].hex,
        row["name"],
        row["age"],
        row["country"],
        row["city"],
        row["bio"],
        row.get("followers") or 0,
    )


def parse_public_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(hex=raw) if len(raw) == 32 else None
    except ValueError:
        return None


# Advanced filters ($2.. are the viewer's filter values, NULL = no constraint; $7 is the viewer's
# tier level). A profile whose owner hides a field never satisfies a filter on that field unless the
# viewer's tier is above the owner's (then the viewer may see that field anyway), so a filter can
# not be used to find out a value the viewer is not allowed to see.
def can_see(field: str, viewer_level: str) -> str:
    """SQL boolean: the owner shows `field` (age | gender | city) OR the viewer outranks them."""
    return f"(p.show_{field} OR {outranks(viewer_level)})"


_FILTERS = f"""
          AND ($2::int  IS NULL OR ({can_see("age", "$7")}    AND p.age >= $2))
          AND ($3::int  IS NULL OR ({can_see("age", "$7")}    AND p.age <= $3))
          AND ($4::text IS NULL OR ({can_see("gender", "$7")} AND p.gender = $4))
          AND ($5::text IS NULL OR ({can_see("city", "$7")}   AND lower(p.city) = lower($5)))
          AND ($6::text IS NULL OR p.looking_for = $6
                                OR (p.looking_for = 'any' AND $6 <> 'any'))"""

# Active profile boost: ends_at is compared with PostgreSQL's clock, so an expired boost stops
# counting by itself.
_BOOSTED = """EXISTS (SELECT 1 FROM profile_boosts bo
                       WHERE bo.user_id = u.id AND bo.ends_at > now())"""


async def next_candidate(db: Db, uid: int, filters=None, viewer_level: int = 0) -> Card | None:
    """Next profile for `uid`: eligible, not self, not blocked either way, not swiped yet, and
    (when the viewer is entitled to advanced filters) matching their filters.

    Order: people who already liked `uid`, then an active profile boost, then the subscription
    ranking level, then the most recently active. Boost only reorders what is already eligible;
    it never lifts a block or ban. `viewer_level` is the viewer's tier level (plan_badge): a
    profile's hidden age / city is shown only to a viewer of a strictly higher tier.
    """
    row = await db.fetchrow(
        f"""
        SELECT p.public_id, p.name,
               CASE WHEN {can_see("age", "$7")}  THEN p.age  END AS age, p.country,
               CASE WHEN {can_see("city", "$7")} THEN p.city END AS city, p.bio,
               {FOLLOWERS} AS followers
        FROM users u JOIN profiles p ON p.user_id = u.id
        {OWNER_TIER_JOIN}
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
        viewer_level,
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


async def list_matches(
    db: Db, uid: int, limit: int, viewer_level: int = 0
) -> tuple[list[Card], int]:
    """(newest `limit` visible matches, total visible). A match whose other side was deleted,
    banned, made underage or blocked (either way) is not visible."""
    rows = await db.fetch(
        f"""
        SELECT p.public_id, p.name, CASE WHEN {can_see("age", "$3")} THEN p.age END AS age,
               p.country, CASE WHEN {can_see("city", "$3")} THEN p.city END AS city, p.bio,
               {FOLLOWERS} AS followers, count(*) OVER () AS total
        FROM matches m
        JOIN users u ON u.id = CASE WHEN m.user_a_id = $1 THEN m.user_b_id ELSE m.user_a_id END
        JOIN profiles p ON p.user_id = u.id
        {OWNER_TIER_JOIN}
        WHERE (m.user_a_id = $1 OR m.user_b_id = $1)
          AND {_ELIGIBLE}
          AND {_NOT_BLOCKED.format(me="$1")}
        ORDER BY m.created_at DESC, m.id DESC
        LIMIT $2
        """,  # noqa: S608 - constant fragments, no user input
        uid,
        limit,
        viewer_level,
    )
    return [_card(r) for r in rows], (rows[0]["total"] if rows else 0)


async def delete_for_user(db: Db, uid: int) -> None:
    """/deleteme: remove every swipe and match involving the user, in both directions."""
    await db.execute("DELETE FROM swipes WHERE swiper_id = $1 OR target_id = $1", uid)
    await db.execute("DELETE FROM matches WHERE user_a_id = $1 OR user_b_id = $1", uid)
    await db.execute("DELETE FROM browse_filters WHERE user_id = $1", uid)
    await db.execute("DELETE FROM profile_boosts WHERE user_id = $1", uid)

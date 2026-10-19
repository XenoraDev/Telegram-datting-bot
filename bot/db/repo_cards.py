"""Partner cards (P7): what one user may see of another.

The visibility decision is made INSIDE the query: a protected value is only selected when the
owner allows it OR the viewer's tier is strictly above the owner's, AND the viewer is entitled
(`CASE WHEN (show_x OR viewer_tier > owner_tier) AND $viewer_can THEN x END`), so a locked or hidden
value never leaves PostgreSQL, let alone reaches Telegram, a log or a callback. Only age, gender and
city are ever selected here: no username and no contact detail.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from .repo_browse import _ELIGIBLE, _NOT_BLOCKED, FOLLOWERS, OWNER_TIER_JOIN, can_see, outranks
from .repo_users import Db


def _fields(age: str, gender: str, city: str, level: str) -> str:
    """Column list; the first three arguments are the $n placeholders of the viewer's entitlements,
    `level` the placeholder of the viewer's tier level (needs OWNER_TIER_JOIN in the FROM)."""
    return f"""
    p.user_id, p.public_id, p.name, p.looking_for, p.bio, p.country,
    p.show_age, p.show_gender, p.show_city, {outranks(level)} AS viewer_outranks,
    CASE WHEN {can_see("age", level)}    AND {age}::bool    THEN p.age    END AS age,
    CASE WHEN {can_see("gender", level)} AND {gender}::bool THEN p.gender END AS gender,
    CASE WHEN {can_see("city", level)}   AND {city}::bool   THEN p.city   END AS city,
    {FOLLOWERS} AS followers
"""


@dataclass(frozen=True, slots=True)
class CardRow:
    user_id: int
    public_id: str
    name: str
    looking_for: str
    bio: str
    country: str | None
    owner_shows_age: bool
    owner_shows_gender: bool
    owner_shows_city: bool
    viewer_outranks: bool  # the viewer's tier level is strictly above the owner's
    age: int | None  # None unless (owner allows OR the viewer outranks) AND the viewer is entitled
    gender: str | None
    city: str | None
    followers: int  # a count of the profile's followers, never who


def _row(r: asyncpg.Record) -> CardRow:
    return CardRow(
        r["user_id"],
        r["public_id"].hex,
        r["name"],
        r["looking_for"],
        r["bio"],
        r["country"],
        r["show_age"],
        r["show_gender"],
        r["show_city"],
        r["viewer_outranks"],
        r["age"],
        r["gender"],
        r["city"],
        r["followers"],
    )


async def one(
    db: Db, partner: int, can_age: bool, can_gender: bool, can_city: bool, viewer_level: int = 0
) -> CardRow | None:
    row = await db.fetchrow(
        f"SELECT {_fields('$2', '$3', '$4', '$5')} FROM profiles p {OWNER_TIER_JOIN} "  # noqa: S608
        "WHERE p.user_id = $1",
        partner,
        can_age,
        can_gender,
        can_city,
        viewer_level,
    )
    return _row(row) if row else None


async def matches(
    db: Db,
    viewer: int,
    can_age: bool,
    can_gender: bool,
    can_city: bool,
    limit: int,
    viewer_level: int = 0,
) -> tuple[list[CardRow], int]:
    """The viewer's newest visible matches (same visibility rules as P2) and the total."""
    rows = await db.fetch(
        f"""
        SELECT {_fields("$3", "$4", "$5", "$6")},
               count(*) OVER () AS total
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
        viewer,
        limit,
        can_age,
        can_gender,
        can_city,
        viewer_level,
    )
    return [_row(r) for r in rows], (rows[0]["total"] if rows else 0)


_LIKERS_FROM = f"""
    FROM swipes l
    JOIN users u ON u.id = l.swiper_id
    JOIN profiles p ON p.user_id = u.id
    {OWNER_TIER_JOIN}
    WHERE l.target_id = $1 AND l.action = 'like'
      AND {_ELIGIBLE}
      AND {_NOT_BLOCKED.format(me="$1")}
      AND NOT EXISTS (SELECT 1 FROM swipes mine WHERE mine.swiper_id = $1 AND mine.target_id = u.id)
"""  # noqa: S608 - constant fragments, no user input


async def count_likers(db: Db, viewer: int) -> int:
    """People who liked the viewer and are still waiting for an answer (a number, no identity)."""
    return await db.fetchval(f"SELECT count(*) {_LIKERS_FROM}", viewer)  # noqa: S608


async def likers(
    db: Db,
    viewer: int,
    can_age: bool,
    can_gender: bool,
    can_city: bool,
    limit: int,
    viewer_level: int = 0,
) -> list[CardRow]:
    rows = await db.fetch(
        f"SELECT {_fields('$3', '$4', '$5', '$6')} {_LIKERS_FROM} "  # noqa: S608 - constant fragments
        "ORDER BY l.created_at DESC LIMIT $2",
        viewer,
        limit,
        can_age,
        can_gender,
        can_city,
        viewer_level,
    )
    return [_row(r) for r in rows]

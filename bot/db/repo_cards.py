"""Partner cards (P7): what one user may see of another.

The visibility decision is made INSIDE the query: a protected value is only selected when the
owner allows it AND the viewer is entitled (`CASE WHEN show_x AND $viewer_can THEN x END`), so a
locked or hidden value never leaves PostgreSQL, let alone reaches Telegram, a log or a callback.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from .repo_browse import _ELIGIBLE, _NOT_BLOCKED
from .repo_users import Db


def _fields(age: str, gender: str, city: str) -> str:
    """Column list; the three arguments are the $n placeholders of the viewer's entitlements."""
    return f"""
    p.user_id, p.public_id, p.name, p.looking_for, p.bio, p.country,
    p.show_age, p.show_gender, p.show_city,
    CASE WHEN p.show_age    AND {age}::bool    THEN p.age    END AS age,
    CASE WHEN p.show_gender AND {gender}::bool THEN p.gender END AS gender,
    CASE WHEN p.show_city   AND {city}::bool   THEN p.city   END AS city
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
    age: int | None  # None unless the owner allows it AND the viewer is entitled
    gender: str | None
    city: str | None


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
        r["age"],
        r["gender"],
        r["city"],
    )


async def one(
    db: Db, partner: int, can_age: bool, can_gender: bool, can_city: bool
) -> CardRow | None:
    row = await db.fetchrow(
        f"SELECT {_fields('$2', '$3', '$4')} FROM profiles p WHERE p.user_id = $1",  # noqa: S608
        partner,
        can_age,
        can_gender,
        can_city,
    )
    return _row(row) if row else None


async def matches(
    db: Db, viewer: int, can_age: bool, can_gender: bool, can_city: bool, limit: int
) -> tuple[list[CardRow], int]:
    """The viewer's newest visible matches (same visibility rules as P2) and the total."""
    rows = await db.fetch(
        f"""
        SELECT {_fields("$3", "$4", "$5")},
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
        viewer,
        limit,
        can_age,
        can_gender,
        can_city,
    )
    return [_row(r) for r in rows], (rows[0]["total"] if rows else 0)


_LIKERS_FROM = f"""
    FROM swipes l
    JOIN users u ON u.id = l.swiper_id
    JOIN profiles p ON p.user_id = u.id
    WHERE l.target_id = $1 AND l.action = 'like'
      AND {_ELIGIBLE}
      AND {_NOT_BLOCKED.format(me="$1")}
      AND NOT EXISTS (SELECT 1 FROM swipes mine WHERE mine.swiper_id = $1 AND mine.target_id = u.id)
"""  # noqa: S608 - constant fragments, no user input


async def count_likers(db: Db, viewer: int) -> int:
    """People who liked the viewer and are still waiting for an answer (a number, no identity)."""
    return await db.fetchval(f"SELECT count(*) {_LIKERS_FROM}", viewer)  # noqa: S608


async def likers(
    db: Db, viewer: int, can_age: bool, can_gender: bool, can_city: bool, limit: int
) -> list[CardRow]:
    rows = await db.fetch(
        f"SELECT {_fields('$3', '$4', '$5')} {_LIKERS_FROM} "  # noqa: S608 - constant fragments
        "ORDER BY l.created_at DESC LIMIT $2",
        viewer,
        limit,
        can_age,
        can_gender,
        can_city,
    )
    return [_row(r) for r in rows]

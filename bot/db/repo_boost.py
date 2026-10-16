"""Profile boost state: one row per user in `profile_boosts` (SQL only, all parameterised).

Every time comparison uses PostgreSQL's clock (`now()`), so a boost expires by itself and the
single-statement activation is atomic under concurrency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .repo_users import Db

_ELIGIBLE = "u.status = 'active' AND u.age_confirmed_at IS NOT NULL AND p.age >= 18"


@dataclass(frozen=True, slots=True)
class BoostRow:
    started_at: datetime
    ends_at: datetime
    cooldown_until: datetime
    active: bool  # ends_at is still in the future (decided by PostgreSQL)
    cooling_down: bool  # a new boost can not start yet
    seconds_left: int = 0  # until ends_at (PostgreSQL clock), 0 when over


async def get(db: Db, uid: int) -> BoostRow | None:
    row = await db.fetchrow(
        "SELECT started_at, ends_at, cooldown_until, ends_at > now() AS active, "
        "cooldown_until > now() AS cooling_down, "
        "greatest(extract(epoch FROM ends_at - now()), 0)::int AS seconds_left "
        "FROM profile_boosts WHERE user_id = $1",
        uid,
    )
    return BoostRow(**dict(row)) if row else None


async def activate(db: Db, uid: int, minutes: int, cooldown_hours: int) -> BoostRow | None:
    """Start a boost if the user is eligible and no boost / cooldown is running. Returns the new
    row, or None when it was refused. ONE statement: the primary key serialises concurrent
    attempts, and the loser re-evaluates the WHERE against the winner's row and gets nothing."""
    row = await db.fetchrow(
        f"""
        INSERT INTO profile_boosts (user_id, started_at, ends_at, cooldown_until)
        SELECT u.id, now(), now() + make_interval(mins => $2),
               now() + make_interval(hours => $3)
        FROM users u JOIN profiles p ON p.user_id = u.id
        WHERE u.id = $1 AND {_ELIGIBLE}
        ON CONFLICT (user_id) DO UPDATE
            SET started_at = EXCLUDED.started_at, ends_at = EXCLUDED.ends_at,
                cooldown_until = EXCLUDED.cooldown_until
            WHERE profile_boosts.cooldown_until <= now()
        RETURNING started_at, ends_at, cooldown_until, true AS active, true AS cooling_down,
                  greatest(extract(epoch FROM ends_at - now()), 0)::int AS seconds_left
        """,  # noqa: S608 - constant fragment, no user input
        uid,
        minutes,
        cooldown_hours,
    )
    return BoostRow(**dict(row)) if row else None


async def viewer_is_eligible(db: Db, uid: int) -> bool:
    return bool(
        await db.fetchval(
            f"SELECT 1 FROM users u JOIN profiles p ON p.user_id = u.id "  # noqa: S608
            f"WHERE u.id = $1 AND {_ELIGIBLE}",
            uid,
        )
    )

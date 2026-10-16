"""Advanced browse filters of a user: plain columns in `browse_filters`, one row per user."""

from __future__ import annotations

from dataclasses import dataclass

from .repo_users import Db


@dataclass(frozen=True, slots=True)
class Filters:
    age_min: int | None = None
    age_max: int | None = None
    gender: str | None = None
    city: str | None = None
    looking_for: str | None = None

    @property
    def is_empty(self) -> bool:
        return self == Filters()

    def sql_args(self) -> tuple:
        """Positional values for the `_FILTERS` fragment of repo_browse ($2..$6)."""
        return (self.age_min, self.age_max, self.gender, self.city, self.looking_for)


async def get(db: Db, uid: int) -> Filters:
    row = await db.fetchrow(
        "SELECT age_min, age_max, gender, city, looking_for FROM browse_filters WHERE user_id = $1",
        uid,
    )
    return Filters(**dict(row)) if row else Filters()


# One fixed statement per filter field: a column name never comes from a caller.
_SET = {
    "age": (
        "INSERT INTO browse_filters (user_id, age_min, age_max) VALUES ($1, $2, $3) "
        "ON CONFLICT (user_id) DO UPDATE SET age_min = EXCLUDED.age_min, "
        "age_max = EXCLUDED.age_max, updated_at = now()"
    ),
    "gender": (
        "INSERT INTO browse_filters (user_id, gender) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET gender = EXCLUDED.gender, updated_at = now()"
    ),
    "city": (
        "INSERT INTO browse_filters (user_id, city) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET city = EXCLUDED.city, updated_at = now()"
    ),
    "looking_for": (
        "INSERT INTO browse_filters (user_id, looking_for) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET looking_for = EXCLUDED.looking_for, "
        "updated_at = now()"
    ),
}
FIELDS = tuple(_SET)


async def set_field(db: Db, uid: int, field: str, *values: object) -> None:
    """Set one filter field (age takes two values); None values clear it."""
    await db.execute(_SET[field], uid, *values)


async def reset(db: Db, uid: int) -> None:
    await db.execute("DELETE FROM browse_filters WHERE user_id = $1", uid)

"""profiles table access."""

from __future__ import annotations

import asyncpg

from .repo_users import Db


async def get(db: Db, uid: int) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT * FROM profiles WHERE user_id = $1", uid)


async def upsert(
    db: Db,
    uid: int,
    *,
    name: str,
    age: int,
    gender: str,
    looking_for: str,
    city: str,
    bio: str,
    country: str | None = None,
) -> bool:
    """Insert/update a profile. The SELECT ... FROM users makes it impossible to create a
    profile for a user who has not confirmed 18+ or who is blocked/banned/deleted."""
    return bool(
        await db.fetchval(
            """
            INSERT INTO profiles (user_id, name, age, gender, looking_for, city, bio, country)
            SELECT u.id, $2, $3, $4, $5, $6, $7, $8 FROM users u
            WHERE u.id = $1 AND u.age_confirmed_at IS NOT NULL
              AND u.status IN ('onboarding', 'active')
            ON CONFLICT (user_id) DO UPDATE SET
                name = EXCLUDED.name, age = EXCLUDED.age, gender = EXCLUDED.gender,
                looking_for = EXCLUDED.looking_for, city = EXCLUDED.city, bio = EXCLUDED.bio,
                country = EXCLUDED.country,
                updated_at = now()
            RETURNING 1
            """,
            uid,
            name,
            age,
            gender,
            looking_for,
            city,
            bio,
            country,
        )
    )


async def delete(db: Db, uid: int) -> None:
    await db.execute("DELETE FROM profiles WHERE user_id = $1", uid)


# ---------------------------------------------------------------- partner-field privacy (P7)
# The owner's switches. Fixed statements per field: a field name never reaches SQL text.
_TOGGLE = {
    "age": "UPDATE profiles SET show_age = NOT show_age WHERE user_id = $1 RETURNING show_age",
    "gender": (
        "UPDATE profiles SET show_gender = NOT show_gender WHERE user_id = $1 RETURNING show_gender"
    ),
    "city": "UPDATE profiles SET show_city = NOT show_city WHERE user_id = $1 RETURNING show_city",
}
PRIVACY_FIELDS = tuple(_TOGGLE)


async def get_privacy(db: Db, uid: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        "SELECT show_age, show_gender, show_city FROM profiles WHERE user_id = $1", uid
    )


async def toggle_privacy(db: Db, uid: int, field: str) -> bool | None:
    """Flip one switch; returns the new value, or None for an unknown field / no profile."""
    sql = _TOGGLE.get(field)
    return await db.fetchval(sql, uid) if sql else None

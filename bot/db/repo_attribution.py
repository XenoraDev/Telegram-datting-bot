"""Traffic attribution SQL (all parameterised): campaigns and each new user's first touch.

Nothing here reads message content, IPs or links; a start payload is only ever compared with the
random campaign codes in `campaigns`, and what a user sent is never stored.
"""

from __future__ import annotations

import asyncpg

from .repo_users import Db

CAMPAIGN_COLUMNS = "id, code, source, name, status, is_test, created_by, created_at"


async def campaign_by_code(db: Db, code: str) -> asyncpg.Record | None:
    return await db.fetchrow(f"SELECT {CAMPAIGN_COLUMNS} FROM campaigns WHERE code = $1", code)  # noqa: S608


async def insert_acquisition(
    db: Db, uid: int, kind: str, campaign_id: int | None, source: str | None
) -> bool:
    """First touch, once. False when the user already has one (it is never replaced)."""
    return bool(
        await db.fetchval(
            "INSERT INTO user_acquisition (user_id, kind, campaign_id, source) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (user_id) DO NOTHING RETURNING 1",
            uid,
            kind,
            campaign_id,
            source,
        )
    )


async def delete_acquisition(db: Db, uid: int) -> None:
    """Erase the user's attribution (account deletion). The only way a row ever changes."""
    await db.execute("DELETE FROM user_acquisition WHERE user_id = $1", uid)


async def create_campaign(
    db: Db, code: str, source: str, name: str, is_test: bool, admin_id: int
) -> asyncpg.Record:
    return await db.fetchrow(
        "INSERT INTO campaigns (code, source, name, is_test, created_by) "  # noqa: S608
        f"VALUES ($1, $2, $3, $4, $5) RETURNING {CAMPAIGN_COLUMNS}",
        code,
        source,
        name,
        is_test,
        admin_id,
    )


async def set_status(db: Db, campaign_id: int, status: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        "UPDATE campaigns SET status = $2 WHERE id = $1 AND status <> $2 "  # noqa: S608
        f"RETURNING {CAMPAIGN_COLUMNS}",
        campaign_id,
        status,
    )


async def get_campaign(db: Db, campaign_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(f"SELECT {CAMPAIGN_COLUMNS} FROM campaigns WHERE id = $1", campaign_id)  # noqa: S608


async def list_campaigns(db: Db) -> list[asyncpg.Record]:
    return await db.fetch(
        f"SELECT {CAMPAIGN_COLUMNS} FROM campaigns ORDER BY created_at DESC, id DESC"  # noqa: S608
    )


async def sources(db: Db) -> list[str]:
    return [r["source"] for r in await db.fetch("SELECT DISTINCT source FROM campaigns ORDER BY 1")]

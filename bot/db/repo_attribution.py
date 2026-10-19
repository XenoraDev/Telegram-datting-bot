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


async def funnel_rows(
    db: Db,
    since,
    until,
    exclude_ids: list[int],
    include_tests: bool,
) -> list[asyncpg.Record]:
    """One aggregate row per (kind, source, campaign) for accounts first seen in [since, until).

    Counts only: no user id leaves the query. "Ever" semantics, like the existing funnel (the
    state of the accounts now). Revenue = approved payments that were not later revoked.
    Load-test users (id <= 0), the admins' own accounts and (unless asked) test campaigns are out.
    """
    return await db.fetch(
        """
        WITH per_user AS (
            SELECT ua.kind, ua.source, ua.campaign_id,
                   (u.age_confirmed_at IS NOT NULL) AS age,
                   EXISTS (SELECT 1 FROM profiles p WHERE p.user_id = u.id) AS profile,
                   EXISTS (SELECT 1 FROM chat_session_members m WHERE m.user_id = u.id) AS chat,
                   EXISTS (SELECT 1 FROM swipes s
                            WHERE s.swiper_id = u.id AND s.action = 'like') AS follow,
                   EXISTS (SELECT 1 FROM matches mt
                            WHERE mt.user_a_id = u.id OR mt.user_b_id = u.id) AS matched,
                   EXISTS (SELECT 1 FROM payments p WHERE p.user_id = u.id) AS pay_started,
                   (SELECT coalesce(sum(p.amount_minor), 0) FROM payments p
                     WHERE p.user_id = u.id AND p.status = 'approved'
                       AND NOT EXISTS (SELECT 1 FROM admin_actions a
                                        WHERE a.action = 'sub_revoke' AND a.payment_id = p.id)
                   ) AS revenue
            FROM user_acquisition ua
            JOIN users u ON u.id = ua.user_id
            LEFT JOIN campaigns c ON c.id = ua.campaign_id
            WHERE u.id > 0 AND NOT (u.id = ANY($3::bigint[]))
              AND ua.acquired_at >= $1 AND ua.acquired_at < $2
              AND ($4 OR c.is_test IS NOT TRUE)
        )
        SELECT kind, source, campaign_id,
               count(*) AS acquired,
               count(*) FILTER (WHERE age) AS age,
               count(*) FILTER (WHERE profile) AS profile,
               count(*) FILTER (WHERE chat) AS chat,
               count(*) FILTER (WHERE follow) AS follow,
               count(*) FILTER (WHERE matched) AS matched,
               count(*) FILTER (WHERE pay_started) AS pay_started,
               count(*) FILTER (WHERE revenue > 0) AS paid,
               coalesce(sum(revenue), 0)::bigint AS revenue
        FROM per_user
        GROUP BY kind, source, campaign_id
        """,
        since,
        until,
        exclude_ids,
        include_tests,
    )


async def untracked_accounts(db: Db, exclude_ids: list[int]) -> int:
    """Live accounts that have no first touch: they were created before tracking began."""
    return await db.fetchval(
        "SELECT count(*) FROM users u WHERE u.id > 0 AND NOT (u.id = ANY($1::bigint[])) "
        "AND u.status IN ('onboarding', 'active') "
        "AND NOT EXISTS (SELECT 1 FROM user_acquisition ua WHERE ua.user_id = u.id)",
        exclude_ids,
    )

"""SQL for support tickets. Every user-side query carries the user id in its WHERE, so a ticket is
only ever found for its owner; admin-side queries are gated by the service."""

from __future__ import annotations

import asyncpg

from .repo_users import Db

COLUMNS = (
    "t.id, t.user_id, t.category, t.status, t.closed_by, t.created_at, t.updated_at, t.closed_at"
)
MAX_OPEN = 3


async def lock_user(db: Db, uid: int) -> bool:
    """Serialise this user's ticket writes (also guards the open-ticket cap)."""
    return bool(await db.fetchval("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", uid))


async def count_unfinished(db: Db, uid: int) -> int:
    return await db.fetchval(
        "SELECT count(*) FROM support_tickets WHERE user_id = $1 AND status <> 'closed'", uid
    )


async def create(db: Db, uid: int, category: str) -> int:
    return await db.fetchval(
        "INSERT INTO support_tickets (user_id, category) VALUES ($1, $2) RETURNING id",
        uid,
        category,
    )


async def add_message(
    db: Db, ticket_id: int, sender: str, text: str, admin_id: int | None = None
) -> None:
    await db.execute(
        "INSERT INTO support_messages (ticket_id, sender, admin_id, ticket_text) "
        "VALUES ($1, $2, $3, $4)",
        ticket_id,
        sender,
        admin_id,
        text,
    )


async def count_messages(db: Db, ticket_id: int) -> int:
    return await db.fetchval(
        "SELECT count(*) FROM support_messages WHERE ticket_id = $1", ticket_id
    )


async def get(db: Db, ticket_id: int, *, lock: bool = False) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"SELECT {COLUMNS} FROM support_tickets t WHERE t.id = $1"  # noqa: S608 - constant columns
        + (" FOR UPDATE" if lock else ""),
        ticket_id,
    )


async def get_owned(
    db: Db, uid: int, ticket_id: int, *, lock: bool = False
) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"SELECT {COLUMNS} FROM support_tickets t WHERE t.id = $1 AND t.user_id = $2"  # noqa: S608
        + (" FOR UPDATE" if lock else ""),
        ticket_id,
        uid,
    )


async def set_status(db: Db, ticket_id: int, status: str, closed_by: str | None = None) -> None:
    await db.execute(
        "UPDATE support_tickets SET status = $2, closed_by = $3, updated_at = now(), "
        "closed_at = CASE WHEN $2 = 'closed' THEN now() END WHERE id = $1",
        ticket_id,
        status,
        closed_by,
    )


async def list_for_user(db: Db, uid: int, limit: int) -> list[asyncpg.Record]:
    return await db.fetch(
        f"SELECT {COLUMNS} FROM support_tickets t WHERE t.user_id = $1 "  # noqa: S608
        "ORDER BY t.id DESC LIMIT $2",
        uid,
        limit,
    )


async def messages(db: Db, ticket_id: int, limit: int | None = None) -> list[asyncpg.Record]:
    """Oldest first; with `limit`, the newest `limit` messages."""
    rows = await db.fetch(
        "SELECT id, sender, admin_id, ticket_text, created_at FROM support_messages "
        "WHERE ticket_id = $1 ORDER BY id DESC LIMIT $2",
        ticket_id,
        limit or 1000,
    )
    return rows[::-1]


_WHERE = "WHERE ($1::text IS NULL OR t.status = $1) "


async def count_tickets(db: Db, status: str | None) -> int:
    return await db.fetchval(
        "SELECT count(*) FROM support_tickets t " + _WHERE,  # noqa: S608 - constant text
        status,
    )


async def list_tickets(db: Db, status: str | None, limit: int, offset: int) -> list[asyncpg.Record]:
    return await db.fetch(
        f"SELECT {COLUMNS}, "  # noqa: S608 - constant text
        "(SELECT count(*) FROM support_messages m WHERE m.ticket_id = t.id) AS n_messages "
        "FROM support_tickets t " + _WHERE + "ORDER BY t.updated_at DESC, t.id DESC "
        "LIMIT $2 OFFSET $3",
        status,
        limit,
        offset,
    )


async def counts(db: Db) -> asyncpg.Record:
    return await db.fetchrow(
        "SELECT count(*) FILTER (WHERE status = 'open') AS open, "
        "count(*) FILTER (WHERE status = 'answered') AS answered FROM support_tickets"
    )


async def delete_for_user(db: Db, uid: int) -> None:
    """/deleteme: the user's tickets and (by cascade) everything they wrote or were answered."""
    await db.execute("DELETE FROM support_tickets WHERE user_id = $1", uid)


async def audit(db: Db, admin_id: int, action: str, *, target: int, detail: str) -> None:
    await db.execute(
        "INSERT INTO admin_actions (admin_id, action, target_user_id, detail) "
        "VALUES ($1, $2, $3, $4)",
        admin_id,
        action,
        target,
        detail,
    )

"""In-bot admin (P3): statistics, user lookup, ban / unban and the reports queue.

Authorization is checked here as well as in the handlers: every public method that acts takes
the acting Telegram id and refuses (`denied`) unless it is in ADMIN_IDS. Every moderation action
is written to the append-only `admin_actions` table in the same transaction as the action.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from ..db import repo_admin, repo_chat, repo_users
from .chat_relay import ContextBuffer
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)

REPORTS_PAGE = 5
MAX_USER_ID = 2**53  # Telegram ids are far below this; keeps input inside a safe integer range


def parse_user_id(raw: str) -> int | None:
    """A Telegram user id typed by an admin: 1-15 ASCII digits, nothing else."""
    s = raw.strip()
    if not (s.isascii() and s.isdecimal() and 1 <= len(s) <= 15):
        return None
    value = int(s)
    return value if 1 <= value < MAX_USER_ID else None


@dataclass(frozen=True, slots=True)
class ActionResult:
    # ok | denied | rate_limited | not_found | invalid_state | protected | already_handled
    kind: str
    partner_id: int | None = None  # chat partner who has to be told the chat ended
    detail: str | None = None  # e.g. the status after an unban


class AdminService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        buffer: ContextBuffer,
        limiter: RateLimiter,
        admin_ids: frozenset[int] = frozenset(),
    ) -> None:
        self._pool = pool
        self._buffer = buffer
        self._limiter = limiter
        self._admin_ids = admin_ids

    def is_admin(self, uid: int) -> bool:
        return uid in self._admin_ids

    # ---------------------------------------------------------------- read-only views

    async def stats(self, admin_id: int) -> asyncpg.Record | None:
        return await repo_admin.stats(self._pool) if self.is_admin(admin_id) else None

    async def user(self, admin_id: int, uid: int) -> asyncpg.Record | None:
        if not self.is_admin(admin_id) or not 1 <= uid < MAX_USER_ID:
            return None
        return await repo_admin.user_overview(self._pool, uid)

    async def reports_page(self, admin_id: int, page: int) -> tuple[list[asyncpg.Record], int]:
        """(reports on this page, total open). Page numbers are clamped, never negative."""
        if not self.is_admin(admin_id):
            return [], 0
        total = await repo_admin.count_open_reports(self._pool)
        last = max(0, (total - 1) // REPORTS_PAGE)
        page = min(max(page, 0), last)
        rows = await repo_admin.list_open_reports(self._pool, REPORTS_PAGE, page * REPORTS_PAGE)
        return rows, total

    async def report(self, admin_id: int, report_id: int) -> asyncpg.Record | None:
        if not self.is_admin(admin_id) or not 1 <= report_id < MAX_USER_ID:
            return None
        return await repo_admin.get_report(self._pool, report_id)

    # ---------------------------------------------------------------- actions

    def _gate(self, admin_id: int) -> ActionResult | None:
        if not self.is_admin(admin_id):
            return ActionResult("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return ActionResult("rate_limited")
        return None

    async def ban(self, admin_id: int, uid: int, *, report_id: int | None = None) -> ActionResult:
        """Ban `uid`: ends their chat, leaves the queue, closes their open reports (if any)."""
        if (refused := self._gate(admin_id)) is not None:
            return refused
        if uid in self._admin_ids:
            return ActionResult("protected")  # admins (incl. oneself) can't be banned in the bot
        if not 1 <= uid < MAX_USER_ID:
            return ActionResult("not_found")
        async with self._pool.acquire() as conn, conn.transaction():
            await repo_chat.lock_matchmaking(conn)
            if await repo_users.get(conn, uid) is None:
                return ActionResult("not_found")
            if not await repo_admin.ban(conn, uid):
                return ActionResult("invalid_state")
            session_ids = await repo_chat.user_session_ids(conn, uid)
            ended = await repo_chat.end_active_session(conn, uid, "admin_ban")
            await repo_chat.leave_queue(conn, uid)
            closed = await repo_admin.action_open_reports_against(conn, uid)
            await repo_admin.audit(
                conn,
                admin_id,
                "report_ban" if report_id else "ban",
                target=uid,
                report_id=report_id,
                detail=f"closed {closed} open report(s)",
            )
        for sid in session_ids:
            self._buffer.drop(sid)
        log.info("admin ban applied")
        return ActionResult("ok", ended[1] if ended else None)

    async def unban(self, admin_id: int, uid: int) -> ActionResult:
        if (refused := self._gate(admin_id)) is not None:
            return refused
        if not 1 <= uid < MAX_USER_ID:
            return ActionResult("not_found")
        async with self._pool.acquire() as conn, conn.transaction():
            if await repo_users.get(conn, uid) is None:
                return ActionResult("not_found")
            new_status = await repo_admin.unban(conn, uid)
            if new_status is None:
                return ActionResult("invalid_state")  # not banned (underage / deleted stay so)
            await repo_admin.drop_ban_tombstone(conn, uid)
            await repo_admin.audit(conn, admin_id, "unban", target=uid, detail=new_status)
        log.info("admin unban applied")
        return ActionResult("ok", detail=new_status)

    async def close_report(self, admin_id: int, report_id: int, *, dismiss: bool) -> ActionResult:
        """Resolve (`actioned`) or dismiss an open report without banning anyone."""
        if (refused := self._gate(admin_id)) is not None:
            return refused
        if not 1 <= report_id < MAX_USER_ID:
            return ActionResult("not_found")
        async with self._pool.acquire() as conn, conn.transaction():
            report = await repo_admin.get_report(conn, report_id)
            if report is None:
                return ActionResult("not_found")
            status = "dismissed" if dismiss else "actioned"
            if not await repo_admin.set_report_status(conn, report_id, status):
                return ActionResult("already_handled")
            await repo_admin.audit(
                conn,
                admin_id,
                "report_dismiss" if dismiss else "report_resolve",
                target=report["reported_id"],
                report_id=report_id,
            )
        return ActionResult("ok")

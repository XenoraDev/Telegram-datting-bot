"""In-bot admin (P3): statistics, user lookup, ban / unban and the reports queue.

Authorization is checked here as well as in the handlers: every public method that acts takes
the acting Telegram id and refuses (`denied`) unless it is in ADMIN_IDS. Every moderation action
is written to the append-only `admin_actions` table in the same transaction as the action.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from ..db import repo_admin, repo_browse, repo_chat, repo_users
from .chat_relay import ContextBuffer
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)

REPORTS_PAGE = 5
WEB_PAGE_SIZE = 25
NOTE_MAX = 80  # admin reason typed in the web panel; goes into the 200 char audit detail
USER_STATUSES = ("onboarding", "active", "banned", "blocked_underage", "deleted")
REPORT_STATUSES = ("open", "actioned", "dismissed")
AUDIT_ACTIONS = (
    "ban",
    "unban",
    "report_resolve",
    "report_dismiss",
    "report_ban",
    "payment_approve",
    "payment_reject",
    "payment_config",
    "plan_save",
    "payment_auto_approve",
    "payment_auto_setting",
    # subscription management (written by the actions added in a later phase)
    "sub_grant",
    "sub_cancel",
    "sub_extend",
    "sub_revoke",
)
MAX_USER_ID = 2**53  # Telegram ids are far below this; keeps input inside a safe integer range


def parse_user_id(raw: str) -> int | None:
    """A Telegram user id typed by an admin: 1-15 ASCII digits, nothing else."""
    s = raw.strip()
    if not (s.isascii() and s.isdecimal() and 1 <= len(s) <= 15):
        return None
    value = int(s)
    return value if 1 <= value < MAX_USER_ID else None


def clean_note(raw: str | None) -> str | None:
    """Optional short reason: printable characters only, single line, bounded."""
    if not raw:
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in raw).split())
    return text[:NOTE_MAX] or None


@dataclass(frozen=True, slots=True)
class Page:
    rows: list[asyncpg.Record]
    total: int
    page: int  # 0-based, already clamped
    pages: int

    @property
    def has_prev(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return self.page + 1 < self.pages


def _page(total: int, requested: int) -> tuple[int, int]:
    pages = max(1, -(-total // WEB_PAGE_SIZE))
    return min(max(requested, 0), pages - 1), pages


@dataclass(frozen=True, slots=True)
class ActionResult:
    # ok | denied | rate_limited | not_found | invalid_state | protected | already_handled
    kind: str
    partner_id: int | None = None  # chat partner who has to be told the chat ended
    detail: str | None = None  # e.g. the status after an unban


def _ban_detail(closed: int, note: str | None) -> str:
    detail = f"closed {closed} open report(s)"
    return f"{detail}; reason: {note}" if note else detail


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

    # ---------------------------------------------------------------- web panel views (P5)
    # Same gate as above: a non-admin id gets nothing. Page numbers are clamped.

    async def users_page(
        self,
        admin_id: int,
        *,
        uid: int | None = None,
        public_id: uuid.UUID | None = None,
        status: str | None = None,
        page: int = 0,
    ) -> Page:
        if not self.is_admin(admin_id):
            return Page([], 0, 0, 1)
        if status not in USER_STATUSES:
            status = None
        if uid is not None and not 1 <= uid < MAX_USER_ID:
            return Page([], 0, 0, 1)
        total = await repo_admin.count_users(
            self._pool, uid=uid, public_id=public_id, status=status
        )
        page, pages = _page(total, page)
        rows = await repo_admin.list_users(
            self._pool,
            uid=uid,
            public_id=public_id,
            status=status,
            limit=WEB_PAGE_SIZE,
            offset=page * WEB_PAGE_SIZE,
        )
        return Page(rows, total, page, pages)

    async def user_detail(self, admin_id: int, uid: int) -> dict | None:
        """Overview + active restrictions + recent reports / audit entries about one user."""
        if (overview := await self.user(admin_id, uid)) is None:
            return None
        return {
            "user": overview,
            "restrictions": await repo_admin.user_restrictions(self._pool, uid),
            "subscriptions": await repo_admin.user_subscriptions(self._pool, uid),
            "reports": await repo_admin.reports_about(self._pool, uid),
            "audit": await repo_admin.list_audit(
                self._pool,
                action=None,
                actor=None,
                target=uid,
                since=None,
                until=None,
                limit=10,
                offset=0,
            ),
        }

    async def reports_list(
        self, admin_id: int, *, status: str | None = "open", page: int = 0
    ) -> Page:
        """status: open | actioned | dismissed, or None for all."""
        if not self.is_admin(admin_id):
            return Page([], 0, 0, 1)
        if status not in REPORT_STATUSES:
            status = None
        total = await repo_admin.count_reports(self._pool, status=status)
        page, pages = _page(total, page)
        rows = await repo_admin.list_reports(
            self._pool, status=status, limit=WEB_PAGE_SIZE, offset=page * WEB_PAGE_SIZE
        )
        return Page(rows, total, page, pages)

    async def audit_page(
        self,
        admin_id: int,
        *,
        action: str | None = None,
        actor: int | None = None,
        target: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        page: int = 0,
    ) -> Page:
        """Read-only view of the append-only audit log (nothing here can change it)."""
        if not self.is_admin(admin_id):
            return Page([], 0, 0, 1)
        if action not in AUDIT_ACTIONS:
            action = None
        filters = {
            "action": action,
            "actor": actor if actor is None or 0 < actor < MAX_USER_ID else -1,
            "target": target if target is None or 0 < target < MAX_USER_ID else -1,
            "since": since,
            "until": until,
        }
        total = await repo_admin.count_audit(self._pool, **filters)
        page, pages = _page(total, page)
        rows = await repo_admin.list_audit(
            self._pool, **filters, limit=WEB_PAGE_SIZE, offset=page * WEB_PAGE_SIZE
        )
        return Page(rows, total, page, pages)

    async def statistics(self, admin_id: int) -> dict | None:
        if not self.is_admin(admin_id):
            return None
        return {
            "stats": await repo_admin.stats(self._pool),
            "reports": await repo_admin.report_breakdown(self._pool),
            "audit": await repo_admin.audit_activity(self._pool),
        }

    async def find_profile(self, admin_id: int, raw: str) -> uuid.UUID | None:
        """Parse a public profile id typed by an admin (32 hex chars, dashes tolerated)."""
        if not self.is_admin(admin_id):
            return None
        return repo_browse.parse_public_id(raw.strip().replace("-", ""))

    # ---------------------------------------------------------------- actions

    def _gate(self, admin_id: int) -> ActionResult | None:
        if not self.is_admin(admin_id):
            return ActionResult("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return ActionResult("rate_limited")
        return None

    async def ban(
        self, admin_id: int, uid: int, *, report_id: int | None = None, note: str | None = None
    ) -> ActionResult:
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
                detail=_ban_detail(closed, clean_note(note)),
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

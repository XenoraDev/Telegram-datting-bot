"""Support tickets: a user writes to support, an admin answers from the web panel.

A ticket is `open` while it waits for support, `answered` while it waits for the user, `closed`
when finished (by either side). A user may have at most 3 unfinished tickets. The text a user
writes lives only in support_messages and goes with the account on /deleteme; the audit log keeps
"ticket #N" and the admin's id, never the text. Every user-side call is scoped to the caller's own
user id, so another user's ticket id finds nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from ..db import repo_payments, repo_tickets
from .admin import Page
from .payments import clean_text
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)

CATEGORIES = ("payment", "account", "chat", "report", "other")
STATUSES = ("open", "answered", "closed")
MAX_TEXT = 2000  # a user's message
MAX_REPLY = 800  # an admin's reply: a Hindi reply must fit the web form size limit once encoded
LIVE_MINUTES = 30  # live-reply mode lasts this long after it was switched on / the last message
MAX_MESSAGES = 40  # per ticket, so one ticket cannot grow without bound
LIST_LIMIT = 10
PAGE_SIZE = 25


@dataclass(frozen=True, slots=True)
class TicketResult:
    # ok | rate_limited | bad_category | empty | too_long | too_many | not_found | closed | full
    # | not_active | denied | no_live
    kind: str
    ticket_id: int | None = None
    user_id: int | None = None  # the ticket's owner, for notifying them after an admin action
    text: str | None = None  # the cleaned text actually stored (an admin reply, to deliver)
    message_id: int | None = None  # id of the message just stored (admin reply)


def _text(raw: str | None, limit: int) -> str | None:
    """Cleaned multi-line text, "" when empty, None when longer than the limit."""
    if len((raw or "").strip()) > limit:
        return None
    return clean_text(raw, limit, multiline=True)


class TicketService:
    def __init__(
        self, pool: asyncpg.Pool, limiter: RateLimiter, admin_ids: frozenset[int] = frozenset()
    ) -> None:
        self._pool = pool
        self._limiter = limiter
        self._admin_ids = admin_ids

    def is_admin(self, uid: int) -> bool:
        return uid in self._admin_ids

    # ============================================================ user side

    async def open(self, uid: int, category: str, text: str | None) -> TicketResult:
        if category not in CATEGORIES:
            return TicketResult("bad_category")
        body = _text(text, MAX_TEXT)
        if body is None:
            return TicketResult("too_long")
        if not body:
            return TicketResult("empty")
        if not self._limiter.allow("ticket_open", uid):
            return TicketResult("rate_limited")
        async with self._pool.acquire() as conn, conn.transaction():
            if not await repo_tickets.lock_user(conn, uid):
                return TicketResult("not_active")
            if await repo_payments.user_status_shared(conn, uid) != "active":
                return TicketResult("not_active")
            if await repo_tickets.count_unfinished(conn, uid) >= repo_tickets.MAX_OPEN:
                return TicketResult("too_many")
            tid = await repo_tickets.create(conn, uid, category)
            await repo_tickets.add_message(conn, tid, "user", body)
        log.info("support ticket opened")
        return TicketResult("ok", tid, uid)

    async def user_reply(self, uid: int, ticket_id: int, text: str | None) -> TicketResult:
        body = _text(text, MAX_TEXT)
        if body is None:
            return TicketResult("too_long")
        if not body:
            return TicketResult("empty")
        if not self._limiter.allow("ticket_message", uid):
            return TicketResult("rate_limited")
        async with self._pool.acquire() as conn, conn.transaction():
            ticket = await repo_tickets.get_owned(conn, uid, ticket_id, lock=True)
            return await self._append_user_message(conn, uid, ticket, ticket_id, body)

    @staticmethod
    async def _append_user_message(conn, uid: int, ticket, ticket_id: int, body: str):
        """The one place a user's message is added to a ticket (one-shot reply and live reply)."""
        if ticket is None:
            return TicketResult("not_found")
        if ticket["status"] == "closed":
            return TicketResult("closed", ticket_id)
        if await repo_tickets.count_messages(conn, ticket_id) >= MAX_MESSAGES:
            return TicketResult("full", ticket_id)
        await repo_tickets.add_message(conn, ticket_id, "user", body)
        await repo_tickets.set_status(conn, ticket_id, "open")
        return TicketResult("ok", ticket_id, uid)

    # ------------------------------------------------------------ live reply (after an answer)

    async def start_live(self, uid: int, ticket_id: int) -> TicketResult:
        """The user tapped "Reply to support" on one of THEIR tickets: from now on (for
        LIVE_MINUTES after each message) their plain messages go to this ticket only."""
        if not self._limiter.allow("ticket_message", uid):
            return TicketResult("rate_limited")
        async with self._pool.acquire() as conn, conn.transaction():
            ticket = await repo_tickets.get_owned(conn, uid, ticket_id, lock=True)
            if ticket is None:
                return TicketResult("not_found")
            if ticket["status"] == "closed":
                return TicketResult("closed", ticket_id)
            if not await repo_tickets.live_start(conn, uid, ticket_id, LIVE_MINUTES):
                return TicketResult("closed", ticket_id)
        return TicketResult("ok", ticket_id, uid)

    async def is_live(self, uid: int, ticket_id: int) -> bool:
        """Is THIS ticket the user's live target right now (so a new answer needs no button)?"""
        live = await repo_tickets.live_ticket(self._pool, uid)
        return live is not None and live["id"] == ticket_id

    async def stop_live(self, uid: int) -> int:
        """Leave live mode (a command, a menu button or the Stop button). Returns how many
        tickets were live (0 or 1)."""
        return await repo_tickets.live_clear(self._pool, uid)

    async def has_live(self, uid: int) -> bool:
        return await repo_tickets.live_ticket(self._pool, uid) is not None

    async def send_live(self, uid: int, text: str | None) -> TicketResult:
        """A plain message from a user in live mode. `no_live` = there is no live ticket (never
        started, expired, or the ticket was closed): the message is NOT a support message."""
        async with self._pool.acquire() as conn, conn.transaction():
            ticket = await repo_tickets.live_ticket(conn, uid, lock=True)
            if ticket is None:
                return TicketResult("no_live")
            body = _text(text, MAX_TEXT)
            if body is None:
                return TicketResult("too_long", ticket["id"])
            if not body:
                return TicketResult("empty", ticket["id"])
            if not self._limiter.allow("ticket_message", uid):
                return TicketResult("rate_limited", ticket["id"])
            res = await self._append_user_message(conn, uid, ticket, ticket["id"], body)
            if res.kind == "ok":
                await repo_tickets.live_extend(conn, ticket["id"], LIVE_MINUTES)
            elif res.kind in ("full", "closed", "not_found"):
                await repo_tickets.live_clear(conn, uid)
            return res

    async def close_by_user(self, uid: int, ticket_id: int) -> TicketResult:
        if not self._limiter.allow("ticket_message", uid):
            return TicketResult("rate_limited")
        async with self._pool.acquire() as conn, conn.transaction():
            ticket = await repo_tickets.get_owned(conn, uid, ticket_id, lock=True)
            if ticket is None:
                return TicketResult("not_found")
            if ticket["status"] == "closed":
                return TicketResult("closed", ticket_id)
            await repo_tickets.set_status(conn, ticket_id, "closed", "user")
        return TicketResult("ok", ticket_id, uid)

    async def my_tickets(self, uid: int) -> list[asyncpg.Record]:
        return await repo_tickets.list_for_user(self._pool, uid, LIST_LIMIT)

    async def my_ticket(self, uid: int, ticket_id: int) -> dict | None:
        """One of the caller's OWN tickets with its latest messages (never anyone else's)."""
        ticket = await repo_tickets.get_owned(self._pool, uid, ticket_id)
        if ticket is None:
            return None
        return {"ticket": ticket, "messages": await repo_tickets.messages(self._pool, ticket_id, 6)}

    # ============================================================ admin side

    async def list_tickets(self, admin_id: int, *, status: str | None, page: int) -> Page:
        if not self.is_admin(admin_id):
            return Page([], 0, 0, 1)
        status = status if status in STATUSES else None
        total = await repo_tickets.count_tickets(self._pool, status)
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        rows = await repo_tickets.list_tickets(self._pool, status, PAGE_SIZE, page * PAGE_SIZE)
        return Page(rows, total, page, pages)

    async def inbox(self, admin_id: int, status: str | None, page: int = 0) -> list:
        """One page of the inbox list for the live support page (admins only)."""
        if not self.is_admin(admin_id):
            return []
        status = status if status in STATUSES else None
        return await repo_tickets.inbox(self._pool, status, PAGE_SIZE, max(page, 0) * PAGE_SIZE)

    async def new_messages(self, admin_id: int, ticket_id: int, after: int) -> dict | None:
        """Messages of one ticket after the cursor, plus the ticket's current state. None for a
        non-admin or an unknown ticket: the same answer, nothing else is ever returned."""
        if not self.is_admin(admin_id):
            return None
        ticket = await repo_tickets.get(self._pool, ticket_id)
        if ticket is None:
            return None
        rows = await repo_tickets.messages_after(self._pool, ticket_id, max(after, 0))
        return {"ticket": ticket, "messages": rows}

    async def counts(self, admin_id: int) -> asyncpg.Record | None:
        return await repo_tickets.counts(self._pool) if self.is_admin(admin_id) else None

    async def detail(self, admin_id: int, ticket_id: int) -> dict | None:
        if not self.is_admin(admin_id):
            return None
        ticket = await repo_tickets.get(self._pool, ticket_id)
        if ticket is None:
            return None
        return {
            "ticket": ticket,
            "messages": await repo_tickets.messages(self._pool, ticket_id),
            "user_status": await repo_payments.user_status_plain(self._pool, ticket["user_id"]),
        }

    async def reply(self, admin_id: int, ticket_id: int, text: str | None) -> TicketResult:
        if not self.is_admin(admin_id):
            return TicketResult("denied")
        body = _text(text, MAX_REPLY)
        if body is None:
            return TicketResult("too_long", ticket_id)
        if not body:
            return TicketResult("empty", ticket_id)
        if not self._limiter.allow("admin_action", admin_id):
            return TicketResult("rate_limited", ticket_id)
        async with self._pool.acquire() as conn, conn.transaction():
            ticket = await repo_tickets.get(conn, ticket_id, lock=True)
            if ticket is None:
                return TicketResult("not_found")
            if ticket["status"] == "closed":
                return TicketResult("closed", ticket_id)
            if await repo_tickets.count_messages(conn, ticket_id) >= MAX_MESSAGES:
                return TicketResult("full", ticket_id)
            message_id = await repo_tickets.add_message(conn, ticket_id, "admin", body, admin_id)
            await repo_tickets.set_status(conn, ticket_id, "answered")
            await repo_tickets.audit(
                conn,
                admin_id,
                "ticket_reply",
                target=ticket["user_id"],
                detail=f"ticket #{ticket_id}",
            )
        return TicketResult("ok", ticket_id, ticket["user_id"], body, message_id)

    async def close(self, admin_id: int, ticket_id: int) -> TicketResult:
        if not self.is_admin(admin_id):
            return TicketResult("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return TicketResult("rate_limited", ticket_id)
        async with self._pool.acquire() as conn, conn.transaction():
            ticket = await repo_tickets.get(conn, ticket_id, lock=True)
            if ticket is None:
                return TicketResult("not_found")
            if ticket["status"] == "closed":
                return TicketResult("closed", ticket_id)
            await repo_tickets.set_status(conn, ticket_id, "closed", "admin")
            await repo_tickets.audit(
                conn,
                admin_id,
                "ticket_close",
                target=ticket["user_id"],
                detail=f"ticket #{ticket_id}",
            )
        return TicketResult("ok", ticket_id, ticket["user_id"])

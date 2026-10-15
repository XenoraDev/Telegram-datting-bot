"""Account lifecycle: hard-blocking minors and /deleteme."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from ..db import repo_chat, repo_profiles, repo_safety, repo_users
from .chat_relay import ContextBuffer

log = logging.getLogger(__name__)

_STAY_BLOCKED = frozenset({"banned", "blocked_underage"})


@dataclass(frozen=True, slots=True)
class DeleteResult:
    kind: str  # deleted | already_deleted | unknown_user
    partner_id: int | None = None  # chat partner that has to be told the chat ended
    kept_block: bool = False  # banned / underage accounts stay blocked after their data is wiped


class AccountService:
    def __init__(self, pool: asyncpg.Pool, buffer: ContextBuffer) -> None:
        self._pool = pool
        self._buffer = buffer

    async def block_underage(self, uid: int, reason: str) -> int | None:
        """Permanently block `uid`: profile/username wiped, queue + chat ended, tombstone written.

        Returns the chat partner (if any) so the caller can tell them the chat ended.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await repo_chat.lock_matchmaking(conn)
            session_ids = await repo_chat.user_session_ids(conn, uid)
            ended = await repo_chat.end_active_session(conn, uid, "underage_block")
            await repo_users.mark_underage(conn, uid, reason)
        for sid in session_ids:
            self._buffer.drop(sid)
        log.info("user blocked as underage (%s)", reason)
        return ended[1] if ended else None

    async def delete_account(self, uid: int) -> DeleteResult:
        """/deleteme. Removes personal data, keeps only what safety needs:

        * the users row (id + status, no username / language / age confirmation),
        * reports + restrictions + blocks AGAINST the user (so deleting does not erase them),
        * ended-chat metadata referenced by a report (no message content exists anywhere),
        * a tombstone for banned / underage users.
        Report contexts about or by the user are nulled and RAM chat buffers are dropped.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await repo_chat.lock_matchmaking(conn)
            row = await repo_users.get(conn, uid)
            if row is None:
                return DeleteResult("unknown_user")
            if row["status"] == "deleted":
                return DeleteResult("already_deleted")

            session_ids = await repo_chat.user_session_ids(conn, uid)
            ended = await repo_chat.end_active_session(conn, uid, "account_deleted")
            await repo_chat.leave_queue(conn, uid)
            await repo_profiles.delete(conn, uid)
            await repo_safety.delete_blocks_by(conn, uid)
            await repo_safety.clear_contexts_for(conn, uid)
            await repo_chat.delete_unreferenced_sessions(conn, uid)
            await repo_users.scrub(conn, uid)
        for sid in session_ids:
            self._buffer.drop(sid)
        log.info("account deleted")
        return DeleteResult("deleted", ended[1] if ended else None, row["status"] in _STAY_BLOCKED)

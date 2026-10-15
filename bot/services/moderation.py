"""Content rules (pure functions) + reports / blocks / abuse restrictions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import asyncpg

from .. import domain
from ..db import repo_chat, repo_safety
from .chat_relay import ContextBuffer

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- reports / blocks


@dataclass(frozen=True, slots=True)
class ReportResult:
    kind: str  # created | duplicate | rate_limited | not_member | invalid_reason
    partner_id: int | None = None  # set when the report ended an active chat
    restricted: bool = False  # the reported user got a temporary chat cooldown


@dataclass(frozen=True, slots=True)
class BlockResult:
    kind: str  # blocked | not_member
    partner_id: int | None = None  # set when the block ended an active chat


class ModerationService:
    def __init__(self, pool: asyncpg.Pool, buffer: ContextBuffer) -> None:
        self._pool = pool
        self._buffer = buffer

    async def report(self, reporter_id: int, session_id: int, reason: str) -> ReportResult:
        """File a report about the other participant of `session_id`.

        `reporter_id` MUST be the Telegram id from the update; the reported user is derived
        from the session membership, never taken from client-supplied data.
        """
        if reason not in domain.REPORT_REASONS:
            return ReportResult("invalid_reason")

        async with self._pool.acquire() as conn, conn.transaction():
            await repo_safety.lock_reporter(conn, reporter_id)
            counterpart = await repo_chat.counterpart_in_session(conn, session_id, reporter_id)
            if counterpart is None:
                return ReportResult("not_member")
            reported_id, session_active = counterpart

            if (
                await repo_safety.count_reports_by(conn, reporter_id, timedelta(hours=1))
                >= domain.REPORTS_PER_HOUR
                or await repo_safety.count_reports_by(conn, reporter_id, timedelta(days=1))
                >= domain.REPORTS_PER_DAY
            ):
                return ReportResult("rate_limited")

            context = self._buffer.snapshot_for_report(session_id, reporter_id)
            report_id = await repo_safety.add_report(
                conn,
                reporter=reporter_id,
                reported=reported_id,
                session_id=session_id,
                reason=reason,
                context=context,
            )
            if report_id is None:
                return ReportResult("duplicate")

            await repo_safety.add_block(conn, reporter_id, reported_id)  # never matched again
            partner_to_notify = None
            if session_active:
                await repo_chat.lock_matchmaking(conn)
                ended = await repo_chat.end_active_session(conn, reporter_id, "report")
                partner_to_notify = ended[1] if ended else None

            restricted = False
            if (
                await repo_safety.distinct_reporters(conn, reported_id, timedelta(hours=24))
                >= domain.RESTRICT_AFTER_DISTINCT_REPORTERS
            ):
                restricted = await repo_safety.add_restriction(
                    conn,
                    reported_id,
                    "chat_cooldown",
                    timedelta(hours=domain.COOLDOWN_HOURS),
                    "multiple_reports",
                )

        log.info("report %s filed (reason=%s restricted=%s)", report_id, reason, restricted)
        return ReportResult("created", partner_to_notify, restricted)

    async def block(self, blocker_id: int, session_id: int) -> BlockResult:
        async with self._pool.acquire() as conn, conn.transaction():
            counterpart = await repo_chat.counterpart_in_session(conn, session_id, blocker_id)
            if counterpart is None:
                return BlockResult("not_member")
            blocked_id, session_active = counterpart
            await repo_safety.add_block(conn, blocker_id, blocked_id)
            partner = None
            if session_active:
                await repo_chat.lock_matchmaking(conn)
                ended = await repo_chat.end_active_session(conn, blocker_id, "block")
                partner = ended[1] if ended else None
        return BlockResult("blocked", partner)

    async def mute_for_abuse(self, uid: int) -> bool:
        """Temporary mute after repeated rate-limit violations."""
        return await repo_safety.add_restriction(
            self._pool,
            uid,
            "mute",
            timedelta(minutes=domain.ABUSE_MUTE_MINUTES),
            "rate_limit_abuse",
        )

    async def purge_expired_contexts(self) -> int:
        return await repo_safety.purge_expired_contexts(self._pool)

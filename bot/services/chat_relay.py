"""Relay rules for anonymous chat (text + stickers only) and the RAM-only report context.

Privacy: message content is NEVER written to the database. The only copy is a small in-memory
ring buffer (last 10 text messages per chat, each dropped after 10 minutes) which is snapshotted
into `reports.context` only when someone files a report.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import asyncpg

from .. import domain
from ..db import repo_chat, repo_safety
from .content_rules import contact_violation, normalize
from .rate_limit import RateLimiter

BUFFER_MAX_MESSAGES = 10
BUFFER_TTL_SECONDS = 600.0
FLOOD_WINDOW_SECONDS = 30.0
FLOOD_IDENTICAL_LIMIT = 3


@dataclass(frozen=True, slots=True)
class BufferedMessage:
    ts: float
    sender_id: int
    text: str


class ContextBuffer:
    """RAM-only ring buffer per chat session. Lost on restart by design."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._sessions: dict[int, deque[BufferedMessage]] = {}

    def add(self, session_id: int, sender_id: int, text: str) -> None:
        q = self._sessions.setdefault(session_id, deque(maxlen=BUFFER_MAX_MESSAGES))
        q.append(BufferedMessage(self._clock(), sender_id, text[: domain.MESSAGE_MAX]))

    def _fresh(self, session_id: int) -> list[BufferedMessage]:
        q = self._sessions.get(session_id)
        if not q:
            return []
        cutoff = self._clock() - BUFFER_TTL_SECONDS
        while q and q[0].ts < cutoff:
            q.popleft()
        if not q:
            del self._sessions[session_id]
            return []
        return list(q)

    def snapshot_for_report(self, session_id: int, reporter_id: int) -> list[dict] | None:
        """JSON-able context with roles instead of user ids; None when nothing is buffered."""
        msgs = self._fresh(session_id)
        if not msgs:
            return None
        return [
            {
                "from": "reporter" if m.sender_id == reporter_id else "reported",
                "text": m.text,
                "at": datetime.fromtimestamp(m.ts, UTC).isoformat(timespec="seconds"),
            }
            for m in msgs
        ]

    def drop(self, session_id: int) -> None:
        self._sessions.pop(session_id, None)

    def prune(self) -> int:
        """Forget expired messages / empty sessions. Returns remaining tracked sessions."""
        for sid in list(self._sessions):
            self._fresh(sid)
        return len(self._sessions)

    def __len__(self) -> int:
        return len(self._sessions)


@dataclass(frozen=True, slots=True)
class Incoming:
    """What the Telegram layer extracted from a message (no aiogram types in services)."""

    kind: Literal["text", "sticker", "other"]
    text: str = ""
    sticker_id: str | None = None
    forwarded: bool = False
    via_bot: bool = False
    entity_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RelayResult:
    status: str  # ok | not_in_chat | muted | unsupported | link | contact | too_long | empty |
    #              flood | rate_limited
    partner_id: int | None = None
    session_id: int | None = None
    text: str = ""
    sticker_id: str | None = None


@dataclass
class _Flood:
    recent: dict[int, deque[tuple[float, str]]] = field(default_factory=lambda: defaultdict(deque))


class ChatRelayService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        buffer: ContextBuffer,
        limiter: RateLimiter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._buffer = buffer
        self._limiter = limiter
        self._clock = clock
        self._flood = _Flood()

    def _is_flood(self, sender_id: int, text: str) -> bool:
        now = self._clock()
        q = self._flood.recent[sender_id]
        while q and now - q[0][0] > FLOOD_WINDOW_SECONDS:
            q.popleft()
        key = normalize(text).strip()
        q.append((now, key))
        return sum(1 for _, k in q if k == key) >= FLOOD_IDENTICAL_LIMIT

    def prune(self) -> int:
        """Forget flood-tracking state of users who have been quiet; returns users still tracked."""
        now = self._clock()
        for uid in list(self._flood.recent):
            q = self._flood.recent[uid]
            while q and now - q[0][0] > FLOOD_WINDOW_SECONDS:
                q.popleft()
            if not q:
                del self._flood.recent[uid]
        return len(self._flood.recent)

    async def relay(self, sender_id: int, msg: Incoming) -> RelayResult:
        """Decide whether `msg` from `sender_id` may be delivered to their chat partner."""
        chat = await repo_chat.get_partner(self._pool, sender_id)
        if chat is None:
            return RelayResult("not_in_chat")
        session_id, partner_id = chat

        if await repo_safety.active_restriction(self._pool, sender_id, "mute") is not None:
            return RelayResult("muted")

        if msg.kind == "other" or msg.forwarded or msg.via_bot:
            return RelayResult("unsupported")

        if msg.kind == "sticker":
            if not msg.sticker_id:
                return RelayResult("unsupported")
            if not self._limiter.allow("chat_message", sender_id):
                return RelayResult("rate_limited")
            self._buffer.add(session_id, sender_id, "[sticker]")
            return RelayResult("ok", partner_id, session_id, sticker_id=msg.sticker_id)

        text = msg.text.strip()
        if not text:
            return RelayResult("empty")
        if len(text) > domain.MESSAGE_MAX:
            return RelayResult("too_long")
        violation = contact_violation(text, msg.entity_types)
        if violation:
            return RelayResult(violation)
        if not self._limiter.allow("chat_message", sender_id):
            return RelayResult("rate_limited")
        if self._is_flood(sender_id, text):
            return RelayResult("flood")
        self._buffer.add(session_id, sender_id, text)
        return RelayResult("ok", partner_id, session_id, text=text)

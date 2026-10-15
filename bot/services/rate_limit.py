"""In-process token-bucket rate limiter (no Redis needed).

State lives in this process only: fine for the single long-polling bot process. If the bot is
ever scaled to several processes the buckets become per-process (limits get looser, never
stricter); the persistent abuse limits (reports, restrictions) are enforced in PostgreSQL.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Policy:
    capacity: float  # burst size
    refill_per_sec: float  # sustained rate


POLICIES: dict[str, Policy] = {
    "message": Policy(capacity=6, refill_per_sec=1.5),  # every incoming message, per user
    "chat_message": Policy(capacity=8, refill_per_sec=2.0),  # relayed chat messages, per sender
    "callback": Policy(capacity=10, refill_per_sec=3.0),  # every button press, per user
    "chat_start": Policy(capacity=5, refill_per_sec=1 / 12),  # searches: 5 burst, 5 per minute
    "notice": Policy(
        capacity=1, refill_per_sec=1 / 8
    ),  # our own "slow down" / "unsupported" replies
    "delete_request": Policy(capacity=3, refill_per_sec=1 / 60),
}

VIOLATION_WINDOW = 60.0
MUTE_AFTER_VIOLATIONS = 10  # blocked updates within the window -> temporary mute


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[tuple[str, int], list[float]] = {}  # (action, key) -> [tokens, last]
        self._violations: dict[int, deque[float]] = {}

    def allow(self, action: str, key: int) -> bool:
        policy = POLICIES[action]
        now = self._clock()
        bucket = self._buckets.get((action, key))
        if bucket is None:
            bucket = self._buckets[(action, key)] = [policy.capacity, now]
        tokens = min(policy.capacity, bucket[0] + (now - bucket[1]) * policy.refill_per_sec)
        bucket[1] = now
        if tokens >= 1:
            bucket[0] = tokens - 1
            return True
        bucket[0] = tokens
        return False

    def record_violation(self, key: int) -> int:
        """Count a blocked update; returns how many happened in the last VIOLATION_WINDOW."""
        now = self._clock()
        q = self._violations.setdefault(key, deque())
        q.append(now)
        while q and now - q[0] > VIOLATION_WINDOW:
            q.popleft()
        return len(q)

    def clear_violations(self, key: int) -> None:
        self._violations.pop(key, None)

    def prune(self, max_idle: float = 900.0) -> int:
        """Drop idle buckets so memory stays bounded."""
        now = self._clock()
        stale = [k for k, b in self._buckets.items() if now - b[1] > max_idle]
        for k in stale:
            del self._buckets[k]
        for key in [
            k for k, q in self._violations.items() if not q or now - q[-1] > VIOLATION_WINDOW
        ]:
            del self._violations[key]
        return len(stale)

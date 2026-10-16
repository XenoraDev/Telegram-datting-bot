"""Login brute-force protection. In-process sliding window (no Redis).

Every login attempt counts, successful or not: a given client IP gets MAX_PER_IP tries per
WINDOW seconds, and the whole site gets MAX_GLOBAL (so a spoofed or rotating address cannot
bypass the limit). The limiter only ever says "no"; it can never let a non-admin in.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

WINDOW = 300.0
MAX_PER_IP = 10
MAX_GLOBAL = 60
_MAX_KEYS = 5000


class LoginThrottle:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, ip: str) -> bool:
        """Record an attempt; False when this IP (or the site as a whole) is over its limit."""
        now = self._clock()
        if len(self._hits) > _MAX_KEYS:
            self._prune(now)
        ip_hits = self._window("ip:" + ip, now)
        all_hits = self._window("*", now)
        if len(ip_hits) >= MAX_PER_IP or len(all_hits) >= MAX_GLOBAL:
            return False
        ip_hits.append(now)
        all_hits.append(now)
        return True

    def _window(self, key: str, now: float) -> deque[float]:
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > WINDOW:
            q.popleft()
        return q

    def _prune(self, now: float) -> None:
        for key in [k for k, q in self._hits.items() if not q or now - q[-1] > WINDOW]:
            del self._hits[key]

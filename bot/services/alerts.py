"""Throttled alerts to the admins in Telegram (an error, Telegram failing).

The same problem repeating must not turn into a flood of messages: each kind of problem (its
`signature`) is announced at most once per WINDOW seconds, with how many times it happened since
the last announcement, and at most MAX_PER_HOUR alerts leave the bot in any hour. Alerts carry the
kind of problem and where it happened, never a message text, a user id or an exception message
(which could contain a secret); the details are in the server log.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable

from aiogram import Bot

from ..texts import en as t

log = logging.getLogger(__name__)

WINDOW = 600.0  # one alert per kind of problem per 10 minutes
MAX_PER_HOUR = 6
MAX_SIGNATURES = 200  # bound on remembered kinds of problems


class AdminAlerter:
    def __init__(
        self,
        admin_ids: frozenset[int] = frozenset(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._admin_ids = admin_ids
        self._clock = clock
        self._last: dict[str, float] = {}  # signature -> time of its last alert
        self._pending: dict[str, int] = {}  # signature -> occurrences since then
        self._sent: deque[float] = deque()  # times of the alerts sent in the last hour

    def take(self, signature: str) -> int | None:
        """Count one occurrence. Returns how many occurrences the alert now announces, or None
        when this one is throttled (it is added to the next alert's count)."""
        now = self._clock()
        self._pending[signature] = self._pending.get(signature, 0) + 1
        while self._sent and now - self._sent[0] > 3600:
            self._sent.popleft()
        last = self._last.get(signature)
        if (last is not None and now - last < WINDOW) or len(self._sent) >= MAX_PER_HOUR:
            return None
        if len(self._last) >= MAX_SIGNATURES:
            self._last.pop(next(iter(self._last)))
        self._last[signature] = now
        self._sent.append(now)
        return self._pending.pop(signature)

    async def alert(self, bot: Bot, signature: str, template: str, **fmt: object) -> bool:
        """Tell every admin, unless this kind of problem was announced recently. `template` may
        use {count} (occurrences since the last alert). True when an alert was sent."""
        count = self.take(signature)
        if count is None or not self._admin_ids:
            return False
        from ..telegram_send import deliver  # late import: telegram_send imports this module

        text = template.format(count=count, **fmt)
        for admin_id in self._admin_ids:
            await deliver(bot, admin_id, text)  # no alerts= here: a failing alert must not recurse
        return True

    async def telegram_failure(self, bot: Bot, kind: str) -> None:
        await self.alert(bot, f"telegram:{kind}", t.ADMIN_TELEGRAM_FAILING, kind=kind)

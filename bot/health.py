"""Container liveness: the bot touches a heartbeat file, Docker HEALTHCHECK reads its age.

``python -m bot.health`` exits 0 when the file is fresh, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import DEFAULT_HEARTBEAT_FILE

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 15.0
PROBE_TIMEOUT = 10.0
MAX_AGE = 60.0


def touch(path: str) -> None:
    Path(path).touch()


async def heartbeat_loop(
    path: str,
    interval: float = HEARTBEAT_INTERVAL,
    probe: Callable[[], Awaitable[object]] | None = None,
) -> None:
    """Touch ``path`` every ``interval`` seconds until cancelled.

    With a ``probe`` (e.g. ``bot.get_me``) the file is only touched while the probe succeeds, so
    the container turns unhealthy when Telegram is unreachable or the token stops working.
    It never dies on errors: a failing probe or an unwritable path is logged and retried.
    """
    while True:
        try:
            if probe is not None:
                await asyncio.wait_for(probe(), PROBE_TIMEOUT)
            touch(path)
        except Exception as exc:  # keep the loop alive; the stale file is what signals trouble
            log.warning("heartbeat not updated: %s", exc)
        await asyncio.sleep(interval)


def is_healthy(path: str, max_age: float = MAX_AGE, now: float | None = None) -> bool:
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return False
    return ((time.time() if now is None else now) - mtime) <= max_age


if __name__ == "__main__":
    sys.exit(0 if is_healthy(os.environ.get("HEARTBEAT_FILE") or DEFAULT_HEARTBEAT_FILE) else 1)

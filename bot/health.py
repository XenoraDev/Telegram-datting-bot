"""Container liveness: the bot touches a heartbeat file, Docker HEALTHCHECK reads its age.

``python -m bot.health`` exits 0 when the file is fresh, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from .config import DEFAULT_HEARTBEAT_FILE

HEARTBEAT_INTERVAL = 15.0
MAX_AGE = 60.0


def touch(path: str) -> None:
    Path(path).touch()


async def heartbeat_loop(path: str, interval: float = HEARTBEAT_INTERVAL) -> None:
    """Touch ``path`` every ``interval`` seconds until cancelled."""
    while True:
        touch(path)
        await asyncio.sleep(interval)


def is_healthy(path: str, max_age: float = MAX_AGE, now: float | None = None) -> bool:
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return False
    return ((time.time() if now is None else now) - mtime) <= max_age


if __name__ == "__main__":
    sys.exit(0 if is_healthy(os.environ.get("HEARTBEAT_FILE") or DEFAULT_HEARTBEAT_FILE) else 1)

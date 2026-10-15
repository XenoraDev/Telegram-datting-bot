import asyncio
import os

import pytest

from bot import health


def test_missing_file_is_unhealthy(tmp_path):
    assert health.is_healthy(str(tmp_path / "nope")) is False


def test_fresh_file_is_healthy_and_old_file_is_not(tmp_path):
    f = tmp_path / "hb"
    health.touch(str(f))
    assert health.is_healthy(str(f)) is True
    old = f.stat().st_mtime - 3600
    os.utime(f, (old, old))
    assert health.is_healthy(str(f)) is False


def test_max_age_boundary(tmp_path):
    f = tmp_path / "hb"
    health.touch(str(f))
    mtime = f.stat().st_mtime
    assert health.is_healthy(str(f), max_age=60, now=mtime + 60) is True
    assert health.is_healthy(str(f), max_age=60, now=mtime + 60.5) is False


async def test_heartbeat_loop_touches_file_until_cancelled(tmp_path):
    f = tmp_path / "hb"
    task = asyncio.create_task(health.heartbeat_loop(str(f), interval=0.01))
    await asyncio.sleep(0.05)
    assert f.exists()
    first = f.stat().st_mtime_ns
    await asyncio.sleep(0.05)
    assert f.stat().st_mtime_ns >= first
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

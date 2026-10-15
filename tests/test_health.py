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


async def test_probe_failure_prevents_touch_and_recovery_resumes_it(tmp_path):
    f = tmp_path / "hb"
    ok = False

    async def probe():
        if not ok:
            raise OSError("telegram unreachable")

    task = asyncio.create_task(health.heartbeat_loop(str(f), interval=0.01, probe=probe))
    await asyncio.sleep(0.08)
    assert not f.exists()  # failing probe -> no heartbeat -> container goes unhealthy
    ok = True
    await asyncio.sleep(0.08)
    assert f.exists()  # loop survived the errors and recovered
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_hanging_probe_is_timed_out(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "PROBE_TIMEOUT", 0.05)
    f = tmp_path / "hb"

    async def probe():
        await asyncio.sleep(10)

    task = asyncio.create_task(health.heartbeat_loop(str(f), interval=0.01, probe=probe))
    await asyncio.sleep(0.2)
    assert not f.exists() and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unwritable_path_does_not_kill_the_loop(tmp_path):
    task = asyncio.create_task(
        health.heartbeat_loop(str(tmp_path / "missing-dir" / "hb"), interval=0.01)
    )
    await asyncio.sleep(0.08)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

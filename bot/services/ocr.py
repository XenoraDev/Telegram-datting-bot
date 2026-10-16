"""Reading text out of a screenshot with Tesseract (a small, deterministic, offline engine).

The image is piped to `tesseract` on stdin and the text comes back on stdout: nothing touches the
filesystem and no network is used. The call is bounded (timeout, output cap, at most
MAX_PARALLEL at once, no OCR for huge images), and every failure becomes an OcrError that the
caller turns into "unreadable": OCR trouble must never block a payment or crash the bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from typing import Protocol

MAX_PARALLEL = 2
TIMEOUT_SECONDS = 20.0
MAX_OUTPUT_BYTES = 256 * 1024
MAX_PIXELS = 16_000_000  # larger screenshots are not read (slow, and not a phone screenshot)


class OcrError(Exception):
    """The text could not be read. `reason` is a short code: unavailable | timeout | too_large |
    failed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OcrEngine(Protocol):
    name: str

    async def read(self, data: bytes, width: int, height: int) -> str: ...


class TesseractOcr:
    name = "tesseract"

    def __init__(self, binary: str = "tesseract", timeout: float = TIMEOUT_SECONDS) -> None:
        self._binary = binary
        self._timeout = timeout
        self._slots = asyncio.Semaphore(MAX_PARALLEL)

    def available(self) -> bool:
        return shutil.which(self._binary) is not None

    async def read(self, data: bytes, width: int, height: int) -> str:
        if width * height > MAX_PIXELS:
            raise OcrError("too_large")
        async with self._slots:
            # page segmentation 6 (a block of text) first; "sparse text" if that finds nothing
            first = await self._run(data, "6")
            if sum(c.isalnum() for c in first) >= 40:
                return first
            return first + "\n" + await self._run(data, "11")

    async def _run(self, data: bytes, psm: str) -> str:
        env = {"PATH": os.environ.get("PATH", ""), "OMP_THREAD_LIMIT": "1", "LC_ALL": "C"}
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "stdin",
                "stdout",
                "--psm",
                psm,
                "-l",
                "eng",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise OcrError("unavailable") from exc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(data), self._timeout)
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise OcrError("timeout") from exc
        except OSError as exc:
            raise OcrError("failed") from exc
        if proc.returncode != 0:
            raise OcrError("failed")
        return out[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")

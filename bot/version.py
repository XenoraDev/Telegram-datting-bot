"""Which release is this process? Read from the VERSION.json baked into the image at build time.

The release script builds the images with ``--build-arg APP_VERSION=<tag> GIT_SHA=<40 hex>
BUILD_TIME=<UTC>``; the Dockerfile writes them to ``/app/VERSION.json`` (and to image labels).
Outside such an image (tests, a plain checkout) the identity is ``dev`` / ``unknown``.

Nothing secret is ever here: a tag, a commit id and a time. The process start time is taken when
this module is first imported (that is when the release started running).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

# This code can run against a schema that a NEWER release made additively larger (see
# bot/db/migrate.py, ALLOW_NEWER_SCHEMA). Releases without this are not safe rollback targets.
SCHEMA_TOLERANT = True

STARTED_AT = datetime.now(UTC)

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "VERSION.json"
_VERSION = re.compile(r"dev|v\d{1,4}\.\d{1,4}\.\d{1,4}(-rc\d{1,3})?")
_SHA = re.compile(r"[0-9a-f]{40}")
_BUILT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


@dataclass(frozen=True, slots=True)
class VersionInfo:
    version: str = "dev"  # the release tag, e.g. v2.0.0
    sha: str = "unknown"  # the full 40-character commit id
    built: str = "unknown"  # image build time, UTC

    @property
    def short_sha(self) -> str:
        return self.sha[:7] if _SHA.fullmatch(self.sha) else self.sha

    @property
    def known(self) -> bool:
        return self.sha != "unknown"

    def line(self) -> str:
        return f"{self.version} ({self.short_sha}) built {self.built}"


def parse(raw: object) -> VersionInfo:
    """Accept only well-formed values; anything else is 'unknown' (never trusted into a page)."""
    if not isinstance(raw, dict):
        return VersionInfo()

    def pick(key: str, pattern: re.Pattern[str], default: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) and pattern.fullmatch(value) else default

    return VersionInfo(
        pick("version", _VERSION, "dev"),
        pick("sha", _SHA, "unknown"),
        pick("built", _BUILT, "unknown"),
    )


def load(path: Path | str | None = None) -> VersionInfo:
    file = Path(path or os.environ.get("VERSION_FILE") or DEFAULT_FILE)
    try:
        return parse(json.loads(file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return VersionInfo()


@lru_cache(maxsize=1)
def current() -> VersionInfo:
    return load()

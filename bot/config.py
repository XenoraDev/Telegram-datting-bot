"""Environment based settings. Pure parsing, no side effects, easy to test."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

_LOG_LEVELS = frozenset(logging.getLevelNamesMapping()) - {"NOTSET", "WARN", "FATAL"}


DEFAULT_HEARTBEAT_FILE = "/tmp/bot.heartbeat"  # noqa: S108 - container-local liveness marker


class ConfigError(ValueError):
    """Raised when the environment is missing or has invalid values."""


@dataclass(frozen=True, slots=True)
class Settings:
    # repr=False: secrets must never end up in logs or tracebacks.
    bot_token: str = field(repr=False)
    database_url: str = field(repr=False)
    admin_ids: frozenset[int] = frozenset()
    log_level: str = "INFO"
    heartbeat_file: str = DEFAULT_HEARTBEAT_FILE


def _parse_admin_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdecimal():
            raise ConfigError(f"ADMIN_IDS contains a non-numeric value: {part!r}")
        ids.add(int(part))
    return frozenset(ids)


def load_settings(env: Mapping[str, str]) -> Settings:
    """Build Settings from a mapping (normally ``os.environ``)."""

    def required(name: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            raise ConfigError(f"{name} is required but not set")
        return value

    log_level = env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in _LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}, got {log_level!r}")

    database_url = required("DATABASE_URL")
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise ConfigError("DATABASE_URL must start with postgresql:// or postgres://")

    return Settings(
        bot_token=required("BOT_TOKEN"),
        database_url=database_url,
        admin_ids=_parse_admin_ids(env.get("ADMIN_IDS", "")),
        log_level=log_level,
        heartbeat_file=env.get("HEARTBEAT_FILE", "").strip() or DEFAULT_HEARTBEAT_FILE,
    )

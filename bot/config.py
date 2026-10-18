"""Environment based settings. Pure parsing, no side effects, easy to test."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

_LOG_LEVELS = frozenset(logging.getLevelNamesMapping()) - {"NOTSET", "WARN", "FATAL"}


QUEUE_TIMEOUT_DEFAULT = 30  # minutes a random-chat search may wait (QUEUE_TIMEOUT_MINUTES)
QUEUE_TIMEOUT_RANGE = (1, 240)
CHAT_INACTIVITY_DEFAULT = 10  # minutes without a message before a chat is closed
CHAT_INACTIVITY_RANGE = (1, 1440)  # CHAT_INACTIVITY_TIMEOUT_MINUTES
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
    queue_timeout_minutes: int = QUEUE_TIMEOUT_DEFAULT
    chat_inactivity_timeout_minutes: int = CHAT_INACTIVITY_DEFAULT


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


def _database_url(env: Mapping[str, str]) -> str:
    """Explicit DATABASE_URL wins; otherwise build it from POSTGRES_* parts (docker compose).

    Building from parts percent-encodes user/password, so any password character is safe.
    Error messages never include the URL or password.
    """
    url = env.get("DATABASE_URL", "").strip()
    if not url:
        parts = {
            k: env.get(k, "").strip() for k in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
        }
        missing = [k for k, v in parts.items() if not v]
        if missing:
            raise ConfigError(
                f"set DATABASE_URL, or all of POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB "
                f"(missing: {', '.join(missing)})"
            )
        host = env.get("POSTGRES_HOST", "").strip() or "db"
        port = env.get("POSTGRES_PORT", "").strip() or "5432"
        url = (
            f"postgresql://{quote(parts['POSTGRES_USER'], safe='')}:"
            f"{quote(parts['POSTGRES_PASSWORD'], safe='')}@{host}:{port}/"
            f"{quote(parts['POSTGRES_DB'], safe='')}"
        )
    if not url.startswith(("postgresql://", "postgres://")):
        raise ConfigError("DATABASE_URL must start with postgresql:// or postgres://")
    try:
        parsed = urlsplit(url)
        parsed.port  # noqa: B018 - raises ValueError for a non-numeric port
    except ValueError:
        parsed = None
    if parsed is None or not parsed.hostname:
        raise ConfigError(
            "DATABASE_URL is not a valid URL (percent-encode special characters in the "
            "password, e.g. / ? # @ : as %2F %3F %23 %40 %3A)"
        )
    return url


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

    database_url = _database_url(env)

    raw_timeout = env.get("QUEUE_TIMEOUT_MINUTES", "").strip()
    queue_timeout = QUEUE_TIMEOUT_DEFAULT
    if raw_timeout:
        lo, hi = QUEUE_TIMEOUT_RANGE
        if not (raw_timeout.isdecimal() and lo <= int(raw_timeout) <= hi):
            raise ConfigError(f"QUEUE_TIMEOUT_MINUTES must be a whole number from {lo} to {hi}")
        queue_timeout = int(raw_timeout)

    raw_idle = env.get("CHAT_INACTIVITY_TIMEOUT_MINUTES", "").strip()
    chat_idle = CHAT_INACTIVITY_DEFAULT
    if raw_idle:
        lo, hi = CHAT_INACTIVITY_RANGE
        if not (raw_idle.isdecimal() and lo <= int(raw_idle) <= hi):
            raise ConfigError(
                f"CHAT_INACTIVITY_TIMEOUT_MINUTES must be a whole number from {lo} to {hi}"
            )
        chat_idle = int(raw_idle)

    return Settings(
        bot_token=required("BOT_TOKEN"),
        database_url=database_url,
        admin_ids=_parse_admin_ids(env.get("ADMIN_IDS", "")),
        log_level=log_level,
        heartbeat_file=env.get("HEARTBEAT_FILE", "").strip() or DEFAULT_HEARTBEAT_FILE,
        queue_timeout_minutes=queue_timeout,
        chat_inactivity_timeout_minutes=chat_idle,
    )

"""Web admin settings. Reuses the bot's settings (token, database, ADMIN_IDS) so there is one
source of truth; only the web specific values are added here. Pure parsing, no side effects."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from bot.config import ConfigError, Settings, load_settings

_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_BOT_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
MIN_SECRET_LENGTH = 32


@dataclass(frozen=True, slots=True)
class WebSettings:
    bot: Settings = field(repr=False)
    admin_domain: str  # the ONLY Host header accepted
    bot_username: str  # public username of the bot, for the Telegram Login Widget
    session_secret: str = field(repr=False)
    session_ttl: float = 8 * 3600.0  # hard limit, however active the admin is
    idle_ttl: float = 3600.0  # logged out after an hour without a request
    login_max_age: int = 300  # Telegram auth_date older than this is rejected
    trust_proxy: bool = True  # take the client IP from Caddy's X-Forwarded-For

    @property
    def admin_ids(self) -> frozenset[int]:
        return self.bot.admin_ids


def load_web_settings(env: Mapping[str, str]) -> WebSettings:
    bot = load_settings(env)
    if not bot.admin_ids:
        raise ConfigError("ADMIN_IDS is empty: nobody would be able to log in to the web admin")

    domain = env.get("ADMIN_DOMAIN", "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.fullmatch(domain):
        raise ConfigError("ADMIN_DOMAIN must be a bare host name such as admin-bot.example.com")

    username = env.get("ADMIN_BOT_USERNAME", "").strip().removeprefix("@")
    if not _BOT_USERNAME_RE.fullmatch(username):
        raise ConfigError("ADMIN_BOT_USERNAME must be the bot's @username (as in BotFather)")

    secret = env.get("ADMIN_SESSION_SECRET", "").strip()
    if len(secret) < MIN_SECRET_LENGTH:
        raise ConfigError(
            f"ADMIN_SESSION_SECRET must be at least {MIN_SECRET_LENGTH} characters "
            "(openssl rand -hex 32)"
        )
    return WebSettings(
        bot=bot,
        admin_domain=domain,
        bot_username=username,
        session_secret=secret,
        trust_proxy=env.get("ADMIN_TRUST_PROXY", "1").strip() not in ("0", "false", "no"),
    )

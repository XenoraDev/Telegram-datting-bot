"""Is this a TEST environment? The load tester refuses to run unless every check is green.

"localhost" proves nothing, so nothing is guessed. An operator must DECLARE the environment in
its configuration, and the declaration must agree with what is actually connected:

* APP_ENV=testing and LOAD_TEST_ENABLED=1                  (explicit flags)
* LOAD_TEST_DB_NAME = the database in the connection URL   (explicit test database), and that
  name must contain "test" and must not look like production
* the database itself carries the comment ``load-test-environment:<LOAD_TEST_ENV_ID>``
  (``COMMENT ON DATABASE``), so the connection cannot silently point at another database
* LOAD_TEST_BOT_ID = the id of the bot token in use, which must not be a declared production bot
* ADMIN_DOMAIN is on LOAD_TEST_ALLOWED_DOMAINS and not on LOAD_TEST_PRODUCTION_DOMAINS
* no unknown negative user ids exist (the synthetic namespace is provably ours)

Only derived facts (database name, host, bot id) are kept, never the token, a password or a URL.
There is deliberately no bypass: not in the UI, not in an environment variable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from ..config import ConfigError, load_settings
from .config import Limits

# What a production-looking name (database, host, user) contains as a whole word.
_PRODUCTION_WORD = re.compile(
    r"(?:^|[^a-z0-9])(prod|production|live|master|primary)(?:[^a-z0-9]|$)"
)
_DB_NAME = re.compile(r"^[a-z0-9_]{3,63}$")
_ENV_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_BOT_ID = re.compile(r"^[0-9]{3,15}$")
COMMENT_PREFIX = "load-test-environment:"

# The workload adapter owns no Telegram client at all; this documents (and the tests enforce) it.
TELEGRAM_TRAFFIC = False


def looks_like_production(text: str) -> bool:
    return bool(_PRODUCTION_WORD.search(text.lower()))


def _csv(raw: str) -> frozenset[str]:
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


@dataclass(frozen=True, slots=True)
class LoadTestSettings:
    """Everything the safety checks need, derived once from the environment."""

    app_env: str = ""
    enabled: bool = False
    env_id: str = ""
    expected_db: str = ""
    db_name: str = ""
    db_host: str = ""
    db_user: str = ""
    declared_bot_id: str = ""
    bot_id: str = ""
    production_bot_ids: frozenset[str] = frozenset()
    production_db_names: frozenset[str] = frozenset()
    admin_domain: str = ""
    allowed_domains: frozenset[str] = frozenset()
    production_domains: frozenset[str] = frozenset()
    config_error: str = ""
    limits: Limits = field(default_factory=Limits)
    pool_size: int = 10
    web_health_url: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LoadTestSettings:
        error = ""
        bot_id = db_name = db_host = db_user = ""
        try:
            settings = load_settings(env)
            bot_id = settings.bot_token.split(":", 1)[0]
            url = urlsplit(settings.database_url)
            db_name = unquote(url.path.lstrip("/"))
            db_host = (url.hostname or "").lower()
            db_user = unquote(url.username or "")
        except ConfigError as exc:
            error = str(exc)[:200]
        pool = env.get("LOAD_TEST_POOL_SIZE", "").strip()
        return cls(
            app_env=env.get("APP_ENV", "").strip().lower(),
            enabled=env.get("LOAD_TEST_ENABLED", "").strip() == "1",
            env_id=env.get("LOAD_TEST_ENV_ID", "").strip(),
            expected_db=env.get("LOAD_TEST_DB_NAME", "").strip(),
            db_name=db_name,
            db_host=db_host,
            db_user=db_user,
            declared_bot_id=env.get("LOAD_TEST_BOT_ID", "").strip(),
            bot_id=bot_id,
            production_bot_ids=_csv(env.get("LOAD_TEST_PRODUCTION_BOT_IDS", "")),
            production_db_names=_csv(env.get("LOAD_TEST_PRODUCTION_DB_NAMES", "")),
            admin_domain=env.get("ADMIN_DOMAIN", "").strip().lower().rstrip("."),
            allowed_domains=_csv(env.get("LOAD_TEST_ALLOWED_DOMAINS", "")),
            production_domains=_csv(env.get("LOAD_TEST_PRODUCTION_DOMAINS", "")),
            config_error=error,
            limits=Limits.from_env(env),
            pool_size=int(pool) if pool.isdecimal() and 2 <= int(pool) <= 100 else 10,
            web_health_url=env.get("LOAD_TEST_WEB_HEALTH_URL", "").strip(),
        )


@dataclass(frozen=True, slots=True)
class DbIdentity:
    """What the live database says about itself."""

    name: str
    comment: str
    foreign_negative_users: int  # users with a negative id that this module did not create


@dataclass(frozen=True, slots=True)
class Check:
    key: str
    label: str
    ok: bool
    detail: str

    def as_json(self) -> dict[str, object]:
        return {"key": self.key, "label": self.label, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class SafetyReport:
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def group_ok(self, group: str) -> bool:
        keys = GROUPS[group]
        return all(c.ok for c in self.checks if c.key in keys)

    def as_json(self) -> list[dict[str, object]]:
        return [c.as_json() for c in self.checks]


BLOCKED_TEXT = "LOAD TEST BLOCKED: production or unknown environment detected."

# The four lights of the Safety Status panel -> the checks behind each.
GROUPS: dict[str, tuple[str, ...]] = {
    "environment": ("environment_flag", "isolation"),
    "database": ("database_config", "database_identity", "database_not_production"),
    "telegram": ("bot_credentials", "telegram_traffic"),
    "production": ("database_not_production", "bot_credentials", "domain", "environment_flag"),
}


def evaluate(cfg: LoadTestSettings, identity: DbIdentity | None) -> SafetyReport:
    """Run every check. `identity` is None when the database could not be asked."""
    checks: list[Check] = []

    def add(key: str, label: str, ok: bool, ok_text: str, bad_text: str) -> None:
        checks.append(Check(key, label, ok, ok_text if ok else bad_text))

    # 1. explicit environment flags
    env = cfg.app_env
    if cfg.config_error:
        bad = f"configuration invalid: {cfg.config_error}"
        add("environment_flag", "Environment", False, "", bad)
    elif env in {"production", "prod", "live", "staging"} or looks_like_production(env):
        add("environment_flag", "Environment", False, "", f"APP_ENV={env!r} is production")
    elif env != "testing":
        add("environment_flag", "Environment", False, "", "APP_ENV is not 'testing' (unknown)")
    elif not cfg.enabled:
        add("environment_flag", "Environment", False, "", "LOAD_TEST_ENABLED=1 is not set")
    else:
        add("environment_flag", "Environment", True, "APP_ENV=testing, load testing enabled", "")

    # 2. explicit test database configuration
    name = cfg.db_name.lower()
    expected = cfg.expected_db.lower()
    label = "Test database configured"
    if not expected:
        add("database_config", label, False, "", "LOAD_TEST_DB_NAME is not set")
    elif not _DB_NAME.fullmatch(expected) or "test" not in expected:
        add("database_config", label, False, "", "LOAD_TEST_DB_NAME must contain 'test'")
    elif name != expected:
        add("database_config", label, False, "", "connected database is not LOAD_TEST_DB_NAME")
    else:
        add("database_config", label, True, f"'{name}' is the declared test database", "")

    # 3. database identity (asked of the live database)
    label = "Database identity"
    if identity is None:
        add("database_identity", label, False, "", "the database could not be queried")
    elif not _ENV_ID.fullmatch(cfg.env_id):
        add("database_identity", label, False, "", "LOAD_TEST_ENV_ID is not set")
    elif identity.name.lower() != name:
        add("database_identity", label, False, "", "live database differs from the URL")
    elif identity.comment != f"{COMMENT_PREFIX}{cfg.env_id}":
        add("database_identity", label, False, "", "database is not marked as this environment")
    else:
        add("database_identity", label, True, "the database carries this environment's marker", "")

    # 4. nothing production-looking
    names = " ".join([name, cfg.db_host, cfg.db_user.lower()])
    label = "No production database"
    if looks_like_production(names) or name in cfg.production_db_names:
        add("database_not_production", label, False, "", "database, host or user looks like prod")
    else:
        add("database_not_production", label, True, "no production-looking database", "")

    # 5. Telegram credentials
    label = "Test bot only"
    if not _BOT_ID.fullmatch(cfg.declared_bot_id):
        add("bot_credentials", label, False, "", "LOAD_TEST_BOT_ID is not set")
    elif cfg.bot_id != cfg.declared_bot_id:
        add("bot_credentials", label, False, "", "BOT_TOKEN is not the declared test bot")
    elif cfg.bot_id in cfg.production_bot_ids:
        add("bot_credentials", label, False, "", "BOT_TOKEN is a declared production bot")
    else:
        add("bot_credentials", label, True, "the configured bot is the declared test bot", "")

    # 6. domain
    domain = cfg.admin_domain
    label = "Test domain"
    if not domain or domain not in cfg.allowed_domains:
        add("domain", label, False, "", "ADMIN_DOMAIN is not on LOAD_TEST_ALLOWED_DOMAINS")
    elif domain in cfg.production_domains or looks_like_production(domain.replace(".", " ")):
        add("domain", label, False, "", "ADMIN_DOMAIN is a production domain")
    else:
        add("domain", label, True, "the admin domain is an allowed test domain", "")

    # 7. no real Telegram traffic
    add(
        "telegram_traffic",
        "Real Telegram traffic",
        TELEGRAM_TRAFFIC is False,
        "disabled: the workload has no Telegram client",
        "the workload would reach Telegram",
    )

    # 8. isolation of synthetic data
    label = "Synthetic data isolation"
    if identity is None:
        add("isolation", label, False, "", "the database could not be queried")
    elif identity.foreign_negative_users:
        add("isolation", label, False, "", "unknown rows use the synthetic id range")
    else:
        add("isolation", label, True, "synthetic ids (negative) are used by nothing else", "")

    return SafetyReport(tuple(checks))

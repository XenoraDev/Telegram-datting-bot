"""Where do new users come from? First-touch attribution from Telegram start links.

A start link looks like ``https://t.me/<bot>?start=c_<code>``; Telegram hands the text after
``start=`` to the bot as the /start argument. ``<code>`` is eight random characters that an admin
created for a campaign. Rules:

* Only a NEW account is ever attributed, once, when it is created. Later /start commands, other
  links, restarts of the bot and returning users never change or add anything (first touch is
  immutable, the database refuses updates).
* The payload is validated strictly and compared (as a parameter) with known campaign codes. The
  text a user sent is never stored or logged. A malformed, unknown or paused link is recorded as
  ``invalid``: only the LINK was invalid, never the user, and nothing is shown to the user.
* No source name is known to the code: sources and campaigns are admin data.
* Nothing is stored about the person except the account id, the campaign and the time.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass

import asyncpg

from ..db import repo_admin
from ..db import repo_attribution as repo
from ..db.repo_users import Db
from .payments import clean_text
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)

PAYLOAD_PREFIX = "c_"  # namespace: later r_ (referral), q_ (QR) can sit beside it
CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
CODE_LENGTH = 8
MAX_PAYLOAD = 64  # Telegram's own limit for a start parameter
_PAYLOAD = re.compile(rf"c_([a-z2-7]{{{CODE_LENGTH}}})")
SOURCE_KEY = re.compile(r"[a-z][a-z0-9_]{1,31}")
MAX_NAME = 60


def parse_payload(raw: str | None) -> tuple[str, str | None]:
    """('none', None) no argument; ('code', <code>) a well-formed campaign link; ('bad', None)
    anything else (wrong shape, too long, odd characters). Never raises."""
    if raw is None or raw == "":
        return "none", None
    if len(raw) > MAX_PAYLOAD or not raw.isascii():
        return "bad", None
    m = _PAYLOAD.fullmatch(raw)
    return ("code", m.group(1)) if m else ("bad", None)


def start_link(bot_username: str | None, code: str) -> str:
    """The link an admin shares. Without a known bot name only the argument is given."""
    payload = f"{PAYLOAD_PREFIX}{code}"
    return f"https://t.me/{bot_username}?start={payload}" if bot_username else payload


@dataclass(frozen=True, slots=True)
class CampaignResult:
    # ok | denied | rate_limited | bad_source | bad_name | not_found | unchanged
    kind: str
    campaign: asyncpg.Record | None = None


class AttributionService:
    def __init__(self, pool: asyncpg.Pool, limiter: RateLimiter, admin_ids: frozenset[int]) -> None:
        self._pool = pool
        self._limiter = limiter
        self._admin_ids = admin_ids

    def is_admin(self, uid: int) -> bool:
        return uid in self._admin_ids

    # ------------------------------------------------------------ first touch (bot side)

    async def record_first_touch(self, uid: int, payload: str | None, db: Db | None = None) -> str:
        """Attribute a NEW account. Call it only when the account was just created. Returns what
        was recorded: 'campaign', 'direct' or 'invalid' ('' if the user already had a first touch).
        Never raises for a bad payload."""
        db = db or self._pool
        shape, code = parse_payload(payload)
        kind, campaign_id, source = "direct", None, None
        if shape == "bad":
            kind = "invalid"
        elif shape == "code":
            campaign = await repo.campaign_by_code(db, code)
            if campaign is None:
                kind = "invalid"  # unknown code
            elif campaign["status"] != "active":
                kind, campaign_id = "invalid", campaign["id"]  # paused: only the link is invalid
            else:
                kind, campaign_id, source = "campaign", campaign["id"], campaign["source"]
        stored = await repo.insert_acquisition(db, uid, kind, campaign_id, source)
        if stored:
            log.info("first touch recorded: %s", kind)  # kind only: never the payload or the user
        return kind if stored else ""

    async def forget(self, uid: int, db: Db | None = None) -> None:
        """The account was deleted: its attribution goes with it."""
        await repo.delete_acquisition(db or self._pool, uid)

    # ------------------------------------------------------------ campaigns (admin side)

    def _gate(self, admin_id: int) -> CampaignResult | None:
        if not self.is_admin(admin_id):
            return CampaignResult("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return CampaignResult("rate_limited")
        return None

    async def campaigns(self, admin_id: int) -> list[asyncpg.Record]:
        return await repo.list_campaigns(self._pool) if self.is_admin(admin_id) else []

    async def sources(self, admin_id: int) -> list[str]:
        return await repo.sources(self._pool) if self.is_admin(admin_id) else []

    async def create_campaign(
        self, admin_id: int, source: str | None, name: str | None, *, is_test: bool = False
    ) -> CampaignResult:
        """A new campaign with a fresh random link code. The admin never builds a parameter."""
        if (refused := self._gate(admin_id)) is not None:
            return refused
        source = (source or "").strip().lower()
        if not SOURCE_KEY.fullmatch(source):
            return CampaignResult("bad_source")
        label = clean_text(name, MAX_NAME)
        if not label:
            return CampaignResult("bad_name")
        for _ in range(5):  # a clash on the 8-character code is astronomically rare; retry anyway
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            try:
                async with self._pool.acquire() as conn, conn.transaction():
                    row = await repo.create_campaign(conn, code, source, label, is_test, admin_id)
                    await repo_admin.audit(
                        conn,
                        admin_id,
                        "campaign_create",
                        detail=f"#{row['id']} {source}: {label}"[:200],
                    )
                return CampaignResult("ok", row)
            except asyncpg.UniqueViolationError:
                continue
        return CampaignResult("not_found")  # pragma: no cover - never in practice

    async def set_status(self, admin_id: int, campaign_id: int, *, paused: bool) -> CampaignResult:
        """Pause or resume a campaign. Nothing is deleted; a paused link just stops attributing."""
        if (refused := self._gate(admin_id)) is not None:
            return refused
        status = "paused" if paused else "active"
        async with self._pool.acquire() as conn, conn.transaction():
            row = await repo.set_status(conn, campaign_id, status)
            if row is None:
                known = await repo.get_campaign(conn, campaign_id)
                return CampaignResult("not_found" if known is None else "unchanged", known)
            await repo_admin.audit(
                conn, admin_id, "campaign_status", detail=f"#{campaign_id} -> {status}"
            )
        return CampaignResult("ok", row)

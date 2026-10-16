"""Admin authentication: Telegram Login Widget verification and server-side sessions.

Identity comes only from Telegram's signed payload; authorization only from ADMIN_IDS. Nothing
the browser sends decides whether someone is an admin. Sessions live in this process (a restart
logs everyone out, which is the safe direction); the cookie carries only a random id.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

# Fields Telegram's widget may send. Anything else is rejected, never passed on.
_LOGIN_FIELDS = frozenset(
    {"id", "first_name", "last_name", "username", "photo_url", "auth_date", "hash"}
)
_MAX_FIELD = 512
_CLOCK_SKEW = 60  # tolerated seconds of auth_date in the future
_MAX_SESSIONS = 1000


class LoginError(Exception):
    """The Telegram payload is invalid, tampered, expired, replayed or not an admin."""


def telegram_hash(params: dict[str, str], bot_token: str) -> str:
    """The hash Telegram computes: HMAC-SHA256 over the sorted fields, keyed by SHA256(token)."""
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "hash")
    key = hashlib.sha256(bot_token.encode()).digest()
    return hmac.new(key, data_check.encode(), hashlib.sha256).hexdigest()


class LoginVerifier:
    def __init__(
        self,
        bot_token: str,
        admin_ids: frozenset[int],
        max_age: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._token = bot_token
        self._admin_ids = admin_ids
        self._max_age = max_age
        self._clock = clock
        self._seen: dict[str, float] = {}  # hash -> time it stops being replayable

    def verify(self, params: dict[str, str]) -> int:
        """Return the admin's Telegram id or raise LoginError (always the same outward result)."""
        if (
            not params
            or set(params) - _LOGIN_FIELDS
            or any(len(v) > _MAX_FIELD for v in params.values())
        ):
            raise LoginError("fields")
        given = params.get("hash", "")
        if not {"id", "auth_date", "hash"} <= params.keys() or not _is_hex(given, 64):
            raise LoginError("fields")
        if not hmac.compare_digest(telegram_hash(params, self._token), given.lower()):
            raise LoginError("signature")
        uid_raw, date_raw = params["id"], params["auth_date"]
        if not (uid_raw.isascii() and uid_raw.isdecimal() and len(uid_raw) <= 15):
            raise LoginError("id")
        if not (date_raw.isascii() and date_raw.isdecimal() and len(date_raw) <= 12):
            raise LoginError("auth_date")
        now = self._clock()
        age = now - int(date_raw)
        if age > self._max_age or age < -_CLOCK_SKEW:
            raise LoginError("expired")
        self._forget_old(now)
        if given.lower() in self._seen:
            raise LoginError("replay")
        uid = int(uid_raw)
        if uid not in self._admin_ids:
            raise LoginError("not_admin")
        self._seen[given.lower()] = now + self._max_age + _CLOCK_SKEW
        return uid

    def _forget_old(self, now: float) -> None:
        for h in [h for h, until in self._seen.items() if until < now]:
            del self._seen[h]


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(c in "0123456789abcdefABCDEF" for c in value)


@dataclass(slots=True)
class Session:
    admin_id: int
    sid: str  # the cookie value; kept for the CSRF binding, never logged
    created: float
    last_seen: float


class SessionStore:
    def __init__(
        self,
        ttl: float,
        idle_ttl: float,
        admin_ids: frozenset[int],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._idle = idle_ttl
        self._admin_ids = admin_ids
        self._clock = clock
        self._by_key: dict[str, Session] = {}

    @staticmethod
    def _key(sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def create(self, admin_id: int) -> Session:
        now = self._clock()
        self._purge(now)
        while len(self._by_key) >= _MAX_SESSIONS:  # bounded memory: drop the oldest
            self._by_key.pop(next(iter(self._by_key)))
        sid = secrets.token_urlsafe(32)
        session = Session(admin_id, sid, now, now)
        self._by_key[self._key(sid)] = session
        return session

    def get(self, sid: str | None) -> Session | None:
        """The live session for this cookie, or None. Re-checks ADMIN_IDS on every call."""
        if not sid or len(sid) > 128:
            return None
        key = self._key(sid)
        session = self._by_key.get(key)
        if session is None:
            return None
        now = self._clock()
        if (
            now - session.created > self._ttl
            or now - session.last_seen > self._idle
            or session.admin_id not in self._admin_ids
        ):
            del self._by_key[key]
            return None
        session.last_seen = now
        return session

    def destroy(self, sid: str | None) -> None:
        if sid:
            self._by_key.pop(self._key(sid), None)

    def _purge(self, now: float) -> None:
        dead = [
            k
            for k, s in self._by_key.items()
            if now - s.created > self._ttl or now - s.last_seen > self._idle
        ]
        for k in dead:
            del self._by_key[k]

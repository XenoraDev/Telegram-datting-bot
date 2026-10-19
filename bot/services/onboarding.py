"""18+ gate and profile onboarding: validation + the state machine around the users table."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from .. import domain
from ..db import repo_profiles, repo_users
from .abuse_words import has_abuse
from .account import AccountService
from .attribution import AttributionService
from .content_rules import profile_text_violation


class InvalidInput(Exception):
    """User input rejected; `code` is mapped to a message by the Telegram layer."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


log = logging.getLogger(__name__)

# ---------------------------------------------------------------- pure validation


def _clean(text: str) -> str:
    return " ".join(text.split())  # collapse whitespace/newlines


def validate_name(raw: str) -> str:
    name = _clean(raw)
    if not 1 <= len(name) <= domain.NAME_MAX:
        raise InvalidInput("name_length")
    if any(not c.isprintable() for c in name):
        raise InvalidInput("name_chars")
    _check_text(name)
    return name


def validate_city(raw: str) -> str:
    city = _clean(raw)
    if not 1 <= len(city) <= domain.CITY_MAX:
        raise InvalidInput("city_length")
    if any(not c.isprintable() or c.isdigit() for c in city):  # a region name, not an address
        raise InvalidInput("city_chars")
    _check_text(city)
    return city


def validate_bio(raw: str) -> str:
    bio = raw.strip()
    if not 1 <= len(bio) <= domain.BIO_MAX:
        raise InvalidInput("bio_length")
    if any(not (c.isprintable() or c == "\n") for c in bio):
        raise InvalidInput("bio_chars")
    _check_text(bio)
    return bio


def _check_text(text: str) -> None:
    violation = profile_text_violation(text)
    if violation:
        raise InvalidInput(f"text_{violation}")
    if has_abuse(text):
        raise InvalidInput("text_abuse")


def parse_age(raw: str) -> int:
    """Return a valid adult age. Raises InvalidInput('age_not_number' | 'age_range').

    Ages below 18 are returned as-is so the caller can hard-block (see AgeResult).
    """
    s = raw.strip()
    if not (s.isdecimal() and len(s) <= 3):
        raise InvalidInput("age_not_number")
    age = int(s)  # int() understands any Unicode decimal digits, so no digit-script bypass
    if age > domain.MAX_AGE:
        raise InvalidInput("age_range")
    return age


def validate_gender(raw: str) -> str:
    if raw not in domain.GENDERS:
        raise InvalidInput("gender")
    return raw


def validate_country(raw: str) -> str:
    if raw not in domain.COUNTRIES:
        raise InvalidInput("country")
    return raw


def validate_looking_for(raw: str) -> str:
    if raw not in domain.LOOKING_FOR:
        raise InvalidInput("looking_for")
    return raw


# ---------------------------------------------------------------- service


@dataclass(frozen=True, slots=True)
class StartResult:
    state: str  # blocked | need_age_confirmation | need_profile | ready


@dataclass(frozen=True, slots=True)
class AgeResult:
    kind: str  # ok | underage
    age: int = 0
    partner_id: int | None = None


@dataclass(frozen=True, slots=True)
class ProfileResult:
    kind: str  # ok | underage | not_allowed
    partner_id: int | None = None


class OnboardingService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        account: AccountService,
        attribution: AttributionService | None = None,
    ) -> None:
        self._pool = pool
        self._account = account
        self._attribution = attribution

    async def start(
        self, uid: int, username: str | None, payload: str | None = None
    ) -> StartResult:
        """/start: create or look up the account and say what the user has to do next.

        `payload` is the text after /start (a campaign link). It matters only for an account that is
        created by THIS call (or restarted after a deletion): that is the one first touch.
        Attribution can never block or slow the sign-up: a failure there is only logged."""
        reason = await repo_users.tombstone_reason(self._pool, uid)
        row = await repo_users.upsert_seen(self._pool, uid, username)
        if reason is not None:
            # banned / underage ids can never come back, even if the users row was reset.
            if reason == "underage" and row["status"] != domain.BLOCKED_UNDERAGE:
                await self._account.block_underage(uid, "tombstone")
            return StartResult("blocked")

        fresh = bool(row["inserted"])
        if row["status"] == domain.DELETED:
            row = await repo_users.reset_deleted(self._pool, uid, username)
            fresh = True  # a deleted account starts over from scratch: a new acquisition
        status = row["status"]
        if status in (domain.BLOCKED_UNDERAGE, domain.BANNED):
            return StartResult("blocked")
        if fresh and self._attribution is not None:
            await self._first_touch(uid, payload)
        if status == domain.ACTIVE:
            return StartResult("ready")
        if row["age_confirmed_at"] is None:
            return StartResult("need_age_confirmation")
        return StartResult("need_profile")

    async def _first_touch(self, uid: int, payload: str | None) -> None:
        try:
            await self._attribution.record_first_touch(uid, payload)
        except Exception:
            log.exception("first-touch attribution failed; sign-up continues")

    async def confirm_adult(self, uid: int) -> bool:
        """The user pressed 'I am 18 or older'. False if the account is not in onboarding."""
        return await repo_users.confirm_adult(self._pool, uid)

    async def declare_minor(self, uid: int) -> int | None:
        """The user pressed 'I am under 18' -> permanent block."""
        return await self._account.block_underage(uid, "self_declared")

    async def submit_age(self, uid: int, raw: str) -> AgeResult:
        """Validate a typed age. Raises InvalidInput for non-numbers / absurd values."""
        age = parse_age(raw)
        if age < domain.MIN_AGE:
            partner = await self._account.block_underage(uid, "age_entered")
            return AgeResult("underage", age, partner)
        return AgeResult("ok", age)

    async def complete_profile(
        self,
        uid: int,
        *,
        name: str,
        age: int,
        gender: str,
        looking_for: str,
        city: str,
        bio: str,
        country: str,
    ) -> ProfileResult:
        """Server-side re-validation of everything, then save + activate in one transaction.

        Raises InvalidInput if any field is invalid. The 18+ rule is enforced here AND by the
        SQL (profile insert requires age_confirmed_at) AND by CHECK constraints.
        """
        if age < domain.MIN_AGE:
            partner = await self._account.block_underage(uid, "age_entered")
            return ProfileResult("underage", partner)
        if age > domain.MAX_AGE:
            raise InvalidInput("age_range")
        fields = {
            "name": validate_name(name),
            "city": validate_city(city),
            "bio": validate_bio(bio),
            "gender": validate_gender(gender),
            "looking_for": validate_looking_for(looking_for),
            "country": validate_country(country),
        }
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                if not await repo_profiles.upsert(conn, uid, age=age, **fields):
                    return ProfileResult("not_allowed")
                if not await repo_users.activate(conn, uid):
                    raise _Rollback  # the profile insert must not survive a failed activation
        except _Rollback:
            return ProfileResult("not_allowed")
        return ProfileResult("ok")


class _Rollback(Exception):
    pass

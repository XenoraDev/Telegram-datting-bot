"""What a viewer sees of a partner after a chat starts or a match happens (P7).

Every card has the same fields. For each protected field (age, gender, city) the state is decided
BEFORE anything is built:

    owner switched it off                 -> "hidden" (nobody sees it; no plan can change that)
    owner allows it, viewer not entitled  -> "locked" (a localized "🔒 Premium" placeholder)
    owner allows it, viewer entitled      -> "shown"  (the value)

The viewer's entitlements (view_partner_age / _gender / _city) come from the EntitlementService;
the database only returns a value when it is allowed (see db/repo_cards.py), so a protected value
is never fetched, formatted, logged or put in a callback for a viewer who may not see it.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from ..db import repo_cards
from .entitlements import EntitlementService
from .rate_limit import RateLimiter

MATCHES_PAGE = 10
VIEW_FEATURES = ["view_partner_age", "view_partner_gender", "view_partner_city"]


@dataclass(frozen=True, slots=True)
class FieldView:
    state: str  # shown | locked | hidden
    value: str | int | None = None  # only set when state == "shown"


@dataclass(frozen=True, slots=True)
class PartnerCard:
    public_id: str
    name: str
    looking_for: str
    bio: str
    country: str | None
    age: FieldView
    gender: FieldView
    city: FieldView
    badge: int  # partner's plan_badge level: 0 none, 1 premium, 2 pro, 3 vip
    cta: bool  # at least one field is locked and the viewer has not been nudged recently

    @property
    def has_locked(self) -> bool:
        return "locked" in (self.age.state, self.gender.state, self.city.state)


def _view(owner_shows: bool, viewer_can: bool, value: str | int | None) -> FieldView:
    if not owner_shows:
        return FieldView("hidden")
    if not viewer_can:
        return FieldView("locked")
    return FieldView("shown", value)


class PartnerCardService:
    def __init__(
        self, pool: asyncpg.Pool, entitlements: EntitlementService, limiter: RateLimiter
    ) -> None:
        self._pool = pool
        self._entitlements = entitlements
        self._limiter = limiter

    async def _can(self, viewer: int) -> tuple[bool, bool, bool]:
        got = await self._entitlements.get_many(viewer, VIEW_FEATURES)
        return tuple(got[f].enabled for f in VIEW_FEATURES)  # type: ignore[return-value]

    async def _build(
        self, viewer: int, row: repo_cards.CardRow, can: tuple[bool, bool, bool], *, nudge: bool
    ) -> PartnerCard:
        badge = (await self._entitlements.get(row.user_id, "plan_badge")).level
        age = _view(row.owner_shows_age, can[0], row.age)
        gender = _view(row.owner_shows_gender, can[1], row.gender)
        city = _view(row.owner_shows_city, can[2], row.city)
        locked = "locked" in (age.state, gender.state, city.state)
        return PartnerCard(
            row.public_id,
            row.name,
            row.looking_for,
            row.bio,
            row.country,
            age,
            gender,
            city,
            badge,
            cta=locked and nudge and self._limiter.allow("premium_cta", viewer),
        )

    async def for_viewer(self, viewer: int, partner: int) -> PartnerCard | None:
        can = await self._can(viewer)
        row = await repo_cards.one(self._pool, partner, *can)
        return await self._build(viewer, row, can, nudge=True) if row else None

    async def matches(self, viewer: int) -> tuple[list[PartnerCard], int, bool]:
        """(cards, total, show_upgrade_hint): one hint for the whole list, not one per card."""
        can = await self._can(viewer)
        rows, total = await repo_cards.matches(self._pool, viewer, *can, MATCHES_PAGE)
        cards = [await self._build(viewer, r, can, nudge=False) for r in rows]
        hint = any(c.has_locked for c in cards) and self._limiter.allow("premium_cta", viewer)
        return cards, total, hint

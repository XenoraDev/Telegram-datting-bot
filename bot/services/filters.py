"""Advanced browse filters (entitlement `advanced_filters`: Pro / VIP).

Who may use them is asked of the EntitlementService on EVERY call (button presses are never
trusted). A filter on age / gender / city additionally needs the matching `view_partner_*`
entitlement, otherwise the filter result would reveal a value the viewer is not allowed to see.
The profile owner's privacy switches are applied in SQL (repo_browse): a hidden value never
matches a filter. Filters live in PostgreSQL; if the entitlement lapses they are kept but ignored.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from .. import domain
from ..db import repo_filters
from ..db.repo_filters import Filters
from .entitlements import EntitlementService
from .onboarding import InvalidInput, validate_city

AGE_MIN, AGE_MAX = domain.MIN_AGE, 99
_FIELD_FEATURE = {
    "age": "view_partner_age",
    "gender": "view_partner_gender",
    "city": "view_partner_city",
    "looking_for": None,
}
_FEATURES = ["advanced_filters", *(f for f in _FIELD_FEATURE.values() if f)]
GENDER_VALUES = ("m", "f", "o")
LOOKING_VALUES = ("m", "f", "any")


@dataclass(frozen=True, slots=True)
class FilterState:
    allowed: bool  # entitled to advanced filters at all
    filters: Filters  # what is stored (shown in the menu)
    locked: frozenset[str] = frozenset()  # fields the viewer may not filter on


@dataclass(frozen=True, slots=True)
class FilterResult:
    kind: str  # ok | denied | locked_field | invalid | no_profile
    state: FilterState | None = None
    code: str | None = None  # validation code for `invalid` (maps to an `invalid.*` text)


class FilterService:
    def __init__(self, pool: asyncpg.Pool, entitlements: EntitlementService) -> None:
        self._pool = pool
        self._entitlements = entitlements

    async def _locked(self, uid: int) -> tuple[bool, frozenset[str]]:
        ents = await self._entitlements.get_many(uid, _FEATURES)
        if not ents["advanced_filters"].enabled:
            return False, frozenset(_FIELD_FEATURE)
        locked = frozenset(
            f for f, feat in _FIELD_FEATURE.items() if feat and not ents[feat].enabled
        )
        return True, locked

    async def state(self, uid: int) -> FilterState:
        allowed, locked = await self._locked(uid)
        return FilterState(allowed, await repo_filters.get(self._pool, uid), locked)

    async def effective(self, uid: int) -> Filters | None:
        """The filters browse must apply right now: nothing unless the viewer is entitled, and
        never a field they may not see."""
        allowed, locked = await self._locked(uid)
        if not allowed:
            return None
        stored = await repo_filters.get(self._pool, uid)
        kept = Filters(
            None if "age" in locked else stored.age_min,
            None if "age" in locked else stored.age_max,
            None if "gender" in locked else stored.gender,
            None if "city" in locked else stored.city,
            stored.looking_for,
        )
        return None if kept.is_empty else kept

    async def _apply(self, uid: int, field: str, *values: object) -> FilterResult:
        allowed, locked = await self._locked(uid)
        if not allowed:
            return FilterResult("denied")
        if field in locked:
            return FilterResult("locked_field", await self.state(uid))
        if not await self._has_profile(uid):
            return FilterResult("no_profile")
        await repo_filters.set_field(self._pool, uid, field, *values)
        return FilterResult("ok", await self.state(uid))

    async def _has_profile(self, uid: int) -> bool:
        return bool(await self._pool.fetchval("SELECT 1 FROM profiles WHERE user_id = $1", uid))

    async def set_age(self, uid: int, low: int | None, high: int | None) -> FilterResult:
        """Age range; (None, None) clears it. Either end may be open."""
        for v in (low, high):
            if v is not None and (isinstance(v, bool) or not AGE_MIN <= v <= AGE_MAX):
                return FilterResult("invalid", code="filter_age")
        if low is not None and high is not None and low > high:
            return FilterResult("invalid", code="filter_age")
        return await self._apply(uid, "age", low, high)

    async def set_gender(self, uid: int, value: str | None) -> FilterResult:
        if value is not None and value not in GENDER_VALUES:
            return FilterResult("invalid", code="filter_choice")
        return await self._apply(uid, "gender", value)

    async def set_looking_for(self, uid: int, value: str | None) -> FilterResult:
        if value is not None and value not in LOOKING_VALUES:
            return FilterResult("invalid", code="filter_choice")
        return await self._apply(uid, "looking_for", value)

    async def set_city(self, uid: int, raw: str | None) -> FilterResult:
        if raw is None:
            return await self._apply(uid, "city", None)
        try:
            city = validate_city(raw)
        except InvalidInput as exc:
            return FilterResult("invalid", code=exc.code)
        return await self._apply(uid, "city", city)

    async def reset(self, uid: int) -> FilterResult:
        allowed, _ = await self._locked(uid)
        if not allowed:
            return FilterResult("denied")
        await repo_filters.reset(self._pool, uid)
        return FilterResult("ok", await self.state(uid))

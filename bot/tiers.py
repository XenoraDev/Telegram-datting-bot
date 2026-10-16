"""What each subscription tier is worth, as plain entitlement data (no code ever asks "is this
user premium?"; it asks the EntitlementService for a feature).

Every feature value is the P4 JSON object {"enabled": bool, "daily_limit": int | null} plus, for
ranked features, {"level": int}. The free plan is the baseline every user has; a paid plan is
merged on top of it most-generous-wins, so a tier only needs to list what it adds or raises.

    random_chat   daily random-chat starts            browse  daily profiles browsed
    swipe         daily likes                         view_partner_age / _gender / _city
    advanced_filters, see_who_liked, profile_boost    capabilities (enabled or absent)
    match_priority  level: queue / browse ranking     plan_badge  level: 1 premium, 2 pro, 3 vip

Unlimited (VIP) means daily_limit null: the per-second abuse limiter in RateLimiter still applies.
The same numbers are seeded by migration 008 (a test keeps the two in sync).
"""

from __future__ import annotations

FREE: dict[str, dict] = {
    "random_chat": {"enabled": True, "daily_limit": 100},
    "browse": {"enabled": True, "daily_limit": 100},
    "swipe": {"enabled": True, "daily_limit": 50},
    "view_partner_city": {"enabled": True},
    "match_priority": {"enabled": True, "level": 0},
}


def _limits(n: int | None) -> dict[str, dict]:
    return {k: {"enabled": True, "daily_limit": n} for k in ("random_chat", "browse", "swipe")}


_UNLOCKS = {f"view_partner_{f}": {"enabled": True} for f in ("age", "gender", "city")}
_PRO_CAPABILITIES = {
    k: {"enabled": True} for k in ("advanced_filters", "see_who_liked", "profile_boost")
}


def _rank(level: int) -> dict[str, dict]:
    return {
        "match_priority": {"enabled": True, "level": level},
        "plan_badge": {"enabled": True, "level": level},
    }


TIERS: dict[str, dict[str, dict]] = {
    "premium": {**_limits(200), **_UNLOCKS, **_rank(1)},
    "pro": {**_limits(500), **_UNLOCKS, **_PRO_CAPABILITIES, **_rank(2)},
    "vip": {**_limits(None), **_UNLOCKS, **_PRO_CAPABILITIES, **_rank(3)},
}
TIER_NAMES = tuple(TIERS)
BADGE_BY_LEVEL = {1: "premium", 2: "pro", 3: "vip"}

"""Admin editor for tier entitlements (limits and capabilities), Free / Premium / Pro / VIP.

Everything is entitlement DATA, as before: a limit or capability is a row in `plan_entitlements`,
read at runtime by the EntitlementService. The editor changes those rows:

  * Premium / Pro / VIP: the values are saved in `tier_settings` (they win over the built-in
    template of bot/tiers.py when a plan of that tier is created or completed) AND written to every
    existing plan of that tier, so all plans of a tier stay consistent. Plan-specific facts (name,
    price, length, active) are not touched, nor are subscriptions or payments.
  * Free: the rows of the free plan are written directly.

Nothing here asks "which plan is this?"; no business logic reads a plan name or a tier.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import asyncpg

from ..db import repo_payments, repo_tiers
from ..tiers import FREE, TIER_NAMES, TIERS
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)

LIMITS = ("random_chat", "browse", "swipe")
CAPABILITIES = (
    "view_partner_age",
    "view_partner_gender",
    "view_partner_city",
    "advanced_filters",
    "see_who_liked",
    "profile_boost",
)
EDITABLE = (*LIMITS, *CAPABILITIES, "match_priority", "plan_badge")
ALL_TIERS = ("free", *TIER_NAMES)
MAX_LIMIT = 1_000_000
MAX_PRIORITY = 100
MAX_BADGE = 3  # 0 none, 1 Premium, 2 Pro, 3 VIP (the badge texts that exist)


@dataclass(frozen=True, slots=True)
class TierView:
    tier: str
    limits: dict[str, tuple[str, int]]  # feature -> (mode "limited" | "unlimited", number)
    capabilities: dict[str, bool]
    priority: int
    badge: int
    plans: list[str]  # codes of the plans that follow this tier
    customised: bool  # differs from the built-in template
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TierResult:
    kind: str  # ok | denied | rate_limited | not_found | invalid
    field: str | None = None  # invalid: which input


def template_of(tier: str) -> dict[str, dict]:
    return FREE if tier == "free" else TIERS[tier]


def _ui(values: dict[str, dict]) -> tuple[dict, dict, int, int]:
    limits: dict[str, tuple[str, int]] = {}
    for feat in LIMITS:
        v = values.get(feat)
        if not v or v.get("enabled") is False:
            limits[feat] = ("limited", 0)
        elif v.get("daily_limit") is None:
            limits[feat] = ("unlimited", 0)
        else:
            limits[feat] = ("limited", int(v["daily_limit"]))
    caps = {c: bool(values.get(c) and values[c].get("enabled", True)) for c in CAPABILITIES}
    prio = values.get("match_priority") or {}
    badge = values.get("plan_badge") or {}
    return (
        limits,
        caps,
        int(prio.get("level", 0)) if prio.get("enabled", True) else 0,
        int(badge.get("level", 0)) if badge.get("enabled", True) else 0,
    )


def _number(raw: str | None, low: int, high: int) -> int | None:
    text = (raw or "").strip()
    if not (text.isascii() and text.isdecimal() and len(text) <= 9):
        return None
    value = int(text)
    return value if low <= value <= high else None


def parse_form(form) -> tuple[dict[str, dict] | None, str | None]:
    """(entitlement values, None) or (None, name of the first invalid input).

    limit: "<feature>_mode" = limited | unlimited, and for "limited" a whole number
    "<feature>_limit" from 0 to 1,000,000 (0 = nobody can use it today). An "unlimited" mode
    ignores the number; a negative, malformed or missing number with "limited" is refused.
    capability: checkbox "cap_<name>" = 1. "match_priority" 0..100, "plan_badge" 0..3."""
    values: dict[str, dict] = {}
    for feat in LIMITS:
        mode = form.get(f"{feat}_mode")
        if mode == "unlimited":
            values[feat] = {"enabled": True, "daily_limit": None}
        elif mode == "limited":
            n = _number(form.get(f"{feat}_limit"), 0, MAX_LIMIT)
            if n is None:
                return None, f"{feat}_limit"
            values[feat] = {"enabled": True, "daily_limit": n}
        else:
            return None, f"{feat}_mode"
    for cap in CAPABILITIES:
        values[cap] = {"enabled": form.get(f"cap_{cap}") == "1"}
    prio = _number(form.get("match_priority"), 0, MAX_PRIORITY)
    if prio is None:
        return None, "match_priority"
    values["match_priority"] = {"enabled": True, "level": prio}
    badge = _number(form.get("plan_badge"), 0, MAX_BADGE)
    if badge is None:
        return None, "plan_badge"
    values["plan_badge"] = {"enabled": badge > 0, "level": badge}
    return values, None


def describe(values: dict[str, dict]) -> str:
    """One audit line: what was saved."""
    parts = []
    for feat in LIMITS:
        limit = values[feat].get("daily_limit")
        parts.append(f"{feat}={'unlimited' if limit is None else limit}")
    on = [c for c in CAPABILITIES if values[c]["enabled"]]
    parts.append("on=" + (",".join(on) or "-"))
    parts.append(f"priority={values['match_priority']['level']}")
    parts.append(f"badge={values['plan_badge']['level']}")
    return " ".join(parts)


def plan_summary(features: dict[str, dict]) -> str:
    """A short, readable line of what a plan's entitlement rows give (for the Plans page)."""
    parts = []
    for name, feat in (("chat", "random_chat"), ("browse", "browse"), ("likes", "swipe")):
        v = features.get(feat)
        if not v or v.get("enabled") is False:
            parts.append(f"{name} off")
        else:
            limit = v.get("daily_limit")
            parts.append(f"{name} {'unlimited' if limit is None else limit}/day")
    shown = [w for w in ("age", "gender", "city") if features.get(f"view_partner_{w}")]
    parts.append("partner " + ("/".join(shown) if shown else "details locked"))
    extras = [
        label
        for label, key in (
            ("filters", "advanced_filters"),
            ("who liked", "see_who_liked"),
            ("boost", "profile_boost"),
        )
        if features.get(key)
    ]
    if extras:
        parts.append(" + ".join(extras))
    level = (features.get("match_priority") or {}).get("level", 0)
    parts.append(f"priority {level}")
    return " · ".join(parts)


class TierAdminService:
    def __init__(
        self, pool: asyncpg.Pool, limiter: RateLimiter, admin_ids: frozenset[int] = frozenset()
    ) -> None:
        self._pool = pool
        self._limiter = limiter
        self._admin_ids = admin_ids

    def is_admin(self, uid: int) -> bool:
        return uid in self._admin_ids

    async def overview(self, admin_id: int) -> list[TierView]:
        if not self.is_admin(admin_id):
            return []
        plans = await repo_tiers.plans_overview(self._pool)
        free_id = await self._pool.fetchval("SELECT id FROM plans WHERE code = 'free'")
        views: list[TierView] = []
        built: dict[str, tuple] = {}
        for tier in ALL_TIERS:
            if tier == "free":
                values = await repo_tiers.plan_values(self._pool, free_id, EDITABLE)
                default = FREE
            else:
                values = await repo_tiers.effective_template(self._pool, tier)
                default = TIERS[tier]
            ui = _ui(values)
            built[tier] = ui
            views.append(
                TierView(
                    tier,
                    ui[0],
                    ui[1],
                    ui[2],
                    ui[3],
                    [p["code"] for p in plans if p["tier"] == tier],
                    ui != _ui(default),
                )
            )
        free_limits = built["free"][0]
        for view in views:
            if view.tier == "free":
                continue
            for feat, (mode, n) in view.limits.items():
                fmode, fn = free_limits[feat]
                if (
                    fmode == "unlimited"
                    and mode == "limited"
                    or (mode == "limited" and fmode == "limited" and n < fn)
                ):
                    view.warnings.append(feat)
        return views

    async def plan_summaries(self, admin_id: int) -> dict[str, str]:
        """{plan code: one-line summary of its limits and capabilities}."""
        if not self.is_admin(admin_id):
            return {}
        rows = await repo_tiers.plans_overview(self._pool)
        return {r["code"]: plan_summary(json.loads(r["features"])) for r in rows}

    def _gate(self, admin_id: int) -> TierResult | None:
        if not self.is_admin(admin_id):
            return TierResult("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return TierResult("rate_limited")
        return None

    async def save(self, admin_id: int, tier: str, form) -> TierResult:
        if (refused := self._gate(admin_id)) is not None:
            return refused
        if tier not in ALL_TIERS:
            return TierResult("not_found")
        values, bad = parse_form(form)
        if values is None:
            return TierResult("invalid", bad)
        await self._write(admin_id, tier, values, f"tier {tier} saved: {describe(values)}")
        return TierResult("ok")

    async def reset(self, admin_id: int, tier: str) -> TierResult:
        """Back to the built-in template of bot/tiers.py."""
        if (refused := self._gate(admin_id)) is not None:
            return refused
        if tier not in ALL_TIERS:
            return TierResult("not_found")
        template = {**template_of(tier)}
        template.setdefault("plan_badge", {"enabled": False, "level": 0})
        for cap in CAPABILITIES:
            template.setdefault(cap, {"enabled": False})
        await self._write(admin_id, tier, template, f"tier {tier} reset to defaults", reset=True)
        return TierResult("ok")

    async def _write(
        self, admin_id: int, tier: str, values: dict[str, dict], detail: str, *, reset: bool = False
    ) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await repo_payments.lock_plan_defaults(conn)
            if tier == "free":
                targets = [await repo_tiers.free_plan_id(conn)]
            else:
                targets = await repo_tiers.plan_ids(conn, tier)
                if reset:
                    await conn.execute("DELETE FROM tier_settings WHERE tier = $1", tier)
                else:
                    for feat in EDITABLE:
                        await repo_tiers.save_override(conn, tier, feat, values[feat], admin_id)
            for plan_id in targets:
                for feat in EDITABLE:
                    value = values.get(feat)
                    await repo_tiers.set_plan_feature(conn, plan_id, feat, value)
            await repo_payments.audit(conn, admin_id, "plan_save", detail=detail)
        log.info("tier %s entitlements updated", tier)

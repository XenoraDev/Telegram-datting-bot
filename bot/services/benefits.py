"""What a plan gives a buyer, as short readable lines (the /premium plan list).

Pure: it only formats entitlement data. The numbers come from the plan's own entitlement rows
merged over the Free plan most-generous-wins (the same `merge` the EntitlementService uses), so
what /premium promises is exactly what the buyer will get, including admin edits on the Tiers page.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..texts.i18n import tr
from .entitlements import Entitlement, merge

FEATURES = (
    "random_chat",
    "browse",
    "swipe",
    "view_partner_age",
    "view_partner_gender",
    "view_partner_city",
    "advanced_filters",
    "see_who_liked",
    "profile_boost",
    "match_priority",
    "plan_badge",
)
_LIMITS = (
    ("random_chat", "benefit.chats", "benefit.chats_unlimited"),
    ("browse", "benefit.browse", "benefit.browse_unlimited"),
    ("swipe", "benefit.likes", "benefit.likes_unlimited"),
)
_PARTNER = ("age", "gender", "city")
_EXTRAS = (
    ("advanced_filters", "benefit.filters"),
    ("see_who_liked", "benefit.who_liked"),
    ("profile_boost", "benefit.boost"),
)

Rows = Mapping[str, object]  # feature key -> raw JSON value (or already-parsed dict)


def effective(free: Rows, plan: Rows | None = None) -> dict[str, Entitlement]:
    """Every feature's merged entitlement: the Free baseline plus (optionally) one paid plan."""
    out = {}
    for feature in FEATURES:
        rows = [("free", free[feature])] if feature in free else []
        if plan is not None and feature in plan:
            rows.append(("subscription", plan[feature]))
        out[feature] = merge(feature, rows)
    return out


def lines(ent: Mapping[str, Entitlement], lang: str) -> list[str]:
    """One short line per benefit that is switched on, most important first."""
    out = []
    for feature, limited, unlimited in _LIMITS:
        e = ent[feature]
        if not e.enabled:
            continue
        out.append(
            tr(unlimited, lang) if e.daily_limit is None else tr(limited, lang, n=e.daily_limit)
        )
    shown = [w for w in _PARTNER if ent[f"view_partner_{w}"].enabled]
    if shown:
        names = ", ".join(tr(f"benefit.field_{w}", lang) for w in shown)
        out.append(tr("benefit.partner", lang, fields=names))
    out.extend(tr(key, lang) for feature, key in _EXTRAS if ent[feature].enabled)
    if ent["match_priority"].level > 0:
        out.append(tr("benefit.priority", lang))
    if ent["plan_badge"].level > 0:
        out.append(tr("benefit.badge", lang))
    return out


def plan_block(name: str, amount: str, days: int, benefit_lines: Iterable[str], lang: str) -> str:
    body = "\n".join(f"• {line}" for line in benefit_lines)
    head = tr("premium.plan_head", lang, name=name, amount=amount, days=days)
    return f"{head}\n{body}" if body else head

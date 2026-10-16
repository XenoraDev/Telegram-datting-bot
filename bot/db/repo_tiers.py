"""Admin-edited tier entitlements (`tier_settings`) and their propagation to plans (SQL only)."""

from __future__ import annotations

import json

from ..tiers import TIERS
from .repo_users import Db

FREE_CODE = "free"


async def overrides(db: Db, tier: str) -> dict[str, dict]:
    rows = await db.fetch(
        "SELECT feature_key, value::text AS value FROM tier_settings WHERE tier = $1", tier
    )
    return {r["feature_key"]: json.loads(r["value"]) for r in rows}


async def effective_template(db: Db, tier: str) -> dict[str, dict]:
    """The built-in template of the tier with the admin's changes on top."""
    return {**TIERS[tier], **await overrides(db, tier)}


async def save_override(db: Db, tier: str, feature: str, value: dict, admin_id: int) -> None:
    await db.execute(
        "INSERT INTO tier_settings (tier, feature_key, value, updated_by) "
        "VALUES ($1, $2, $3::jsonb, $4) ON CONFLICT (tier, feature_key) "
        "DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
        "updated_at = now()",
        tier,
        feature,
        json.dumps(value),
        admin_id,
    )


async def plan_ids(db: Db, tier: str) -> list[int]:
    rows = await db.fetch("SELECT id FROM plans WHERE tier = $1 ORDER BY id FOR UPDATE", tier)
    return [r["id"] for r in rows]


async def free_plan_id(db: Db) -> int | None:
    return await db.fetchval("SELECT id FROM plans WHERE code = $1 FOR UPDATE", FREE_CODE)


async def set_plan_feature(db: Db, plan_id: int, feature: str, value: dict | None) -> None:
    """Write one entitlement row of a plan; None removes it (= the feature is off)."""
    if value is None or value.get("enabled") is False:
        await db.execute(
            "DELETE FROM plan_entitlements WHERE plan_id = $1 AND feature_key = $2",
            plan_id,
            feature,
        )
        return
    await db.execute(
        "INSERT INTO plan_entitlements (plan_id, feature_key, value) VALUES ($1, $2, $3::jsonb) "
        "ON CONFLICT (plan_id, feature_key) DO UPDATE SET value = EXCLUDED.value",
        plan_id,
        feature,
        json.dumps(value),
    )


async def plan_values(db: Db, plan_id: int, features: tuple[str, ...]) -> dict[str, dict]:
    rows = await db.fetch(
        "SELECT feature_key, value::text AS value FROM plan_entitlements "
        "WHERE plan_id = $1 AND feature_key = ANY($2::text[])",
        plan_id,
        list(features),
    )
    return {r["feature_key"]: json.loads(r["value"]) for r in rows}


async def plans_overview(db: Db) -> list:
    """Every paid plan with its entitlement rows, for the summary on the Plans page."""
    return await db.fetch(
        "SELECT p.id, p.code, p.tier, "
        "coalesce(jsonb_object_agg(e.feature_key, e.value) "
        "FILTER (WHERE e.feature_key IS NOT NULL), '{}'::jsonb)::text AS features "
        "FROM plans p LEFT JOIN plan_entitlements e ON e.plan_id = p.id "
        "WHERE p.code <> 'free' GROUP BY p.id ORDER BY p.id"
    )

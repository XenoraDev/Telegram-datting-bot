-- 010: admin-editable tier entitlements.
-- The built-in tier templates live in bot/tiers.py. An admin can change them on the Tiers page;
-- the changed values are stored here (one row per tier and feature) and win over the built-in
-- template whenever a plan of that tier is created, completed or edited. Saving a tier also writes
-- the values to every existing plan of that tier, so plans of one tier stay consistent.
-- Additive: nothing existing is changed or removed.
CREATE TABLE tier_settings (
    tier        TEXT        NOT NULL CHECK (tier IN ('premium', 'pro', 'vip')),
    feature_key TEXT        NOT NULL CHECK (feature_key ~ '^[a-z][a-z0-9_]{0,47}$'),
    value       JSONB       NOT NULL,
    updated_by  BIGINT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tier, feature_key)
);

-- 006: P4 language + entitlement foundation. Additive: no existing row is changed or removed.
-- No payment provider is involved; provider columns are free-form hooks for a later phase.

-- Language: users.language already exists (default 'en'); restrict it to supported codes.
ALTER TABLE users
    ADD CONSTRAINT users_language_supported CHECK (language IN ('en', 'hi'));

CREATE TABLE plans (
    id               SERIAL      PRIMARY KEY,
    code             TEXT        NOT NULL UNIQUE CHECK (code ~ '^[a-z][a-z0-9_]{0,31}$'),
    name             TEXT        NOT NULL,
    billing_interval TEXT        NOT NULL DEFAULT 'none'
        CHECK (billing_interval IN ('none', 'month', 'year')),
    is_active        BOOLEAN     NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- value is a JSON object: {"enabled": bool, "daily_limit": int|null}; a missing / null
-- daily_limit means unlimited.
CREATE TABLE plan_entitlements (
    plan_id     INTEGER NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
    feature_key TEXT    NOT NULL CHECK (feature_key ~ '^[a-z][a-z0-9_]{0,47}$'),
    value       JSONB   NOT NULL CHECK (jsonb_typeof(value) = 'object'),
    PRIMARY KEY (plan_id, feature_key)
);

-- A user's plan over time (renewals / payments are a later phase; nothing writes here yet).
CREATE TABLE subscriptions (
    id                   BIGSERIAL   PRIMARY KEY,
    user_id              BIGINT      NOT NULL REFERENCES users (id),
    plan_id              INTEGER     NOT NULL REFERENCES plans (id),
    status               TEXT        NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'past_due', 'canceled', 'expired')),
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    current_period_end   TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN     NOT NULL DEFAULT false,
    provider             TEXT,
    provider_ref         TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX subscriptions_user ON subscriptions (user_id, status);

-- Individual overrides: boosts, promos, manual grants.
CREATE TABLE entitlement_grants (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     BIGINT      NOT NULL REFERENCES users (id),
    feature_key TEXT        NOT NULL CHECK (feature_key ~ '^[a-z][a-z0-9_]{0,47}$'),
    value       JSONB       NOT NULL CHECK (jsonb_typeof(value) = 'object'),
    source      TEXT        NOT NULL CHECK (source IN ('subscription', 'boost', 'admin', 'promo')),
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX entitlement_grants_user ON entitlement_grants (user_id, feature_key);

-- Only counts, per user + feature + UTC day. No content, no timestamps finer than a day.
CREATE TABLE usage_counters (
    user_id      BIGINT  NOT NULL REFERENCES users (id),
    feature_key  TEXT    NOT NULL,
    period_start DATE    NOT NULL,
    count        INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (user_id, feature_key, period_start)
);
CREATE INDEX usage_counters_period ON usage_counters (period_start);

-- The default ("free") plan mirrors today's product: everything that exists is enabled and
-- unlimited, so no current user notices anything.
INSERT INTO plans (code, name) VALUES ('free', 'Free');
INSERT INTO plan_entitlements (plan_id, feature_key, value)
SELECT p.id, f.key, f.value::jsonb
FROM plans p,
     (VALUES ('random_chat', '{"enabled": true}'),
             ('browse',      '{"enabled": true}'),
             ('swipe',       '{"enabled": true}')) AS f (key, value)
WHERE p.code = 'free';

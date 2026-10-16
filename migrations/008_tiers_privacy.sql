-- 008: subscription tiers (Premium / Pro / VIP), partner-field privacy, queue priority.
-- Additive: no existing row is deleted. Free-plan limits and tier data are plain entitlement rows
-- (see bot/services/tiers.py); admin-edited values are never overwritten.

-- Which tier template a paid plan was built from (the runtime never looks at it: it asks for
-- entitlements). NULL = custom plan.
ALTER TABLE plans
    ADD COLUMN tier TEXT CHECK (tier IS NULL OR tier IN ('premium', 'pro', 'vip'));

-- The profile owner decides which of these partners may see. A subscription can unlock showing a
-- value to a viewer, but never overrides a "false" here.
ALTER TABLE profiles
    ADD COLUMN show_age    BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN show_gender BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN show_city   BOOLEAN NOT NULL DEFAULT true;

-- Higher-tier waiters are served first (then the longest waiting).
ALTER TABLE chat_queue
    ADD COLUMN priority SMALLINT NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 100);
CREATE INDEX chat_queue_priority ON chat_queue (priority DESC, joined_at);

-- Free plan: the daily limits. Only rows still at the original unlimited default are changed;
-- anything an operator edited is left alone. New rows never overwrite existing ones.
UPDATE plan_entitlements pe SET value = '{"enabled": true, "daily_limit": 100}'::jsonb
FROM plans p
WHERE p.id = pe.plan_id AND p.code = 'free' AND pe.feature_key IN ('random_chat', 'browse')
  AND pe.value = '{"enabled": true}'::jsonb;
UPDATE plan_entitlements pe SET value = '{"enabled": true, "daily_limit": 50}'::jsonb
FROM plans p
WHERE p.id = pe.plan_id AND p.code = 'free' AND pe.feature_key = 'swipe'
  AND pe.value = '{"enabled": true}'::jsonb;
INSERT INTO plan_entitlements (plan_id, feature_key, value)
SELECT p.id, f.key, f.value::jsonb
FROM plans p,
     (VALUES ('view_partner_city', '{"enabled": true}'),
             ('match_priority',    '{"enabled": true, "level": 0}')) AS f (key, value)
WHERE p.code = 'free'
ON CONFLICT (plan_id, feature_key) DO NOTHING;

-- Paid plans created in P6 were given the three plain "unlimited" flags and nothing else. Turn
-- exactly those into the Premium tier; a plan an admin customised is not touched.
CREATE TEMP TABLE _legacy_paid ON COMMIT DROP AS
SELECT p.id FROM plans p
WHERE p.price_minor IS NOT NULL AND p.tier IS NULL
  AND (SELECT count(*) FROM plan_entitlements e WHERE e.plan_id = p.id) = 3
  AND NOT EXISTS (SELECT 1 FROM plan_entitlements e WHERE e.plan_id = p.id
                  AND (e.value <> '{"enabled": true}'::jsonb
                       OR e.feature_key NOT IN ('random_chat', 'browse', 'swipe')));
UPDATE plans SET tier = 'premium' WHERE id IN (SELECT id FROM _legacy_paid);
DELETE FROM plan_entitlements WHERE plan_id IN (SELECT id FROM _legacy_paid);
INSERT INTO plan_entitlements (plan_id, feature_key, value)
SELECT l.id, t.key, t.value::jsonb
FROM _legacy_paid l,
     (VALUES ('random_chat',       '{"enabled": true, "daily_limit": 200}'),
             ('browse',            '{"enabled": true, "daily_limit": 200}'),
             ('swipe',             '{"enabled": true, "daily_limit": 200}'),
             ('view_partner_age',    '{"enabled": true}'),
             ('view_partner_gender', '{"enabled": true}'),
             ('view_partner_city',   '{"enabled": true}'),
             ('match_priority',    '{"enabled": true, "level": 1}'),
             ('plan_badge',        '{"enabled": true, "level": 1}')) AS t (key, value);

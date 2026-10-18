-- 018: structured fields on the admin audit log for subscription management. Additive only.
--
-- A cancel / revoke audit entry now records WHICH subscription (and, for a revoked payment
-- approval, which payment) was affected, and the admin's full reason, as real columns instead of
-- a 200-character text. Every existing row simply has NULL in the new columns; no row is changed
-- or removed, no constraint is dropped, and the table stays append-only.
ALTER TABLE admin_actions
    ADD COLUMN subscription_id BIGINT,
    ADD COLUMN payment_id      BIGINT,
    ADD COLUMN reason          TEXT CHECK (reason IS NULL OR char_length(reason) <= 300);

CREATE INDEX admin_actions_payment ON admin_actions (payment_id) WHERE payment_id IS NOT NULL;
CREATE INDEX admin_actions_subscription ON admin_actions (subscription_id)
    WHERE subscription_id IS NOT NULL;

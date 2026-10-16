-- 007: P6 manual UPI payments. Additive: no existing row is changed or removed.
-- A user pays outside the bot, uploads a screenshot (evidence only) and an admin approves or
-- rejects it. Only approval activates a subscription, through the P4 `subscriptions` table.

-- Price and length of a purchasable plan. NULL on both = not for sale (the free plan).
ALTER TABLE plans
    ADD COLUMN duration_days INTEGER CHECK (duration_days BETWEEN 1 AND 3660),
    ADD COLUMN price_minor   INTEGER CHECK (price_minor BETWEEN 1 AND 100000000),
    ADD COLUMN currency      TEXT    NOT NULL DEFAULT 'INR' CHECK (currency ~ '^[A-Z]{3}$'),
    ADD CONSTRAINT plans_price_with_duration CHECK ((price_minor IS NULL) = (duration_days IS NULL));

-- One row: where users are told to pay. Not active until an admin completes it.
CREATE TABLE payment_settings (
    id           BOOLEAN     PRIMARY KEY DEFAULT true CHECK (id),
    upi_id       TEXT CHECK (upi_id IS NULL OR upi_id ~ '^[A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9.-]{1,31}$'),
    payee_name   TEXT CHECK (payee_name IS NULL OR char_length(payee_name) BETWEEN 1 AND 60),
    instructions TEXT CHECK (instructions IS NULL OR char_length(instructions) <= 500),
    is_active    BOOLEAN     NOT NULL DEFAULT false,
    updated_by   BIGINT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NOT is_active OR (upi_id IS NOT NULL AND payee_name IS NOT NULL))
);
INSERT INTO payment_settings (id) VALUES (true);

-- Status flow:  created -> pending -> approved | rejected      created -> cancelled
--   created   order opened, user has not submitted yet
--   pending   submitted with a screenshot, waiting for an admin
-- amount / duration / pay-to are a snapshot of what the user was shown.
CREATE TABLE payments (
    id               BIGSERIAL   PRIMARY KEY,
    reference        TEXT        NOT NULL UNIQUE CHECK (reference ~ '^PAY-[A-Z2-7]{10}$'),
    user_id          BIGINT      NOT NULL REFERENCES users (id),
    plan_id          INTEGER     NOT NULL REFERENCES plans (id),
    amount_minor     INTEGER     NOT NULL CHECK (amount_minor > 0),
    currency         TEXT        NOT NULL CHECK (currency = 'INR'),
    duration_days    INTEGER     NOT NULL CHECK (duration_days > 0),
    pay_to           TEXT        NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'created'
        CHECK (status IN ('created', 'pending', 'approved', 'rejected', 'cancelled')),
    utr              TEXT        CHECK (utr IS NULL OR utr ~ '^[A-Z0-9]{6,35}$'),
    submitted_at     TIMESTAMPTZ,
    reviewed_by      BIGINT,
    reviewed_at      TIMESTAMPTZ,
    rejection_reason TEXT        CHECK (rejection_reason IS NULL OR char_length(rejection_reason) <= 300),
    subscription_id  BIGINT      REFERENCES subscriptions (id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status NOT IN ('pending', 'approved') OR submitted_at IS NOT NULL),
    CHECK (status NOT IN ('approved', 'rejected') OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)),
    CHECK (status <> 'rejected' OR rejection_reason IS NOT NULL),
    CHECK (status <> 'approved' OR subscription_id IS NOT NULL)
);
CREATE INDEX payments_user ON payments (user_id, created_at DESC);
CREATE INDEX payments_status ON payments (status, created_at DESC);
CREATE INDEX payments_utr ON payments (utr) WHERE utr IS NOT NULL;
-- Abuse guard enforced by the database: a user has at most ONE open order (created or pending).
CREATE UNIQUE INDEX payments_one_open_per_user ON payments (user_id)
    WHERE status IN ('created', 'pending');
-- A transaction id can back only one approved payment.
CREATE UNIQUE INDEX payments_utr_approved ON payments (utr)
    WHERE status = 'approved' AND utr IS NOT NULL;

-- Only legal moves; approved / rejected / cancelled rows are frozen; what was ordered never changes.
CREATE FUNCTION payments_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.user_id <> OLD.user_id OR NEW.plan_id <> OLD.plan_id
       OR NEW.amount_minor <> OLD.amount_minor OR NEW.currency <> OLD.currency
       OR NEW.duration_days <> OLD.duration_days OR NEW.reference <> OLD.reference
       OR NEW.pay_to <> OLD.pay_to THEN
        RAISE EXCEPTION 'payment order details are immutable';
    END IF;
    IF OLD.status IN ('approved', 'rejected', 'cancelled') THEN
        RAISE EXCEPTION 'payment is final (%)', OLD.status;
    END IF;
    IF NEW.status <> OLD.status AND NOT (
           (OLD.status = 'created' AND NEW.status IN ('pending', 'cancelled'))
        OR (OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected'))) THEN
        RAISE EXCEPTION 'illegal payment status change % -> %', OLD.status, NEW.status;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER payments_guard_update BEFORE UPDATE ON payments
    FOR EACH ROW EXECUTE FUNCTION payments_guard();

-- Evidence only: the screenshot bytes, validated before they get here. Kept in the database
-- (no files on disk, nothing executable, no paths) and deleted with its payment.
CREATE TABLE payment_evidence (
    payment_id   BIGINT      PRIMARY KEY REFERENCES payments (id) ON DELETE CASCADE,
    content_type TEXT        NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
    size_bytes   INTEGER     NOT NULL CHECK (size_bytes BETWEEN 1 AND 3145728),
    sha256       TEXT        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    data         BYTEA       NOT NULL CHECK (octet_length(data) = size_bytes),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX payment_evidence_sha ON payment_evidence (sha256);

-- A payment id can back only one subscription row.
CREATE UNIQUE INDEX subscriptions_provider_ref ON subscriptions (provider, provider_ref)
    WHERE provider_ref IS NOT NULL;

-- Audit: the new admin actions (the table stays append-only).
ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;
ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check CHECK (action IN (
    'ban', 'unban', 'report_resolve', 'report_dismiss', 'report_ban',
    'payment_approve', 'payment_reject', 'payment_config', 'plan_save'));

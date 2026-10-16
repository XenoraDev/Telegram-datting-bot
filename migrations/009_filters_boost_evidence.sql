-- 009: advanced browse filters, profile boost, advisory payment-screenshot analysis.
-- Additive only: no existing row is changed or deleted.

-- Advanced filters of a user (Pro / VIP entitlement `advanced_filters`; the entitlement is
-- re-checked on every use, so a lapsed subscription simply stops applying them). One row per user.
CREATE TABLE browse_filters (
    user_id     BIGINT      PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    age_min     SMALLINT    CHECK (age_min BETWEEN 18 AND 99),
    age_max     SMALLINT    CHECK (age_max BETWEEN 18 AND 99),
    gender      TEXT        CHECK (gender IN ('m', 'f', 'o')),
    city        TEXT        CHECK (city IS NULL OR char_length(city) BETWEEN 1 AND 40),
    looking_for TEXT        CHECK (looking_for IN ('m', 'f', 'any')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (age_min IS NULL OR age_max IS NULL OR age_min <= age_max)
);

-- Profile boost (entitlement `profile_boost`). One row per user, so "one boost at a time" is a
-- primary-key fact, not application logic: activation is a single INSERT .. ON CONFLICT DO UPDATE
-- .. WHERE statement that only succeeds once the previous cooldown is over. A boost counts while
-- ends_at > now() (evaluated by PostgreSQL), so it expires on its own: no job is needed.
CREATE TABLE profile_boosts (
    user_id        BIGINT      PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    started_at     TIMESTAMPTZ NOT NULL,
    ends_at        TIMESTAMPTZ NOT NULL,
    cooldown_until TIMESTAMPTZ NOT NULL,
    CHECK (ends_at > started_at AND cooldown_until >= ends_at)
);

-- What the analyser could read from a payment screenshot. ADVISORY ONLY: an admin still decides.
-- Only extracted fields are kept, never the raw OCR text. The row belongs to the stored image and
-- is deleted with it (the existing evidence retention), or when the image is replaced.
CREATE TABLE payment_evidence_analysis (
    payment_id     BIGINT      PRIMARY KEY REFERENCES payment_evidence (payment_id) ON DELETE CASCADE,
    sha256         TEXT        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    verdict        TEXT        NOT NULL
        CHECK (verdict IN ('VALIDATION_OK', 'VALIDATION_WARNING', 'VALIDATION_MISMATCH',
                           'VALIDATION_UNREADABLE')),
    engine         TEXT        NOT NULL CHECK (char_length(engine) <= 32),
    amount_minor   INTEGER     CHECK (amount_minor IS NULL OR amount_minor BETWEEN 1 AND 100000000),
    payee          TEXT        CHECK (payee IS NULL OR char_length(payee) <= 80),
    utr            TEXT        CHECK (utr IS NULL OR utr ~ '^[A-Z0-9]{6,35}$'),
    paid_at        TIMESTAMP,  -- as printed on the screenshot (India time), no zone
    success_marker BOOLEAN,
    findings       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    analyzed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

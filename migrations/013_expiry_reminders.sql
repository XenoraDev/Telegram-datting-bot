-- 013: "your plan ends soon" reminders. Additive: one new table, nothing existing is changed.
-- One row says "this user was reminded about the subscription end that falls on period_end", so a
-- reminder is sent once per end date (a renewal moves the end date, so the next end is reminded
-- again). The row holds no text and nothing personal; rows older than 30 days are tidied up.

CREATE TABLE expiry_reminders (
    user_id    BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    period_end TIMESTAMPTZ NOT NULL,
    sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, period_end)
);
CREATE INDEX expiry_reminders_end ON expiry_reminders (period_end);

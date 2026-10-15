-- 004: P2 browse + like/pass + mutual match. Purely additive: no existing row is changed.

-- Opaque, stable handle of a profile for button payloads, so Telegram user ids never appear in
-- callback data. Existing rows get their own random value (volatile default).
ALTER TABLE profiles ADD COLUMN public_id UUID NOT NULL DEFAULT gen_random_uuid();
CREATE UNIQUE INDEX profiles_public_id ON profiles (public_id);

CREATE TABLE swipes (
    id         BIGSERIAL   PRIMARY KEY,
    swiper_id  BIGINT      NOT NULL REFERENCES users (id),
    target_id  BIGINT      NOT NULL REFERENCES users (id),
    action     TEXT        NOT NULL CHECK (action IN ('like', 'pass')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (swiper_id <> target_id),
    UNIQUE (swiper_id, target_id)
);
-- "who liked me" lookups (browse ordering, mutual-like check)
CREATE INDEX swipes_target_like ON swipes (target_id, swiper_id) WHERE action = 'like';

-- One row per pair: the smaller user id is always user_a_id, so (A,B) and (B,A) collide.
CREATE TABLE matches (
    id         BIGSERIAL   PRIMARY KEY,
    user_a_id  BIGINT      NOT NULL REFERENCES users (id),
    user_b_id  BIGINT      NOT NULL REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (user_a_id < user_b_id),
    UNIQUE (user_a_id, user_b_id)
);
CREATE INDEX matches_user_b ON matches (user_b_id);

-- Reports filed from a browsed profile (no chat session): at most one open per pair.
CREATE UNIQUE INDEX one_open_profile_report
    ON reports (reporter_id, reported_id)
    WHERE source = 'profile' AND status = 'open';

-- 002: P1 schema: users + 18+ gate, profiles, anonymous chat sessions, blocks, reports,
-- restrictions, deletion tombstones. NO message content is ever stored here.

CREATE TABLE users (
    id               BIGINT      PRIMARY KEY,           -- Telegram user id
    username         TEXT,
    language         TEXT        NOT NULL DEFAULT 'en',
    status           TEXT        NOT NULL DEFAULT 'onboarding'
        CHECK (status IN ('onboarding', 'active', 'banned', 'blocked_underage', 'deleted')),
    status_reason    TEXT,
    banned_until     TIMESTAMPTZ,
    age_confirmed_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- DB-level 18+ gate: nobody can be 'active' without having confirmed they are an adult.
    CONSTRAINT active_requires_age_confirmation
        CHECK (status <> 'active' OR age_confirmed_at IS NOT NULL)
);

CREATE TABLE profiles (
    user_id     BIGINT      PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    name        TEXT        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 30),
    -- DB-level 18+ gate: a profile for a minor cannot exist. (Upper bound is app policy.)
    age         SMALLINT    NOT NULL CHECK (age >= 18),
    gender      TEXT        NOT NULL CHECK (gender IN ('m', 'f', 'o')),
    looking_for TEXT        NOT NULL CHECK (looking_for IN ('m', 'f', 'any')),
    city        TEXT        NOT NULL CHECK (char_length(city) BETWEEN 1 AND 40),
    bio         TEXT        NOT NULL CHECK (char_length(bio) <= 300),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE chat_queue (
    user_id   BIGINT      PRIMARY KEY REFERENCES users (id),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX chat_queue_joined_at ON chat_queue (joined_at);

-- chat_sessions + chat_session_members are the ONLY source of truth for who is chatting
-- with whom (there is deliberately no users.partner_id). Metadata only, no messages.
CREATE TABLE chat_sessions (
    id         BIGSERIAL   PRIMARY KEY,
    user_a     BIGINT      NOT NULL REFERENCES users (id),
    user_b     BIGINT      NOT NULL REFERENCES users (id),
    status     TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at   TIMESTAMPTZ,
    ended_by   BIGINT      REFERENCES users (id),
    end_reason TEXT,
    CHECK (user_a <> user_b),
    CHECK ((status = 'active') = (ended_at IS NULL))
);
CREATE INDEX chat_sessions_user_a ON chat_sessions (user_a, ended_at);
CREATE INDEX chat_sessions_user_b ON chat_sessions (user_b, ended_at);

CREATE TABLE chat_session_members (
    session_id BIGINT  NOT NULL REFERENCES chat_sessions (id) ON DELETE CASCADE,
    user_id    BIGINT  NOT NULL REFERENCES users (id),
    active     BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (session_id, user_id)
);
-- The database itself guarantees a user is in at most ONE active session.
CREATE UNIQUE INDEX one_active_session_per_user ON chat_session_members (user_id) WHERE active;

CREATE TABLE blocks (
    blocker_id BIGINT      NOT NULL REFERENCES users (id),
    blocked_id BIGINT      NOT NULL REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (blocker_id, blocked_id),
    CHECK (blocker_id <> blocked_id)
);
CREATE INDEX blocks_blocked ON blocks (blocked_id);

CREATE TABLE reports (
    id                 BIGSERIAL   PRIMARY KEY,
    reporter_id        BIGINT      NOT NULL REFERENCES users (id),
    reported_id        BIGINT      NOT NULL REFERENCES users (id),
    session_id         BIGINT      REFERENCES chat_sessions (id) ON DELETE SET NULL,
    source             TEXT        NOT NULL CHECK (source IN ('chat', 'profile')),
    reason             TEXT        NOT NULL
        CHECK (reason IN ('spam', 'harassment', 'sexual', 'minor', 'scam', 'other')),
    -- Last <=10 text messages around the report, only captured when a report is filed.
    -- Nulled by the maintenance job after context_expires_at (30 days).
    context            JSONB,
    status             TEXT        NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'actioned', 'dismissed')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    context_expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 days',
    CHECK (reporter_id <> reported_id)
);
CREATE UNIQUE INDEX one_open_report_per_session
    ON reports (reporter_id, reported_id, session_id)
    WHERE status = 'open' AND session_id IS NOT NULL;
CREATE INDEX reports_reported ON reports (reported_id, created_at);
CREATE INDEX reports_reporter ON reports (reporter_id, created_at);
CREATE INDEX reports_context_expiry ON reports (context_expires_at) WHERE context IS NOT NULL;

CREATE TABLE restrictions (
    id         BIGSERIAL   PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES users (id),
    kind       TEXT        NOT NULL CHECK (kind IN ('mute', 'chat_cooldown')),
    until      TIMESTAMPTZ NOT NULL,
    reason     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX restrictions_user_until ON restrictions (user_id, until);

-- Only the id + why, kept so banned / underage users cannot simply come back.
-- No FK on purpose: it must survive any future purge of the users row.
CREATE TABLE deletion_tombstones (
    user_id    BIGINT      PRIMARY KEY,
    reason     TEXT        NOT NULL CHECK (reason IN ('banned', 'underage')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

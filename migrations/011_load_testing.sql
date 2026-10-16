-- 011: Server Capacity / Load Testing (admin module). Additive only: no existing table, row or
-- constraint is changed. These tables hold test configuration, aggregated metrics and reports.
-- They never hold a secret (no tokens, passwords or URLs) and never a chat message.

-- One row per load test. A job is created ONLY after the admin pressed the Telegram confirmation;
-- the separate worker process claims it, runs the synthetic workload and writes the result back.
CREATE TABLE load_test_runs (
    id                  BIGSERIAL   PRIMARY KEY,
    status              TEXT        NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'starting', 'running', 'stopping',
                          'completed', 'cancelled', 'aborted', 'blocked', 'failed')),
    config              JSONB       NOT NULL,   -- profile, chats, msgs_per_chat_min, duration_s, ramp_s
    config_hash         TEXT        NOT NULL CHECK (config_hash ~ '^[0-9a-f]{64}$'),
    thresholds          JSONB       NOT NULL,   -- the PASS/WARNING/FAIL limits this run was judged by
    requested_by        BIGINT      NOT NULL,   -- admin id (no FK: audit survives user purges)
    environment         JSONB,                  -- result of the safety checks (names + verdicts only)
    server_info         JSONB,                  -- cpu count, memory, docker / postgres / app versions
    commit_hash         TEXT        CHECK (commit_hash IS NULL OR char_length(commit_hash) <= 64),
    worker_id           TEXT        CHECK (worker_id IS NULL OR char_length(worker_id) <= 80),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    heartbeat_at        TIMESTAMPTZ,
    cancel_requested_by BIGINT,
    cancel_requested_at TIMESTAMPTZ,
    stop_reason         TEXT        CHECK (stop_reason IS NULL OR char_length(stop_reason) <= 300),
    summary             JSONB,
    result              TEXT        CHECK (result IN ('PASS', 'WARNING', 'FAIL')),
    result_reasons      JSONB,
    partial             BOOLEAN     NOT NULL DEFAULT false,
    CHECK ((status IN ('completed', 'cancelled', 'aborted', 'blocked', 'failed'))
           = (finished_at IS NOT NULL))
);
CREATE INDEX load_test_runs_created ON load_test_runs (created_at DESC);
-- The database itself guarantees that two load tests can never be active at the same time.
CREATE UNIQUE INDEX load_test_one_active ON load_test_runs ((true))
    WHERE status IN ('queued', 'starting', 'running', 'stopping');

-- Liveness of load-test worker processes, so the admin page can show "worker online / offline"
-- even when no test is running.
CREATE TABLE load_test_workers (
    worker_id    TEXT        PRIMARY KEY CHECK (char_length(worker_id) <= 80),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    state        TEXT        NOT NULL DEFAULT 'idle' CHECK (state IN ('idle', 'busy', 'blocked')),
    detail       TEXT        CHECK (detail IS NULL OR char_length(detail) <= 200)
);

-- Aggregated windows (one row per few seconds), never one row per simulated message.
CREATE TABLE load_test_metrics (
    run_id         BIGINT           NOT NULL REFERENCES load_test_runs (id) ON DELETE CASCADE,
    seq            INTEGER          NOT NULL,
    ts             TIMESTAMPTZ      NOT NULL,
    elapsed_s      REAL             NOT NULL,
    active_chats   INTEGER          NOT NULL,
    target_msgs_s  REAL             NOT NULL,
    msgs_s         REAL             NOT NULL,
    sent_ok        INTEGER          NOT NULL,
    sent_failed    INTEGER          NOT NULL,
    db_errors      INTEGER          NOT NULL DEFAULT 0,
    p50_ms         REAL,
    p95_ms         REAL,
    p99_ms         REAL,
    max_ms         REAL,
    cpu_pct        REAL,
    ram_pct        REAL,
    swap_pct       REAL,
    worker_cpu_pct REAL,
    loop_lag_ms    REAL,
    in_flight      INTEGER,
    db_connections INTEGER,
    db_conn_pct    REAL,
    db_latency_ms  REAL,
    bot_ok         BOOLEAN,
    web_ok         BOOLEAN,
    PRIMARY KEY (run_id, seq)
);

-- One-time Telegram confirmations. Only the SHA-256 of the random token is stored.
CREATE TABLE load_test_confirmations (
    id            BIGSERIAL   PRIMARY KEY,
    token_hash    TEXT        NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    admin_id      BIGINT      NOT NULL,
    config        JSONB       NOT NULL,
    config_hash   TEXT        NOT NULL CHECK (config_hash ~ '^[0-9a-f]{64}$'),
    state         TEXT        NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'consumed', 'cancelled', 'superseded', 'expired', 'failed')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    decided_at    TIMESTAMPTZ,
    run_id        BIGINT      REFERENCES load_test_runs (id),
    tg_chat_id    BIGINT,
    tg_message_id BIGINT,
    CHECK (expires_at > created_at)
);
CREATE INDEX load_test_confirmations_pending ON load_test_confirmations (admin_id) WHERE state = 'pending';

-- Admin-editable PASS / WARNING / FAIL thresholds (key -> number). Missing keys use the defaults.
CREATE TABLE load_test_settings (
    key        TEXT        PRIMARY KEY CHECK (key ~ '^[a-z0-9_]{1,40}$'),
    value      DOUBLE PRECISION NOT NULL,
    updated_by BIGINT      NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Registry of the synthetic users of a run. Synthetic user ids are NEGATIVE (Telegram ids are
-- always positive), so they can never collide with, or be mistaken for, a real person.
CREATE TABLE load_test_users (
    user_id BIGINT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE CHECK (user_id < 0),
    run_id  BIGINT NOT NULL REFERENCES load_test_runs (id) ON DELETE CASCADE
);
CREATE INDEX load_test_users_run ON load_test_users (run_id);

-- Append-only audit trail of everything done with the module.
CREATE TABLE load_test_audit (
    id         BIGSERIAL   PRIMARY KEY,
    admin_id   BIGINT      NOT NULL,
    action     TEXT        NOT NULL
        CHECK (action IN ('configured', 'confirmation_sent', 'confirmed', 'confirmation_cancelled',
                          'confirmation_expired', 'started', 'stop_requested', 'stopped',
                          'completed', 'aborted', 'cancelled', 'blocked', 'failed', 'exported',
                          'thresholds_saved')),
    run_id     BIGINT      REFERENCES load_test_runs (id),
    detail     TEXT        CHECK (detail IS NULL OR char_length(detail) <= 300),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX load_test_audit_created ON load_test_audit (created_at DESC);

CREATE FUNCTION load_test_audit_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'load_test_audit is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER load_test_audit_no_change
    BEFORE UPDATE OR DELETE ON load_test_audit
    FOR EACH ROW EXECUTE FUNCTION load_test_audit_append_only();

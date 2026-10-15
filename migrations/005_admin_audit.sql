-- 005: P3 in-bot admin audit trail. Additive; no existing table is changed.
-- Append-only: rows can be neither updated nor deleted. No foreign keys on purpose, so the log
-- survives any later purge of users / reports. It stores ids and the action, never chat text.

CREATE TABLE admin_actions (
    id             BIGSERIAL   PRIMARY KEY,
    admin_id       BIGINT      NOT NULL,
    action         TEXT        NOT NULL
        CHECK (action IN ('ban', 'unban', 'report_resolve', 'report_dismiss', 'report_ban')),
    target_user_id BIGINT,
    report_id      BIGINT,
    detail         TEXT        CHECK (detail IS NULL OR char_length(detail) <= 200),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX admin_actions_created ON admin_actions (created_at DESC);
CREATE INDEX admin_actions_target ON admin_actions (target_user_id, created_at DESC);

CREATE FUNCTION admin_actions_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'admin_actions is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER admin_actions_no_change
    BEFORE UPDATE OR DELETE ON admin_actions
    FOR EACH ROW EXECUTE FUNCTION admin_actions_append_only();

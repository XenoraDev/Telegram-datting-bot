-- 012: support tickets. Additive: two new tables and one widened CHECK on admin_actions.
-- A user writes to support (category, then text); an admin answers from the web panel. The text
-- lives only in support_messages.ticket_text and is deleted with the account (/deleteme). It is
-- never copied into admin_actions (the audit log keeps ids and the action, nothing a user wrote).

CREATE TABLE support_tickets (
    id         BIGSERIAL   PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES users (id),
    category   TEXT        NOT NULL
        CHECK (category IN ('payment', 'account', 'chat', 'report', 'other')),
    -- open: waiting for support; answered: waiting for the user; closed: finished
    status     TEXT        NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'answered', 'closed')),
    closed_by  TEXT        CHECK (closed_by IN ('user', 'admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at  TIMESTAMPTZ,
    CHECK ((status = 'closed') = (closed_at IS NOT NULL)),
    CHECK ((status = 'closed') = (closed_by IS NOT NULL))
);
CREATE INDEX support_tickets_user   ON support_tickets (user_id, id DESC);
CREATE INDEX support_tickets_status ON support_tickets (status, updated_at DESC);

CREATE TABLE support_messages (
    id          BIGSERIAL   PRIMARY KEY,
    ticket_id   BIGINT      NOT NULL REFERENCES support_tickets (id) ON DELETE CASCADE,
    sender      TEXT        NOT NULL CHECK (sender IN ('user', 'admin')),
    admin_id    BIGINT,
    ticket_text TEXT        NOT NULL CHECK (char_length(ticket_text) BETWEEN 1 AND 2000),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((sender = 'admin') = (admin_id IS NOT NULL))
);
CREATE INDEX support_messages_ticket ON support_messages (ticket_id, id);

-- At most 3 unfinished tickets per user, enforced by the database (the service checks first for a
-- friendly answer). The advisory lock serialises two simultaneous inserts of the same user.
CREATE FUNCTION support_tickets_limit() RETURNS trigger AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('support_tickets', NEW.user_id));
    IF (SELECT count(*) FROM support_tickets
        WHERE user_id = NEW.user_id AND status <> 'closed') >= 3 THEN
        RAISE EXCEPTION 'too many open support tickets' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER support_tickets_max_open
    BEFORE INSERT ON support_tickets
    FOR EACH ROW EXECUTE FUNCTION support_tickets_limit();

-- Audit: the two new admin actions (the table stays append-only). Every value 007 allowed stays.
ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;
ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check CHECK (action IN (
    'ban', 'unban', 'report_resolve', 'report_dismiss', 'report_ban',
    'payment_approve', 'payment_reject', 'payment_config', 'plan_save',
    'ticket_reply', 'ticket_close'));

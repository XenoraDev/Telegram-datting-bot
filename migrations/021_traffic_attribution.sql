-- 021: traffic source / campaign attribution. Additive only: two new tables, one trigger, and the
-- admin_actions action CHECK widened (every value allowed so far stays allowed).
--
-- campaigns:        a generic source + campaign list. `source` is a free key (instagram, youtube,
--                   channel_a ...): no source name is known to the code. `code` is the random
--                   part of the Telegram start link (c_<code>). Campaigns are never deleted,
--                   only paused.
-- user_acquisition: ONE row per NEW user, written once when the account is first created, never
--                   changed (a trigger refuses UPDATEs: first touch is immutable). kind is
--                   'campaign' (a working link), 'direct' (no link) or 'invalid' (a link that was
--                   malformed, unknown or paused: only the LINK was invalid, never the user; the
--                   raw text is never stored). Existing users get no row: nothing is invented.
--                   The row goes with the account (ON DELETE CASCADE / the delete-account scrub).
-- No IP, URL, device data, username or message content is stored here.
CREATE TABLE campaigns (
    id         BIGSERIAL   PRIMARY KEY,
    code       TEXT        NOT NULL UNIQUE CHECK (code ~ '^[a-z2-7]{8}$'),
    source     TEXT        NOT NULL CHECK (source ~ '^[a-z][a-z0-9_]{1,31}$'),
    name       TEXT        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 60),
    status     TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
    is_test    BOOLEAN     NOT NULL DEFAULT false,
    created_by BIGINT      NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX campaigns_source ON campaigns (source);

CREATE TABLE user_acquisition (
    user_id     BIGINT      PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    kind        TEXT        NOT NULL CHECK (kind IN ('campaign', 'direct', 'invalid')),
    campaign_id BIGINT      REFERENCES campaigns (id),
    source      TEXT        CHECK (source IS NULL OR source ~ '^[a-z][a-z0-9_]{1,31}$'),
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- a working link names its campaign and source; a direct start names neither; an invalid
    -- link may remember the (paused) campaign it pointed to, but is never attributed to a source.
    CONSTRAINT acquisition_shape CHECK (
        (kind = 'campaign' AND campaign_id IS NOT NULL AND source IS NOT NULL)
        OR (kind = 'direct' AND campaign_id IS NULL AND source IS NULL)
        OR (kind = 'invalid' AND source IS NULL))
);
CREATE INDEX user_acquisition_campaign ON user_acquisition (campaign_id, acquired_at);
CREATE INDEX user_acquisition_when ON user_acquisition (acquired_at);

-- First touch is immutable: nothing about an acquisition row can be changed afterwards
-- (it can only disappear with the account).
CREATE FUNCTION user_acquisition_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'first-touch attribution is immutable';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER user_acquisition_no_update BEFORE UPDATE ON user_acquisition
    FOR EACH ROW EXECUTE FUNCTION user_acquisition_guard();

ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;
ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check CHECK (action IN (
    'ban', 'unban', 'report_resolve', 'report_dismiss', 'report_ban',
    'payment_approve', 'payment_reject', 'payment_config', 'plan_save',
    'ticket_reply', 'ticket_close',
    'payment_auto_approve', 'payment_auto_setting',
    'sub_grant', 'sub_cancel', 'sub_extend', 'sub_revoke',
    'sub_shorten',
    'campaign_create', 'campaign_status'));

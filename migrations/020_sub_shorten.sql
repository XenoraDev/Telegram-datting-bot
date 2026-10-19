-- 020: audit action for shortening a subscription (S3). Additive only.
--
-- 1. The CHECK on admin_actions is widened with 'sub_shorten'; every value allowed so far stays
--    allowed and no existing audit row is touched.
-- 2. A payment's extension can be taken off its subscription ONCE: this partial unique index makes
--    a second removal (a double submit or two admins at the same moment) impossible in the
--    database itself, not only in the code.
ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;
ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check CHECK (action IN (
    'ban', 'unban', 'report_resolve', 'report_dismiss', 'report_ban',
    'payment_approve', 'payment_reject', 'payment_config', 'plan_save',
    'ticket_reply', 'ticket_close',
    'payment_auto_approve', 'payment_auto_setting',
    'sub_grant', 'sub_cancel', 'sub_extend', 'sub_revoke',
    'sub_shorten'));

CREATE UNIQUE INDEX admin_actions_one_removal_per_payment ON admin_actions (payment_id)
    WHERE action = 'sub_shorten' AND payment_id IS NOT NULL;

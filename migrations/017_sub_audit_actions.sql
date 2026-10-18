-- 017: audit action names for subscription management (grant / cancel / extend / revoke).
-- Additive only: the CHECK on admin_actions is widened; every value allowed so far stays allowed
-- and no existing audit row is touched. Nothing writes the new actions yet (a later phase does).
ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;
ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check CHECK (action IN (
    'ban', 'unban', 'report_resolve', 'report_dismiss', 'report_ban',
    'payment_approve', 'payment_reject', 'payment_config', 'plan_save',
    'ticket_reply', 'ticket_close',
    'payment_auto_approve', 'payment_auto_setting',
    'sub_grant', 'sub_cancel', 'sub_extend', 'sub_revoke'));

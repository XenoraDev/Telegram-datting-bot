-- 015: optional automatic payment approval. Additive only; nothing existing is changed or deleted.
--
--  * payment_settings.auto_approve   the admin switch. DEFAULT false = manual approval, exactly as
--                                    before. Turning it on is an explicit admin action.
--  * payments.approval_mode          how an approved payment was approved: 'manual' (an admin) or
--                                    'auto' (the verified-evidence rule). Every existing row is
--                                    'manual', which is what happened to it.
--  * admin_actions                   two more audit actions (the table stays append-only). Every
--                                    value allowed so far stays allowed.
ALTER TABLE payment_settings
    ADD COLUMN auto_approve            BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN auto_approve_changed_by BIGINT,
    ADD COLUMN auto_approve_changed_at TIMESTAMPTZ;

ALTER TABLE payments
    ADD COLUMN approval_mode TEXT NOT NULL DEFAULT 'manual'
        CHECK (approval_mode IN ('manual', 'auto'));

ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;
ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check CHECK (action IN (
    'ban', 'unban', 'report_resolve', 'report_dismiss', 'report_ban',
    'payment_approve', 'payment_reject', 'payment_config', 'plan_save',
    'ticket_reply', 'ticket_close',
    'payment_auto_approve', 'payment_auto_setting'));

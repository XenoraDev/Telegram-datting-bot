-- 019: "live" support replies. Additive only.
--
-- After support answers, the user can tap "Reply to support" and then just type: their messages go
-- to THAT ticket without opening /tickets each time. The chosen ticket is stored here, with a
-- deadline, so it survives a bot restart and expires by itself:
--   live_until   NULL = not in live mode; otherwise the moment the live mode ends. Every live
--                message pushes it forward; a closed ticket can never be live (CHECK below, and
--                closing a ticket clears it).
-- No message text is stored here: messages still go to support_messages exactly as before.
ALTER TABLE support_tickets ADD COLUMN live_until TIMESTAMPTZ;
ALTER TABLE support_tickets
    ADD CONSTRAINT support_live_not_closed CHECK (live_until IS NULL OR status <> 'closed');
CREATE INDEX support_tickets_live ON support_tickets (user_id) WHERE live_until IS NOT NULL;

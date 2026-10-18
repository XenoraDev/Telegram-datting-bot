-- 016: when a chat last had a message, so a silent chat can be closed after N minutes. Additive
-- only. Only a timestamp is stored: never message text, never who wrote it.
--
-- Existing active chats get the time this migration runs, so none is closed the moment it ships.
ALTER TABLE chat_sessions ADD COLUMN last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX chat_sessions_active_idle ON chat_sessions (last_activity_at) WHERE status = 'active';

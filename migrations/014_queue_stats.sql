-- 014: random-chat queue statistics. Additive: one new table, nothing existing is changed.
-- Aggregated counters per UTC day and hour: no user id, no text, nothing personal. They answer
-- "how long do people wait, how many searches end without a partner, and when are people online".

CREATE TABLE queue_stats (
    day              DATE     NOT NULL,
    hour             SMALLINT NOT NULL CHECK (hour BETWEEN 0 AND 23),
    instant          INTEGER  NOT NULL DEFAULT 0 CHECK (instant >= 0),   -- paired at once
    waited           INTEGER  NOT NULL DEFAULT 0 CHECK (waited >= 0),    -- paired after waiting
    wait_sum_seconds BIGINT   NOT NULL DEFAULT 0 CHECK (wait_sum_seconds >= 0),
    expired          INTEGER  NOT NULL DEFAULT 0 CHECK (expired >= 0),   -- the search timed out
    cancelled        INTEGER  NOT NULL DEFAULT 0 CHECK (cancelled >= 0), -- the user pressed stop
    PRIMARY KEY (day, hour)
);

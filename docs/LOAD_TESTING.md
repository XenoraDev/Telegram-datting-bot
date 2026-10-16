# Server Capacity / Load Testing

> ## ⚠️ Never run load testing against production.
> This module exists to answer one question in an **isolated testing environment**:
> *how many concurrent active 1-to-1 chats can this server comfortably handle?*
> It refuses to start unless the environment proves it is a test environment, and there is no
> bypass. Do not point it at the live bot, the live database or the live domain.

Admin → **Server capacity** (`/admin/loadtest`).

## What is measured

* **Active chat**: one pair of synthetic users exchanging messages inside a chat session.
* **Chats vs messages per second**: the load is *chats × messages per chat per minute*.
  250 chats × 50 msg/min/chat = 12,500 msg/min = 208.33 msg/sec. The same 12,500 msg/min can be
  250 busy chats or 1,250 quiet ones, which stress the server differently, so both numbers are
  configured and reported.
* The exact capacity is **never hard-coded**. It is calculated from the tests you ran (below).

### What the workload does

For every synthetic message the harness runs the same code the bot runs for a real chat message:
`repo_users.get` (the user-context middleware), the `message` rate limiter (throttle middleware)
and `ChatRelayService.relay` (partner lookup, mute check, content rules, contact tracker,
per-sender rate limit, flood filter, RAM context buffer). Only the last step, `bot.send_message`
to the partner, is replaced by a counter (`NullDelivery`). Blocks, bans, privacy, rate limits and
content rules are therefore **not** bypassed. Synthetic chat sessions are inserted directly
(finding a match is a different code path) and chat history is never stored.

### Why real Telegram messages are not used

Thousands of real messages would spam Telegram, hit the Bot API rate limits (roughly 30 messages
per second per bot), need thousands of real accounts and contact real people. The workload
module owns no Telegram client at all (a test fails if `aiogram` is ever imported there).

**Consequence:** the report measures your server (CPU, RAM, PostgreSQL, the relay logic). It does
**not** measure Telegram's own limits, network latency to Telegram or the bot's outgoing queue. A
real chat load of 12,500 msg/min is also far above what Telegram lets one bot send, so treat the
capacity as the *server-side* ceiling.

## Production protection

The tester refuses to run (UI start disabled, worker marks the job *blocked*) unless **all** of
these hold. They are declared by the operator in the testing environment's `.env`:

| Check | Setting |
|---|---|
| Explicit test environment | `APP_ENV=testing` and `LOAD_TEST_ENABLED=1` (`production`, `prod`, `live`, `staging` or unset is refused) |
| Explicit test database | `LOAD_TEST_DB_NAME=<name>` must equal the database in the connection URL and contain `test` |
| Database identity | the database carries the comment `load-test-environment:<LOAD_TEST_ENV_ID>` (see below) |
| No production-looking database | database name, host and user must not contain `prod`, `production`, `live`, `master`, `primary`; `LOAD_TEST_PRODUCTION_DB_NAMES` is also refused |
| Test Telegram bot only | `LOAD_TEST_BOT_ID` must equal the id of `BOT_TOKEN` and must not be in `LOAD_TEST_PRODUCTION_BOT_IDS` |
| Test domain only | `ADMIN_DOMAIN` must be in `LOAD_TEST_ALLOWED_DOMAINS` and not in `LOAD_TEST_PRODUCTION_DOMAINS` |
| Health probe stays internal | `LOAD_TEST_WEB_HEALTH_URL` (optional) must be plain `http` to localhost or a compose service name |
| No real Telegram traffic | structural: the workload has no Telegram client |
| Isolation | no user with a negative id exists that this module did not create |

`localhost` proves nothing and is never assumed to be a test environment. The checks run when you
open the page, when you request the confirmation, again when you press CONFIRM in Telegram, and a
last time in the worker before the workload starts. Secrets are never stored or logged: only
derived facts (database name, host, bot id) are used, never the token, password or URL.

### Mark the testing database once

```sql
-- in the TESTING database only
COMMENT ON DATABASE dating_loadtest IS 'load-test-environment:env-testing-7f3a9c21';
```

and put the same id into the testing `.env`: `LOAD_TEST_ENV_ID=env-testing-7f3a9c21`.

## Setting up the testing environment

Use a **separate compose project** (separate database volume, separate bot token, separate
domain). Never reuse the production `.env`.

```
cp .env.example .env.loadtest      # then fill in the testing values, see .env.loadtest.example
docker compose -p dating-bot-loadtest --env-file .env.loadtest --profile web --profile loadtest \
    up -d --build db bot web loadtest-worker
docker compose -p dating-bot-loadtest exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c "COMMENT ON DATABASE \"$POSTGRES_DB\" IS 'load-test-environment:<LOAD_TEST_ENV_ID>'"
```

Always pass `-p dating-bot-loadtest` (the compose file pins the production project name
otherwise). The worker is the `loadtest-worker` service (`python -m bot.loadtest.worker`); it is
off unless the `loadtest` profile is enabled. Nothing starts a test automatically.

Operator limits (the admin UI can only choose lower values): `LOAD_TEST_MAX_CHATS` (default 2000,
hard ceiling 4000), `LOAD_TEST_MAX_MSGS_PER_CHAT_MIN` (120), `LOAD_TEST_MAX_TOTAL_MSGS_PER_MIN`
(100000), `LOAD_TEST_MAX_DURATION_S` (1800), `LOAD_TEST_MAX_RAMP_S` (900),
`LOAD_TEST_MAX_TOTAL_MESSAGES` (3000000), `LOAD_TEST_POOL_SIZE` (10, the bot's own pool size).
`LOAD_TEST_DOCKER_VERSION` is stored in reports if you set it (a container cannot read it).

## Configuring and starting a test

1. **Configuration.** Pick Light (50 × 40), Medium (100 × 40), Heavy (250 × 50), Stress (500 × 50)
   or Custom: concurrent chats, messages per chat per minute, duration at full load, ramp-up.
   The page calculates msg/min, msg/sec and the estimated total. Zero, negative, fractional,
   malformed or over-limit values are rejected, never clamped. The presets are workloads, not
   capacity claims.
2. **Review.** *Review configuration* shows the exact test (environment, chats, rate, duration,
   ramp-up, estimated messages, profile) and the Safety Status. If any light is red, *Send
   Confirmation* is disabled.
3. **Telegram confirmation.** *Send Confirmation to Telegram* sends a message with
   **✅ CONFIRM LOAD TEST / ❌ CANCEL** to your Telegram account (English or Hindi, your bot
   language). **Nothing starts before you press CONFIRM.** The token is random, one-time, valid 5
   minutes, bound to you and to the exact configuration (only its SHA-256 is stored). Changing
   *any* field and reviewing again invalidates the earlier confirmation; an old token can never
   start a different configuration; a second token supersedes the first; pressing it twice does
   nothing the second time.
4. **Run.** CONFIRM creates a job. The separate worker claims it, re-checks the environment,
   creates the synthetic users, runs the workload and writes the report. A job that no worker
   claims within 15 minutes is cancelled. Only one test can be active (enforced by the database).

The web request never runs the workload; the page refreshes itself every 5 seconds while a test is
active (no JavaScript is needed).

### Stopping

**STOP TEST** is graceful: the load stops, a *partial* report is written and the synthetic rows are
removed. It never stops PostgreSQL, restarts services, deletes containers or volumes.

### Automatic fail-safe

The worker stops the test (status *aborted*, result FAIL, partial report with the reason) when:
RAM stays at/above the critical level for two windows, CPU stays critical for the configured
time, database connections are exhausted, database errors exceed the limit, a monitored service
fails its health checks repeatedly, the database server restarts, the load generator stalls, or
the worker loses its database. A worker that dies is detected by the admin page after 45 seconds
and its run is ended with a partial report. Production services are never restarted.

## Reading the result

| Result | Meaning |
|---|---|
| 🟢 **PASS** | every measurement stayed below its warning level |
| 🟡 **WARNING** | at least one measurement is above a warning level (resources high, latency degraded, small error increase, throughput slightly below target) |
| 🔴 **FAIL** | a fail level was reached (high error rate, severe latency, sustained rate far below target, DB connection exhaustion, resource exhaustion), a monitored service restarted, or the fail-safe stopped the test |

Every verdict lists the measurement that caused it. The default thresholds (error rate 0.5 % / 2 %,
P95 500 / 1500 ms, CPU 80 / 95 %, RAM 80 / 92 %, throughput 97 / 90 % of target, DB connections
70 / 90 %, ...) are **reasonable starting values, not scientifically proven**. Change them under
*Thresholds*; each report keeps the thresholds it was judged by, so old verdicts stay explainable.
CPU is judged on its 95th percentile over the run, not on a single spike (the peak is shown too).
Latency is measured from the moment a message was *due* to the moment the relay finished, so a
saturated system cannot hide queueing delay. If the load generator itself runs out of CPU the
result carries a warning (the server may be able to do more than measured).

Bot and web health are observed when possible (bot: it holds database connections; web: the
optional `LOAD_TEST_WEB_HEALTH_URL`); a service that is not observable is shown as *not
observed*, and restarts are only counted for observed services.

## Observed capacity

Calculated from finished tests, per hardware configuration (CPU count + memory), newest result per
chat level:

1. Only tests that finished (completed, or stopped by the fail-safe) count. Tests you stopped
   yourself (partial) are shown in the history but never counted.
2. Only comparable workloads count: 40–60 msg/chat/min and at least 2 minutes at full load.
3. Walking the levels upwards, **recommended capacity** is the highest level reached before the
   first level that is not a PASS *inside the safety margins* (CPU ≤ 70 %, RAM ≤ 75 %, P95 ≤ 300 ms,
   errors ≤ 0.1 % by default; configurable).
4. **Warning zone** is that first level if it ended in WARNING (or passed without the margins).
   **Observed failure** is the lowest FAIL level.
5. A better result at a higher level after a worse one is ignored. Nothing above the highest tested
   level is ever claimed.

The page prints the reason, for example: *"250 chats recommended because the 250-chat test passed
with 0.02 % errors and P95 180 ms, while the 500-chat test entered WARNING (CPU 82 %, P95 620 ms)."*

## History, comparison, export

* **Reports**: id, date, chats, msg/min/chat, duration, P95, peak CPU, peak RAM, error rate,
  result. Old reports are never deleted automatically. Each stores the CPU count, memory,
  PostgreSQL / Python (and optional Docker) versions and the application commit, never secrets.
* **Compare** any two tests: each measurement is marked improved / degraded / same; the page warns
  when workload or hardware differ. Before/after an upgrade: re-run the same level on the new
  server; the capacity summary then shows a second hardware line.
* **Export**: *Download CSV* (every measurement window) and *Download JSON* (the whole report) for
  finished tests; admin session required; audited. They contain no tokens, passwords, URLs or
  message text.

## A safe 50 → 100 → 250 → 500 → 1000 sequence

1. Make sure nobody else uses the testing server. Start with **Light (50 × 40)**.
2. Review → confirm in Telegram → watch the live page. After it completes, read *Why this result*.
3. Only on a 🟢 PASS does the page offer *Prepare next recommended test*; it merely fills the form
   (same rate, next level). You still review and confirm it in Telegram. Nothing escalates by itself.
4. Repeat for 100, 250, 500, 1000 (use the same messages-per-chat rate, e.g. 50, for a clean
   comparison). Stop climbing at the first WARNING or FAIL and run that level again to confirm.
5. Read the *Capacity summary*. After a server upgrade run the same levels again.

## Audit and logs

Every step is recorded in `load_test_audit` (append-only): configured, confirmation sent,
confirmed / cancelled / expired, started, stop requested, completed, aborted, cancelled, blocked,
failed, exported, thresholds saved, each with the admin id and time. Logs (`bot.loadtest`) carry
the test id, state transitions, worker start/stop, safety results and the final verdict; never the
bot token, database password, session secret, Telegram login data or message text.

## Files

`migrations/011_load_testing.sql` · `bot/loadtest/` (config, safety, evaluate, metrics, report,
runner, worker) · `bot/db/repo_loadtest.py` · `bot/services/loadtest.py` ·
`bot/handlers/loadtest.py` · `web/app/routes/loadtest.py` · `web/app/templates/loadtest*.html` ·
`tests/loadtest/`, `tests/web/test_loadtest_admin.py`, `tests/handlers/test_loadtest_flow.py`.

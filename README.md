# Dating & Random Chat Bot — v2

Telegram bot (18+ only) for profiles, anonymous random chat and match-making.
Python 3.12 · aiogram 3 · PostgreSQL 16 · Docker Compose.

> **Status: P0 (infrastructure) and P1 (profile + 18+ safety + random chat) are done.** Browse/match,
> admin, languages/limits/premium, web admin and payments come in later phases.

| Phase | Scope | Status |
|---|---|---|
| P0 | Infrastructure: Docker, PostgreSQL, migrations, config, backups, tests | **done** |
| P1 | Profile + 18+ safety + Random Chat | **done** |
| P2 | Browse / Like / Match + Report / Block | not started |
| P3 | Admin + Moderation + Audit | not started |
| P4 | Languages + Limits + Premium entitlements | not started |
| P5 | Web admin (FastAPI + Caddy HTTPS) | not started |
| P6 | Payments + subscription lifecycle | not started |

## What P1 does

* **18+ gate**: `/start` shows an adults-only declaration. "I am under 18", or typing an age below
  18, hard-blocks the account permanently (tombstone kept, profile/username wiped). Enforced in the
  service layer, in SQL (a profile needs a confirmed declaration) and by DB constraints
  (`profiles.age >= 18`, `active` requires `age_confirmed_at`).
* **Profile**: name, age, gender, who you want to meet, city, bio. Links, @usernames, e-mails, phone
  numbers, sexual-solicitation and "I'm 15"-style text are rejected. `/profile`, `/editprofile`.
* **Random chat**: `/chat`, `/next`, `/stop`. Anonymous: the partner only sees age, gender and city.
  **Text and stickers only**: photos, files, voice, forwards, links, usernames and phone numbers
  are blocked (Telegram @usernames are allowed); text is re-sent as plain text (no HTML, no hidden links, content-protected).
* **Safety**: `/report` (reason buttons) and `/block`, also available after a chat ends. Reports
  are limited to 5/hour and 20/day per user; 3 distinct reporters in 24 h put the reported user on a
  24 h chat cooldown (admin review comes in P3). Per-user rate limits on messages, buttons and
  searches; repeated flooding earns a 5 minute mute.
* **`/deleteme`**: removes profile, username, age confirmation, own block list, queue entry and
  chat sessions without reports. Kept for safety: the user id + status, reports / restrictions /
  blocks *against* the user (report contexts are nulled), and a tombstone for banned/underage ids.

### Privacy model

* **Chat messages are never stored in the database.** Only session metadata (who, when) is.
* For moderation the bot keeps the **last 10 text messages per chat in RAM only**, each for at most
  10 minutes (lost on restart). Only when someone files a report is that snapshot copied into
  `reports.context` (with `from: reporter|reported`, never user ids) and it is nulled after 30 days
  by the maintenance job.
* No username, user id or contact is ever revealed to a chat partner; button payloads contain
  chat-session ids only.
* Contact filter: phone numbers are detected by normalisation + context, not a bare digit count
  (separators, +91/international, Unicode and spelled-out digits; a plain 8-9 digit number needs a
  cue such as "call"/"whatsapp"/"number"). A number split over consecutive messages is caught using
  a tiny per-chat-and-sender memory: at most the last 15 **digits** (no text) for 2 minutes / 3
  messages, max 2000 entries, RAM only, never in reports.
* Limitations: rate limits and the report buffer live in this process (single bot instance);
  text filters can't stop every obfuscation (e.g. spelling out a domain), which is what reports are
  for; the onboarding conversation state is in memory, so a restart makes users send `/start` again.
* Logs never contain message text. Keep `LOG_LEVEL` at `INFO` or above in production.

## Run on a server (Docker)

Needs Docker with the Compose plugin. The bot uses Telegram long polling: **no domain, no open
port** is needed.

```bash
cp .env.example .env
# edit .env: BOT_TOKEN (from @BotFather), ADMIN_IDS, POSTGRES_PASSWORD
#   generate a password:  openssl rand -base64 24   (any characters are fine)
docker compose up -d --build
docker compose logs -f bot        # look for: "applied 1 migration(s)" and "starting long polling"
docker compose ps                 # bot should become "healthy" (see Health below)
```

Then message your bot: `/start` → "Bot is running", `/ping` → "pong — database: ok".

Update later: `git pull && docker compose up -d --build` (new migrations apply automatically on start).

### Configuration (`.env`)

| Variable | Required | Meaning |
|---|---|---|
| `BOT_TOKEN` | yes | Token from @BotFather |
| `ADMIN_IDS` | no | Comma-separated Telegram user ids (admin features from P3) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | yes (compose) | Database credentials; the bot builds a percent-encoded connection URL from them (`POSTGRES_HOST`, default `db`, and `POSTGRES_PORT`, default `5432`, are optional) |
| `LOG_LEVEL` | no | `DEBUG`/`INFO`/`WARNING`/`ERROR` (default `INFO`) |
| `BACKUP_DIR`, `BACKUP_KEEP_DAYS` | no | Used by `scripts/backup.sh` (defaults `./backups`, `14`) |
| `DATABASE_URL` | only without compose | Full URL, overrides `POSTGRES_*` parts (percent-encode special characters in the password). Compose ignores it. |
| `HEARTBEAT_FILE` | no | Liveness marker path (default `/tmp/bot.heartbeat`) |

`.env` is gitignored. Never commit it. The database container publishes **no ports**; only the
bot container (same internal network) can reach it.

## Health

The bot calls Telegram's `getMe` every 15 s and touches a heartbeat file only when that succeeds;
the Docker healthcheck turns **unhealthy** if the file is older than 60 s (Telegram unreachable or
token revoked). It does not detect a *second* poller using the same token (Telegram answers 409 and
the bot silently receives nothing): make sure no other process, e.g. the old prototype, runs with
this `BOT_TOKEN`. On start the bot also deletes any webhook set for the token.

## Backups

```bash
./scripts/backup.sh                                   # → backups/<db>_<UTC timestamp>.sql.gz
./scripts/restore.sh backups/<file>.sql.gz --yes      # DESTRUCTIVE: replaces the public schema
```

* `backup.sh` verifies the dump before keeping it and deletes this database's backups older than
  `BACKUP_KEEP_DAYS`. Files are `chmod 600` in a `700` directory.
* Daily cron example: `15 3 * * * cd /path/to/repo && ./scripts/backup.sh >> backups/backup.log 2>&1`
* Both scripts talk to PostgreSQL with plain `docker exec` (no TTY), and use `docker compose` /
  `docker-compose` only to find the container. They deliberately avoid `docker compose exec -T`,
  which some hosts reject (`unknown shorthand flag: 'T' in -T`). If compose is unusable on a host,
  set `DB_CONTAINER=<postgres container name>` (and `BOT_CONTAINER=<bot container name>` for
  `restore.sh`) in `.env`; `docker ps` shows the names.
* **Copy backups off the server** (another machine/bucket); a backup on the same disk does not
  survive losing the disk.
* `restore.sh` stops the bot, replays the dump in a single transaction (all-or-nothing) and starts
  the bot again. Test a restore on a staging copy before you need it.

## Migrations

Plain SQL files in `migrations/`, named `NNN_description.sql` (`001`, `002`, … contiguous), applied
automatically at start-up by `bot/db/migrate.py` (or manually: `python -m bot.db.migrate`).

* Forward-only. **Never edit or delete a migration that was already applied**: its sha256 is stored
  in `schema_migrations` and start-up refuses to continue on a mismatch. Add a new file instead.
* Each file runs in one transaction (failure = nothing applied). Don't use statements that can't
  run in a transaction (e.g. `CREATE INDEX CONCURRENTLY`), and don't write your own
  `BEGIN`/`COMMIT`/`ROLLBACK` (the runner rejects such files).
* Migrations run on a dedicated connection with no query timeout, so slow ones are not cancelled.
* A PostgreSQL advisory lock makes concurrent start-ups safe.
* The bot refuses to start if the database contains migrations that are missing on disk (old
  code against a newer schema).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# Tests use a real PostgreSQL. Any server where the user may CREATE DATABASE works, e.g.:
docker run -d --name pgtest -e POSTGRES_PASSWORD=pg -p 55432:5432 postgres:16
export TEST_DATABASE_URL=postgresql://postgres:pg@127.0.0.1:55432/postgres

pytest                 # DB tests are skipped when TEST_DATABASE_URL is unset
ruff check . && ruff format --check .
```

Each DB test creates and drops its own database, so migrations always run from scratch.

Run the bot outside Docker: export `BOT_TOKEN` and `DATABASE_URL`, then `python -m bot.main`.

## Layout

```
bot/
  main.py          entry point: config → DB pool → migrations → long polling
  config.py        environment parsing/validation (secrets excluded from repr)
  health.py        heartbeat file + `python -m bot.health` (Docker HEALTHCHECK)
  db/pool.py       asyncpg pool with start-up retry
  db/migrate.py    migration runner
  handlers/        thin Telegram handlers (start, profile, account, safety, chat, fallback)
  middlewares/     throttle -> user context (private chats only) -> account-status gate
  services/        business logic (onboarding, matchmaking, chat relay, moderation, account)
  db/repo_*.py     all SQL (users, profiles, chat sessions, safety)
migrations/        numbered SQL migrations
scripts/           backup.sh, restore.sh
tests/             pytest suite (config, health, main, migrations, handlers)
Dockerfile  docker-compose.yml  .env.example
```

Conventions for upcoming phases: handlers stay thin (Telegram ↔ service), business logic goes in
`bot/services/`, SQL only in `bot/db/`.

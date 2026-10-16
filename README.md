# Dating & Random Chat Bot — v2

Telegram bot (18+ only) for profiles, anonymous random chat and match-making.
Python 3.12 · aiogram 3 · PostgreSQL 16 · Docker Compose.

> **Status: P0–P6 are done, plus subscription tiers.** Payment verification is MANUAL (no gateway, no webhook).

| Phase | Scope | Status |
|---|---|---|
| P0 | Infrastructure: Docker, PostgreSQL, migrations, config, backups, tests | **done** |
| P1 | Profile + 18+ safety + Random Chat | **done** |
| P2 | Browse / Like / Match + Report / Block | **done** |
| P3 | In-bot admin: users, reports, statistics, audit | **done** |
| P4 | Languages + entitlements + usage limits (no payments) | **done** |
| P5 | Web admin (FastAPI behind aaPanel Apache HTTPS) | **done** |
| P6 | Manual UPI payments + premium subscriptions | **done** |

## Subscription tiers: Free / Premium / Pro / VIP

A tier is nothing but **entitlement data** (`bot/tiers.py`, stored in `plan_entitlements`); no code
asks "is this user Premium?", it asks the `EntitlementService` for a feature. Merging is
most-generous-wins on top of the Free plan, so a paid plan only lists what it raises.

| | Free | Premium | Pro | VIP |
|---|---|---|---|---|
| random chat starts / day (`random_chat`) | 100 | 200 | 500 | unlimited* |
| profiles browsed / day (`browse`) | 100 | 200 | 500 | unlimited* |
| likes / day (`swipe`) | 50 | 200 | 500 | unlimited* |
| partner age / gender (`view_partner_age`, `_gender`) | 🔒 | ✅ | ✅ | ✅ |
| partner city (`view_partner_city`) | ✅ | ✅ | ✅ | ✅ |
| `advanced_filters`, `see_who_liked` (`/likes`), `profile_boost` | – | – | ✅ | ✅ |
| `match_priority` / `plan_badge` level | 0 / – | 1 ⭐ | 2 💎 | 3 👑 |

\* "unlimited" = no DAILY limit. The per-second abuse guards (`chat_start`, `browse_view`, `like`,
the message throttle) stay on for every tier. Everything above is data: change a number on the
plan, not the code. Partner **city** is allowed for Free because the brief says it follows the
owner's privacy only; "chat starts" means pressing 💬 Random Chat / ➡️ Next (a press that does
not start a search is given back).

**Setting it up:** web admin → *Plans*: every plan has a **Tier**; creating or changing the tier
applies that tier's entitlements to the plan (an edit that keeps the tier never touches hand-tuned
values; *custom (keep)* leaves them alone). Migration 008 turned P6-style paid plans (exactly the
three plain "unlimited" flags) into Premium and left customised plans and edited Free limits as
they were.

**Daily usage** lives in `usage_counters` (UTC day), taken atomically by `consume()`; Telegram
buttons are never trusted. Chat starts, shown profiles and likes are charged before the work and
refunded when nothing happened; a limit message tells the user the limit and the reset time (00:00
UTC) and points to `/premium`.

**Priority:** `match_priority` orders the random-chat queue (highest tier first, then longest
waiting; stored in `chat_queue.priority`) and breaks ties in Browse (people who liked you still
come first). `profile_boost` and `advanced_filters` are capability flags only: there is no filter
UI or boost schedule yet.

**Partner card** (random-chat start, mutual match, `/matches`, `/likes`): the same fields for
everyone; each of age / gender / city is
`shown` = the owner allows it AND the viewer is entitled, `locked` = "🔒 Premium" (owner allows,
viewer not entitled) or `hidden` = "🙈 Hidden by the user" (the owner switched it off; **no plan
overrides that**). The decision is made inside the SQL query (`db/repo_cards.py`): a protected
value is never selected for a viewer who may not see it, so it cannot reach text, callbacks, logs
or errors. The random-chat card stays anonymous (no name / bio); the match card adds name,
looking-for and about, plus the partner's badge. One upgrade hint ("🔓 Subscribe to unlock…
/premium") per 6 hours, not per card. Browse cards (before liking) are unchanged.
Owners use **/privacy** (age / gender / city toggles, `profiles.show_*`, default shown).

**Buying again** (`decide_purchase` in `services/payments.py`; one transaction, subscription rows
locked): the same plan, or another plan of the same tier, extends the running subscription from
its *current expiry* (not from today), pushing anything queued behind it back by the same days; a
higher tier takes effect immediately and carries the remaining time over; a lower tier is queued
and starts when the current time ends; an expired subscription is simply replaced by a fresh one.
A subscription that has not started yet grants nothing. At most one subscription is effective at
any moment. The user is told the end date (or the start date of a queued plan).

## Premium: manual UPI payments (P6)

> **Payment screenshots are evidence only. Premium is activated only after manual admin approval.**

No payment gateway and no webhook: the user pays by UPI outside the bot, an admin checks the money
arrived and approves in the web panel. Money flow and premium are connected by one place only:
`PaymentService.approve`.

**User flow (Telegram, English / हिन्दी):** ⭐ Premium (menu button or `/premium`) → pick a plan →
the bot sends a QR (a standard `upi://pay` link with the UPI ID, payee, **exact amount** and the
payment reference) plus the instructions → the user pays in their UPI app → *I have paid* → sends a
screenshot → optionally the UTR / transaction id → **pending**. The bot tells them when an admin
approved (premium end date) or rejected (with the reason) the payment. The QR only pre-fills the
payment; it proves nothing.

**Admin flow (web panel):**
* **Payment settings**: UPI ID, payee name, instructions, on/off. Payments ship switched OFF.
* **Plans**: code, name, days, price in rupees, active. Plans are the P4 `plans` table (3 new
  columns); a paid plan starts with the same feature flags as Free. Changing a price affects new
  orders only; an order keeps the amount the user was shown.
* **Payments**: pending first, newest first, filter by status. The detail page shows the expected
  amount, plan, UTR, who it was shown to pay, the screenshot, and warnings if the same UTR or an
  identical screenshot appears on another payment. **Approve** or **Reject** (a reason of at least
  3 characters is required and is shown to the user). No profile, chat or contact data is shown.
* Dashboard counts payments awaiting review; every decision and setting change is in the audit log
  (`payment_approve`, `payment_reject`, `payment_config`, `plan_save`; UPI ids are not logged).

**Approval** runs in one transaction with the payment row locked: still `pending`? account active?
plan still on? screenshot present? then it creates the P4 `subscriptions` row (or extends the
user's live one by the plan's days when renewing), marks the payment approved with reviewer and
time, and writes the audit row. Two admins clicking at once produce one subscription. If anything
fails nothing is kept. Expiry needs no job: `EntitlementService` ignores a subscription once its
`current_period_end` has passed (the user falls back to the Free plan); housekeeping only marks old
rows `expired`.

**State model** (enforced in the service and by a database trigger): `created` → `pending` →
`approved` | `rejected`, and `created` → `cancelled`. Approved / rejected / cancelled rows can't
change; the order details (user, plan, amount, pay-to) can never change. A user has at most one
open order (unique index); orders nobody submitted are cancelled after 24 h. A UTR can back only one
approved payment (unique index), and a UTR already pending / approved elsewhere is refused at
submission; a rejected one may be offered again.

**Screenshot security:** the type comes from the *content* (JPEG / PNG / WebP magic), never the file
name, extension or declared MIME. Each file is parsed: JPEG segments + end marker, PNG chunks with
CRCs + a bounded zlib check (decompression bombs refused) + nothing after `IEND`, WebP RIFF/VP8
headers; truncated, tampered, polyglot (data appended after the image), oversized (> 3 MB) or
absurd-dimension files are refused. Accepted bytes are stored **in PostgreSQL** (`payment_evidence`),
not on disk: no path, no user-supplied file name, nothing executable, included in backups. The
admin page serves them with the validated type, `nosniff`, `no-store` and `default-src 'none'`.
Screenshots of finished payments are deleted after 90 days. OCR / auto-approval do not exist.

**Abuse limits:** rate limits on opening orders, uploads and submissions; one open order per
user; amount validated against the plan; only active adults can order.

**Set up UPI:** web admin → *Payment settings* → enter the UPI ID and payee name → tick *Payments
active* → save; then *Plans* → create e.g. `premium_30`, 30 days, price 99. Test with a small
amount before announcing.

**Tests for P6:** `pytest tests/services/test_payments.py tests/services/test_image_check.py
tests/handlers/test_premium_flow.py tests/web/test_payments_web.py tests/db/test_p6_migration.py`
(needs `TEST_DATABASE_URL`). Migration `007_manual_payments.sql` is additive.

## Web admin (P5)

A FastAPI panel at `https://$ADMIN_DOMAIN/` (e.g. `admin-bot.nxt-server.cloud`) for the people in
`ADMIN_IDS`: dashboard, users (search by Telegram id or profile id, paged), reports (resolve /
dismiss / ban), statistics and the read-only audit log. It reuses the bot's `AdminService`, so ban /
unban / report rules (admins can't be banned, underage and deleted accounts stay as they are,
chat ended, queue left, audit row written) are exactly the in-bot ones; `/admin` in Telegram is
unchanged. **No chat transcripts, no live chats, no phone / e-mail / username are shown**; the only
chat text is the report snapshot the bot already keeps (until its expiry, then it is gone).

* **Login** = Telegram Login Widget. The payload is verified per Telegram's algorithm (HMAC with
  `SHA256(BOT_TOKEN)`), must be ≤ 5 min old, can be used once, and the id must be in `ADMIN_IDS`.
  There are no passwords. Sessions are server-side (in this process; a restart logs everyone out),
  cookie `__Host-admin_session`: random id, `HttpOnly`, `Secure`, `SameSite=Lax`; 8 h maximum,
  1 h idle; `ADMIN_IDS` is re-checked on every request.
* **CSRF**: every POST needs a token = HMAC(`ADMIN_SESSION_SECRET`, session id) (constant-time
  compare) and, if the browser sends Origin / Referer, they must be this site. GET never mutates.
* **Host**: only `ADMIN_DOMAIN` is served (anything else gets `400`); `X-Forwarded-Host` is ignored.
* **Login rate limit** (in-process, no Redis): 10 attempts / 5 min per client address and 60 / 5 min
  for the whole site; every attempt counts. It can only refuse, never let anyone in.
* Headers: strict CSP (no scripts except the Telegram widget on `/login`), `nosniff`, `Referrer-Policy: same-origin` (not `no-referrer`: that makes browsers send `Origin: null` on form posts, which the CSRF check rejects),
  frame denial, HSTS, `no-store`. `Cross-Origin-Opener-Policy` is `same-origin` everywhere except
  `/login`, which uses `same-origin-allow-popups`: Telegram's login popup reports back through
  `window.opener`, and `same-origin` severs it (the login then silently never completes). Jinja autoescaping everywhere; no inline JS/CSS.
* Deployment: `web` is one container in the **`web` compose profile** (the bot deploys exactly as
  before until you opt in). It publishes `127.0.0.1:8000` only (loopback, not reachable from the
  internet). HTTPS is terminated by the server's **existing Apache / aaPanel**, which proxies the
  admin domain to that port; this stack never uses ports 80/443 and has no Caddy. The database is
  never published. The bot owns migrations; `web` only waits for them. No schema change.
* Client IP for the login limit: the last `X-Forwarded-For` entry (added by Apache), believed only
  when the direct peer is private / loopback.
* One `web` instance only: sessions and the login throttle live in its memory (scaling out would
  need a shared session store).

Setup (once):
1. DNS `A` record for `ADMIN_DOMAIN` → this server. BotFather `/setdomain` for the bot = `ADMIN_DOMAIN`.
2. `.env`: `ADMIN_DOMAIN`, `ADMIN_BOT_USERNAME`, `ADMIN_SESSION_SECRET` (`openssl rand -hex 32`);
   check `ss -ltnp | grep ':8000'` is empty (else set `ADMIN_WEB_PORT`).
3. aaPanel → Website → Add site (only this domain, no PHP) → SSL → Let's Encrypt → Force HTTPS.
4. Add `deploy/apache-admin-bot.conf.example` to that site's Apache config (only that vhost).
5. `docker compose -p dating-bot-v2 --profile web up -d --build web`

## What P4 does

* **🌐 Language** (`/language` or the menu button): English / हिन्दी, saved per user in
  `users.language` (default English, never guessed from Telegram). **Everything the bot says to a
  user is translated**: every message, button and keyboard (onboarding, random chat, browse /
  matches, profile, safety notices, `/deleteme`, account states, rate-limit notices). Strings live
  in `texts/en.py` (English, the baseline: constants + the generated `MESSAGES` catalog) and
  `texts/hi.py`; code calls `tr(key, lang)` from `texts/i18n.py`, and `lang` is injected into
  every handler from `users.language`. A missing Hindi key falls back to English; a test fails if
  the catalogs ever drift (keys, `{placeholders}`, HTML tags, `/commands`). Users are always
  addressed in THEIR language (e.g. a Hindi and an English user in one chat each see their own).
  Callback data never contains text, so it is identical in every language. The operator-only
  admin panel stays English (only "not authorized" is translated); `/ping` is a diagnostic.
  To add a language: add `texts/<code>.py`, register it in `i18n.py`, and extend the `users.language` CHECK
  constraint with a new migration.
* **Entitlements** (`services/entitlements.py`, the single place to ask "may this user do X?"):
  `get(uid, feature)`, `can_use(uid, feature)`, `consume(uid, feature)`. Resolution = default
  `free` plan + live subscriptions + live grants, most generous wins; a feature nobody grants is
  disabled. `consume` counts usage per UTC day in ONE atomic SQL statement, so concurrent calls
  can never exceed a daily limit. The seeded free plan enables random chat / browse / swipe with
  no limits (today's behaviour); nothing in the bot enforces a limit yet.
* Tables: `plans`, `plan_entitlements`, `subscriptions`, `entitlement_grants`, `usage_counters`
  (counts only; purged after ~35 days and on `/deleteme`). **No payments**: nothing writes
  subscriptions yet.

## What P3 does (in-bot admin)

Set `ADMIN_IDS` (comma-separated Telegram ids) in `.env`; the admin must have sent `/start` once.

* `/admin` opens a button dashboard: **👥 Users** (send a Telegram id, or `/user <id>`), **🚨 Reports**
  (open reports, newest first; open one to see reporter / reported ids, reason and the retained
  context), **📊 Statistics** (aggregate counts only).
* Actions: ban (with confirmation; ends the chat, leaves the queue, closes the user's open
  reports) and unban (back to `active`, or `onboarding` without an adult profile); resolve /
  dismiss a report; ban the reported user straight from a report. Underage-blocked and deleted
  accounts cannot be changed; admins cannot be banned in the bot.
* Authorization is checked against `ADMIN_IDS` on every command, text input and button press (and
  again in the service); non-admins only see "Not authorized". Admin actions are rate limited.
* Every action is written to the append-only `admin_actions` audit table (ids + action, no chat text).

## What P2 does

* **🔎 Browse** (`/browse`): one profile card at a time (name, age, flag + city/region, bio) with
  ❤️ Like, ❌ Pass, 🚨 Report, 🚫 Block. Hidden: yourself, non-adult/banned/deleted users, anyone
  blocked in either direction, and profiles you already swiped. `looking_for` is not a filter.
* **Mutual like = match** (`matches`, one canonical row per pair, created under a per-pair
  advisory lock). Both users get "It's a Match!"; **no username or contact is ever revealed**.
* **💞 My Matches** (`/matches`): latest 10 visible matches (deleted / banned / blocked ones are hidden).
* Button payloads use an opaque `profiles.public_id`, never Telegram ids. Profile reports reuse
  the `reports` table, rate limits and the 3-reporter cooldown. `/deleteme` removes swipes and matches.
* Random Chat is unchanged (gender-independent).

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
web/               FastAPI admin panel (P5): app/ (auth, csrf, routes, templates), health.py
deploy/             Apache vhost snippet for the web admin (reference)
migrations/        numbered SQL migrations
scripts/           backup.sh, restore.sh
tests/             pytest suite (config, health, main, migrations, handlers)
Dockerfile  docker-compose.yml  .env.example  requirements-web.txt
```

Conventions for upcoming phases: handlers stay thin (Telegram ↔ service), business logic goes in
`bot/services/`, SQL only in `bot/db/`.

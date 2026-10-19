# Dating & Random Chat Bot — v2

Telegram bot (18+ only) for profiles, anonymous random chat and match-making.
Python 3.12 · aiogram 3 · PostgreSQL 16 · Docker Compose.

> **Status: P0–P6 are done, plus subscription tiers, advanced filters, profile boost, the tier/limit
> editor and the advisory screenshot check.** Payment verification is MANUAL (no gateway, no webhook).

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

### Tiers & limits (admin editor)

Web admin → **Tiers & limits** edits the entitlements of **Free, Premium, Pro and VIP** without
touching code: daily chat starts / browse / likes (a number from 0 to 1,000,000, or *Unlimited*),
the capabilities (`view_partner_age`, `_gender`, `_city`, `advanced_filters`, `see_who_liked`,
`profile_boost`), `match_priority` (0–100) and the badge (none / ⭐ / 💎 / 👑).

* **Tier-wide vs plan-specific.** These settings belong to the tier: saving *Premium* writes them to
  every plan of that tier (so *Premium 30 days* and *Premium 60 days* never disagree) and remembers
  them in `tier_settings` for plans created later. Name, price, length and on/off are plan-specific
  and are never touched here; neither are subscriptions or payments. Custom plans (tier "custom")
  are left alone. *Reset* returns a tier to the built-in table in `bot/tiers.py`.
* **Free** is edited in place (the `free` plan). Everyone has Free and a paid plan adds to it with
  the more generous value winning, so a paid tier cannot be lower than Free (the page warns when you
  set it so).
* Validation: negative, fractional, non-ASCII, empty or over-range numbers and unknown modes are
  refused and nothing is saved. Every save and reset is written to the audit log with the values.
  Admin-only, CSRF-protected, rate-limited.
* The owner's privacy stands against any viewer of the same or a lower tier: no setting here can show
  an age, gender or city that its owner hid to them. A viewer of a strictly higher tier is the one
  exception (see *Tier outranks privacy* below).

**Default plans:** at start-up the bot makes sure plans `premium`, `pro` and `vip` exist
(`PaymentService.ensure_default_plans`, also the **Create / complete default tiers** button on the
*Plans* page). Missing ones are created **switched off with no price and no length** (prices are
yours to set); a plan that already exists is never overwritten: with the same tier only missing
feature keys are added, a customised plan is left alone; subscriptions and payments are never
touched. The *Plans* page lists Premium / Pro / VIP with their plans.

**Setting it up:** web admin → *Plans*: every plan has a **Tier**; creating or changing the tier
applies that tier's entitlements to the plan (an edit that keeps the tier never touches hand-tuned
values; *custom (keep)* leaves them alone). Migration 008 turned P6-style paid plans (exactly the
three plain "unlimited" flags) into Premium and left customised plans and edited Free limits as
they were.

**Daily usage** lives in `usage_counters` (UTC day), taken atomically by `consume()`; Telegram
buttons are never trusted. Chat starts, shown profiles and likes are charged before the work and
refunded when nothing happened; a limit message tells the user the limit and the reset time (00:00
UTC) and points to `/premium`.

**Priority:** Browse order = people who already liked you, then an **active profile boost**, then
the subscription level (`match_priority`), then the most recently active. The random-chat queue
(stored in `chat_queue.priority`) is ordered: anyone who has waited more than 2 minutes first (so
nobody is starved), then an active boost, then the tier level, then the longest waiting.
Ranking only re-orders people who are already eligible: it never lifts a block, a ban, an owner's
privacy switch or a limit.

### Advanced filters (Pro / VIP: `advanced_filters`)

`/filters`, or the 🎯 button under a browse card. Filters: **age range** (presets 18–24 … 60+),
**gender**, **city** (typed, case-insensitive exact match) and **looking for**. They are stored per
user in `browse_filters` and apply to Browse only (random chat stays gender-independent).

* The entitlement is asked of `EntitlementService` on every press and on every browse; a stale or
  forged button proves nothing, and if the plan ends the stored filters are kept but ignored.
* A filter on age / gender / city also needs the matching `view_partner_*` entitlement (Pro and VIP
  have it): filtering by a value the viewer may not see would reveal it by elimination.
* **Privacy:** a profile whose owner hides age / gender / city never matches a filter on that
  field unless the viewer's tier is strictly above the owner's (*Tier outranks privacy*), so a filter
  (or an empty result) can never be used to find out a value the viewer may not see. The card
  that comes back is built exactly as without filters. Blocked, banned, deleted and underage users
  never appear. No filter value is logged or put in a callback (callbacks carry a preset index).
* *Looking for* = people whose own "looking for" is that value **or "everyone"**; choosing
  "everyone" shows only people who said everyone.
* Free / Premium get a localized "available with Pro or VIP" message.

### Profile boost (Pro / VIP: `profile_boost`)

`/boost` → **🚀 Activate Boost**. Exact rules:

* lasts **60 minutes** (`BOOST_MINUTES`); the next one can start **12 hours after the previous one
  started** (`COOLDOWN_HOURS`, so at most two a day). Pressing during a boost shows when it
  expires; pressing during the cooldown shows when it ends.
* state lives in PostgreSQL (`profile_boosts`, one row per user). Activation is one
  `INSERT … ON CONFLICT DO UPDATE … WHERE cooldown_until <= now()` statement, so twelve simultaneous
  presses create exactly one boost. A boost counts while `ends_at > now()` in PostgreSQL: it
  expires by itself, no job or cron is involved.
* a boost that was started keeps running to its end even if the subscription ends meanwhile
  (at most 60 minutes); after that the entitlement is needed again.
* it moves the profile up in Browse and the chat queue (see *Priority*); everyone else is still
  shown, because each viewer sees each profile once and the boost is short and rate-limited.

**Partner card** (random-chat start, mutual match, `/matches`, `/likes`): the same fields for
everyone; each of age / gender / city is
`shown` = the owner allows it AND the viewer is entitled, `locked` = "🔒 Premium" (owner allows,
viewer not entitled) or `hidden` = "🙈 Hidden by the user" (the owner switched it off and the viewer's
tier is not above the owner's). The decision is made inside the SQL query (`db/repo_cards.py`): a protected
value is never selected for a viewer who may not see it, so it cannot reach text, callbacks, logs
or errors. The random-chat card stays anonymous (no name / bio); the match card adds name,
looking-for and about, plus the partner's badge. One upgrade hint ("🔓 Subscribe to unlock…
/premium") per 6 hours, not per card. Browse cards (before liking) follow the same tier rule.
Owners use **/privacy** (age / gender / city toggles, `profiles.show_*`, default shown).

**Tier outranks privacy** (age / gender / city only, on every surface: Browse card and filters,
`/matches`, `/likes`, random-chat card, reveal button, follow notification). Tiers are ordered by the
`plan_badge` level (Free 0 < Premium 1 < Pro 2 < VIP 3; the viewer's and the owner's are both read from
the entitlements, subscriptions and grants included, no plan name is hard-coded). If the viewer's level
is **strictly higher** than the owner's, the viewer sees the field even if the owner switched it off;
the same or a lower tier gets exactly the owner's choice (Free/Premium/Pro/VIP owner vs the same tier:
hidden; Free owner vs Pro or VIP: shown; Pro owner vs VIP: shown; VIP owner vs anyone: hidden). The
viewer's `view_partner_*` entitlement still applies on top. It never reveals a username, a user id or a
contact: those are not part of any card query.

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
payment reference) plus the instructions → the user pays in their UPI app → **sends the payment
screenshot straight to the chat as a photo** (no "I have paid" button) → **pending**. The bot tells
them when an admin approved (premium end date) or rejected (with the reason) the payment. The QR
only pre-fills the payment; it proves nothing.

**Direct screenshot upload (how it behaves):**
* A photo (or an image file) from an active user who has an **open order** is that order's
  evidence. The order is found on the server from the Telegram user id; no payment id travels in
  any button or message, so nobody can attach a picture to someone else's order.
* In ONE transaction (order row locked): validate the image → store it → order `created` →
  `pending`. The advisory OCR then runs (see below). The admins get a notification.
* **No open order** (never started, cancelled, expired after 24 h, already decided): nothing is
  created; the user is told, in their language, to choose a plan with ⭐ Premium first.
* **A second screenshot** for an order that is already submitted is refused with a clear message
  (the first stays; two photos sent at the same instant produce one submission).
* **UTR is optional**: put it in the photo caption (`UTR: 4123 4567 8901` works), or send it as a
  message within 10 minutes after the photo. A UTR already used by another payment is refused; a
  caption that is not a UTR is ignored (the screenshot still counts).
* Photos sent during a random chat are never payment evidence. The old *I have paid* button is
  gone; stale copies of it only repeat the "send the screenshot" instruction.
* Telegram photos arrive as JPEG; image *files* are accepted when they claim `image/*`, and the
  bytes are validated either way (JPEG / PNG / WebP structure, size, bombs, trailing data).

**Admin flow (web panel):**
* **Payment settings**: UPI ID, payee name, instructions, on/off. Payments ship switched OFF.
* **Plans**: code, tier, name, days, price in rupees, active. Plans are the P4 `plans` table; the
  page shows which settings are plan-specific (name, price, length, on/off) and which come from the
  tier. Changing a price affects new orders only; an order keeps the amount the user was shown.
* **Tiers & limits**: see below.
* **Payments**: pending first, newest first, filter by status; the list shows order, user, plan,
  amount, status, created time, whether a screenshot is stored, the automatic-check verdict and the
  review state. The detail page has four separate parts: **1 order data** (expected amount, plan,
  duration, reference, created), **2 submitted by the user** (UTR, screenshot, warnings if the same
  UTR or an identical screenshot appears on another payment), **3 automatic screenshot check**
  (read values next to the expected ones, verdict, warnings; labelled *advisory only*) and **4 admin
  decision** (approve / reject with a reason; afterwards who decided and when). **Approve** or **Reject** (a reason of at least
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
Screenshots of finished payments are deleted after 90 days. Nothing is ever approved
automatically.

**Screenshot check (advisory, P8).** When a screenshot is stored the bot reads it with
**Tesseract OCR** (English, offline, no network, no ML model; installed in the bot image, piped
through stdin with a 20 s timeout, 2 at a time, images over 16 megapixels are not read) and looks
for: the **amount**, the **payee** UPI ID (or the payee name), the **UTR / reference**, the printed
**date and time** (India time) and **success / failed / pending** wording. It compares them with the
order and stores a verdict:

| verdict | meaning |
|---|---|
| `VALIDATION_OK` | amount and payee match, success wording or a reference is present, date plausible |
| `VALIDATION_WARNING` | something could not be confirmed (amount / payee unreadable, similar-looking UPI ID, pending, date older than the order or in the future) |
| `VALIDATION_MISMATCH` | a different amount, a different payee UPI ID, or a failed / declined payment |
| `VALIDATION_UNREADABLE` | (almost) no text, no payment information, or OCR failed (missing, timed out, too large) |

The result is **advice for the admin**: the payment page shows the verdict, the extracted
fields (with the order's values beside them), the UTR the user typed versus the one on the picture,
and plain-language warnings; the payments list has a "Screenshot check" column. The admin still
verifies the money in the bank / UPI app and decides. A mismatch does not block approval and an OK
does not approve. The paying user is told nothing about the analysis.

**What is kept:** only the extracted fields and finding codes (`payment_evidence_analysis`); the OCR
text exists only in memory during the analysis and is never stored or logged (logs carry the
payment id and the verdict only). The row is deleted with the screenshot (90 days after the
decision) and replaced when the user sends a new picture. OCR trouble never blocks an order. It is
heuristic: a ₹ sign read as another symbol is tolerated, a heavily cropped or non-English screen
will simply be "unreadable".

**Abuse limits:** rate limits on opening orders, uploads and submissions; one open order per
user; amount validated against the plan; only active adults can order.

**Set up UPI:** web admin → *Payment settings* → enter the UPI ID and payee name → tick *Payments
active* → save; then *Plans* → create e.g. `premium_30`, 30 days, price 99. Test with a small
amount before announcing.

**Tests for P6:** `pytest tests/services/test_payments.py tests/services/test_image_check.py
tests/handlers/test_premium_flow.py tests/web/test_payments_web.py tests/db/test_p6_migration.py`
(needs `TEST_DATABASE_URL`). Migration `007_manual_payments.sql` is additive.

## Subscription management for admins (S2 / S3, migration 020)

On a user's page (and on a payment's page) an admin can correct subscriptions without touching the
database. Every action is a two-step page: a GET preview that changes nothing, then a POST that
needs the admin session, the CSRF token, our own origin, a ticked confirmation and a written reason
(3 to 300 characters). Each one writes an audit entry (admin, action, subscription, payment, reason)
in the same transaction, tells the user in Telegram, and never deletes a row or changes a payment.

| Action | What it does |
| --- | --- |
| Cancel | Marks a running or queued subscription `canceled` (S2). |
| Revoke payment | Cancels the subscription a payment created, when that payment is the only one behind it (S2). Refused when the subscription was extended or an admin changed its time. |
| Take time off | Removes whole days from a running or queued subscription. Whatever is queued behind it moves earlier by the same days, so there is never a gap or an overlap. At least an hour must remain from now (otherwise cancel it); ended subscriptions are refused and never revived. |
| Remove an extension | Takes exactly one approved payment's days off the subscription it extended (`payments.duration_days`, provable). The payment that created the subscription is refused as ambiguous (its share may include converted upgrade time), and a payment can be removed only once (unique index from migration 020). |
| Grant a plan | Gives a plan for 1 to 365 days with no payment (`provider = admin_grant`, no `payments` row, no revenue). Uses the same extend / upgrade (value-based carry-over) / queue policy as a purchase. |

Queued example: VIP ends Dec 17 and Premium is queued Dec 17 to Jan 16. Taking 10 days off the VIP
makes it end Dec 7 and moves the Premium to Dec 7 to Jan 6.

Every form carries the end date (or, for a grant, a fingerprint of the user's subscriptions) the
admin was shown. If it changed meanwhile, or the form is submitted twice, or two admins act at the
same moment, only the first change applies. The account and its subscriptions are row-locked in the
same order as an approval, so an admin change and an approval can not interleave.

## Follow / Friends wording

In the bot the words are **Follow** (was Like) and **Friends** (was Match: two people who follow each
other); `/friends` and `/followers` are the commands, `/matches` and `/likes` still work, and the old
"💞 My Matches" menu text of chats opened before the change still opens the list. Only the words changed:
the tables (`swipes`, `matches`), the button data (`lk:`, `ps:`, `mm`, `mc:`), the Pro "see who follows you"
feature (key `see_who_liked`) and every rule are as before. A profile shows `👥 Followers: N` (only when
N > 0; the number of active users who follow it, never who).

**"Someone followed you" push:** after a follow that is not mutual the followed person gets one message
(rate limited; none while they are in a chat; none for blocked or banned people). **Free:** a count only
("people waiting for you: N") and a pointer to `/followers` / `/premium`, never who. **Pro / VIP** (the
`see_who_liked` entitlement): the follower's card (their plan's fields, the follower's privacy) with
**➕ Follow back**, which is the normal Follow button, so every existing check applies. A failing push
never breaks the follower's browsing.

## Abusive-language filter

`bot/services/abuse_words.py` blocks a chat message, and rejects profile name / city / bio text, that
contains a word from a reviewed list (English, Hinglish and Devanagari). **Whole words only**, so
"class", "assistant", "chutney" or "Scunthorpe" pass; ambiguous words ("chod", "kutta", "mc", "bc")
are not on the list on purpose. Case, repeated letters ("fuuuck"), digits / symbols used as letters
("sh1t", "$hit"), spread-out letters ("f u c k") and zero-width characters are undone first. A blocked
chat message is not delivered, buffered or stored; the sender gets a short throttled notice and the chat
goes on. No automatic ban or mute: reporting works as before. A missed insult is reportable; to add a word,
extend the tuples at the top of the module (tests show how).

## Queue & funnel page (migration 014)

Admin web, **Queue & funnel** (read-only, last 7 or 30 days, UTC with an IST column): how many random-chat
searches found a partner (at once / after waiting, with the average wait), timed out or were stopped, the
busiest hours, per-day totals, and a funnel of new real users (started -> confirmed 18+ -> finished the
profile -> had a chat -> followed someone, counted from the existing tables). The counters live in
`queue_stats`, per UTC day and hour, **numbers only, no user id**; writing them can never break matching
(a failure is logged and dropped). They start counting when this version runs. This is the data to look at
before deciding about anything that fills the queue (such as the on-hold AI companions).

## My plan and the "plan ends soon" reminder (migration 013)

* **`/myplan`** (any active user, also during a chat): the plan they are on, its end date and
  **days left** (rounded up, never 0 while it still runs), a queued renewal ("Next: ... starts on"),
  today's use against the limits (chats, browse, likes) and the benefits they actually get (their
  plan merged over Free and any admin grant). Free users see "Plan: Free" and a link to `/premium`.
  It reads only the caller's own rows.
* **Reminder:** the maintenance job (every 10 minutes, and at start-up) messages a user **once** when
  the end of their *last* live subscription is 3 days or less away, in their language, with the plan
  name, the date and the days left. A queued renewal counts, so someone who already renewed is not
  nagged. The row in `expiry_reminders` is per user **and end date**, so after a renewal the next end
  date is reminded again. Banned users and plans that are switched off are skipped. Right after a
  deploy, users already inside the 3-day window get their reminder at the first run.

## Support tickets (migration 012)

_Migrations since the first release, all additive: `012_support_tickets`, `013_expiry_reminders`, `014_queue_stats`, `015_auto_approve`, `016_chat_activity`, `017_sub_audit_actions`, `018_audit_sub_columns`, `019_support_live_reply`, `020_sub_shorten`._

Users write to support from the bot; admins answer in the web panel. Nothing is sent to a
third party.

* **User (Telegram, English / हिन्दी):** 🆘 Support (menu button or `/support`) → pick a topic
  (payment, account, chat, report, other) → send **one message** (up to 2000 characters) → ticket
  `#N`. `/tickets` lists the latest 10; tap one to read the thread, **reply** or **close** it.
  Support is unavailable during a chat (the same rule as Premium). At most **3 unfinished
  tickets** per user (enforced by a database trigger as well as by the service); a ticket holds at
  most 40 messages. Opening is rate limited.
* **Admin (web, Support in the menu):** list by status (waiting for support / answered / closed),
  read the thread (shown escaped, as untrusted text), **reply** (up to 800 characters, so a Hindi
  reply fits the 8 KB form limit) or **close**. The user gets the answer in Telegram in their own
  language. POSTs need the admin session, the CSRF token and our own origin like every other
  admin action. The admins in `ADMIN_IDS` also get a short Telegram alert for a new ticket and for
  a follow-up (ticket number and topic only, never the text).
* **Audit:** `ticket_reply` and `ticket_close` go to the append-only audit log with the ticket
  number only. A user's text lives **only** in `support_messages.ticket_text`.
* **Privacy:** `/deleteme` (and the underage block) deletes the user's tickets and messages. This is
  the second deliberate place user-written text is stored (after `reports.context`): it is a message
  *to support*, never chat content; chat text still never reaches the database.

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
| `DATABASE_URL` | only without compose | Full URL, overrides `POSTGRES_*` parts (percent-encode special characters in the password). Under compose leave it unset (the URL is built from `POSTGRES_*` with host `db`); if you do set it, use host `db`. |
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
  survive losing the disk. The step-by-step runbook (cron, off-server copy, "did it run?" check,
  restore drill) is [`docs/BACKUP_RUNBOOK.md`](docs/BACKUP_RUNBOOK.md); the repository installs
  nothing by itself.
* `restore.sh` stops the bot, replays the dump in a single transaction (all-or-nothing) and starts
  the bot again. Test a restore on a staging copy before you need it.

## Server capacity / load testing (testing environment only)

Admin → **Server capacity** measures how many concurrent active chats the server handles, using a
synthetic workload through the real chat relay path (no real Telegram messages) and a separate
worker. It refuses to run unless the environment declares and proves itself a test environment, and
a test starts only after a one-time confirmation button in Telegram. **Never run load testing against
production.** Full guide: [docs/LOAD_TESTING.md](docs/LOAD_TESTING.md), settings:
[.env.loadtest.example](.env.loadtest.example).

## Known limitations

* The screenshot check is a heuristic OCR (English): cropped, blurred, very small or non-English
  screens come back "could not be read", and a forged picture can pass. It only advises; the admin
  must verify the money.
* Boost length (60 min) and cooldown (12 h) are constants in `bot/services/boost.py`, not an admin
  setting. A started boost runs to its end even if the subscription ends meanwhile.
* Advanced filters apply to Browse only (not random chat, `/likes` or matches).
* Ranking priority comes from subscriptions only, not from admin grants. Browse cards (before a like)
  show the age to every viewer unless the owner hid it.
* Rate limiters (slow-down, the upgrade hint, boost presses) are in memory and reset on restart; the
  real limits (daily usage, one boost at a time, one open order) are in PostgreSQL.
* Web sessions are in memory (a restart logs the admin out); one web worker by design.
* The Docker image, Apache/aaPanel and Telegram itself cannot be exercised in the test sandbox: the
  tests run the real services against PostgreSQL with a faked Telegram, plus a real HTTP smoke run
  of the web app.

## Releases, deploy and rollback

Production runs an exact **tag + full commit SHA** (`v2.0.0-rc1` … `v2.0.0`, `v2.0.1`, `v2.1.0`), never
a branch head. `scripts/release.sh` does preflight, backup, build, migrate, restart (bot then web),
health verification, automatic application rollback on failure, `mark-good`, `rollback` to a recorded
known-good release and keeps a ledger; the admin Dashboard shows the running release. Migrations
follow the N-1 compatibility policy (`release/migration_compat.json`), so the previous release can run on
the newer schema; the database is never rolled back by the tooling. Full procedure, the one-time
bootstrap release, downtime expectations and failure handling:
[`docs/RELEASE_RUNBOOK.md`](docs/RELEASE_RUNBOOK.md). Release tags must be **annotated**; they are
created by the manual GitHub Actions workflow *Create release tag* (validates, never overwrites; see the
runbook), not in the GitHub web UI (that makes lightweight tags).

## Deployment notes (for later)

```bash
git pull origin claude/dating-bot-v2
./scripts/backup.sh
docker compose -p dating-bot-v2 --profile web up -d --build bot web
```

Migrations (up to `013_expiry_reminders`) apply by themselves when the bot starts; the web service
waits for them. The bot image installs `tesseract-ocr` (English) for the screenshot check: the first
build downloads it; without it everything works and screenshots are just "could not be read". After
deploying: *Tiers & limits* (review the numbers), *Plans* (price, days and on/off of the Premium /
Pro / VIP plans), then a test purchase with a small amount.

### Restarts and off-peak deploys

A restart (every deploy is one) is short, but know what it does:

* **Survives** (in PostgreSQL): accounts, profiles, matches, the random-chat queue, running chats
  (both sides keep chatting), payments, tickets, plans. Updates that arrive while the bot is down are
  kept by Telegram (up to 24 hours) and delivered when it is back.
* **Is lost** (in memory): half-finished conversations (the sign-up form, the filters "city" prompt, a
  support message being typed, a UTR being sent), rate-limit counters, and the last-10-messages
  buffer that backs a report (a report filed in the minutes right after a restart has no chat
  excerpt). A user whose sign-up was interrupted taps a menu button or sends `/start` and carries on;
  a menu button also ends any other half-finished conversation.
* **Deploy at the quietest hour** (late night in your users' time zone, once you know when your
  users are online), never in the evening peak.
* Order: `./scripts/backup.sh` first, then the `docker compose ... up -d --build` line above. Watch
  `docker compose logs -f bot` until it prints `starting long polling`; then send `/ping` to the bot.
* `QUEUE_TIMEOUT_MINUTES` (default 30, range 1-240) is how long a random-chat search waits before it
  is cancelled with a "try again later / try Browse" message. Set it in `.env`; it applies at the next start.
* `CHAT_INACTIVITY_TIMEOUT_MINUTES` (default 10, range 1-1440): an active chat (random or Friends) with
  no message for that long is closed and both people are told. It is **not** a maximum chat length:
  every delivered message restarts the timer. A short loop checks once a minute (migration 016 stores
  only the time of the last message, never text).
* **Automatic payment approval** (Admin → Payment settings, **OFF by default**, migration 015): when an
  admin switches it on, a payment is approved by itself only if the screenshot analysis is fully OK
  (amount, payee UPI id and UTR all read and matching, UTR and image never used before, user and plan
  active). Anything uncertain stays for a human. Audit action `payment_auto_approve`; switch it off at
  any time. The screenshot check is evidence, not proof of settlement: keep checking the bank statement.
* A user with a higher paid tier running cannot buy a lower-tier plan (same tier = renewal, higher =
  upgrade). The order comes from the plans' tier data, not their names.
* If something raises inside a handler, the user sees a short "something went wrong" line and the
  admins in `ADMIN_IDS` get **one** Telegram alert per kind of error per 10 minutes (at most 6 per
  hour): the kind of error and where, never message text. The traceback is in the log only. The same
  throttled alert tells the admins when Telegram keeps refusing or failing to deliver messages
  (rate limits are retried twice first). Each admin must have pressed *Start* in the bot once to
  receive alerts.

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
  code against a newer schema), except during an application rollback by the release script
  (`ALLOW_NEWER_SCHEMA=1`, only for the next migration numbers; nothing is applied).
* **Compatibility (N-1).** Release N may only apply migrations that release N-1 code can run against:
  add (tables, nullable / defaulted columns, widened CHECKs, indexes) first; drop, rename, narrow or
  rewrite only in a later release. Every migration needs an entry (`expand` / `conditional` / `contract`
  + a note) in `release/migration_compat.json`; a test enforces it. See
  [`docs/RELEASE_RUNBOOK.md`](docs/RELEASE_RUNBOOK.md).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# Tests use a real PostgreSQL. Any server where the user may CREATE DATABASE works, e.g.:
docker run -d --name pgtest -e POSTGRES_PASSWORD=pg -p 55432:5432 postgres:16
export TEST_DATABASE_URL=postgresql://postgres:pg@127.0.0.1:55432/postgres

pytest                 # DB tests are skipped when TEST_DATABASE_URL is unset
# the OCR tests need the tesseract binary (apt install tesseract-ocr) and skip without it
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
scripts/           backup.sh, restore.sh, release.sh (deploy / rollback)
release/           migration_compat.json (rollback compatibility of every migration)
tests/             pytest suite (config, health, main, migrations, handlers)
Dockerfile  docker-compose.yml  .env.example  requirements-web.txt
```

Conventions for upcoming phases: handlers stay thin (Telegram ↔ service), business logic goes in
`bot/services/`, SQL only in `bot/db/`.

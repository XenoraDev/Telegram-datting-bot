# Phase B plan: labelled AI companions

**Status: PLAN ONLY. Nothing here is implemented.** Phase B starts only after (1) Phase A
(commit `c1bfc3c`) is deployed and smoke-tested, and (2) the owner approves this plan and answers
the decisions in section 10. Each step below ends with tests, ruff, a commit, a push and a STOP for
approval, exactly like Phase A. No deploy, no VPS change, no edits to migrations 001-012.

## 1. Why, and the one rule that shapes everything

Early on there are few users, so a random-chat search often waits and the user leaves. The
fix is an **AI companion** a user can talk to while nobody real is online.

**The rule: an AI is always clearly an AI.** Every place a user meets one says so ("🤖 AI
companion"): the offer, the card, the first message, `/terms`, `/about`. We do not build accounts
that look like real people. Reasons: it would deceive users of a bot that also sells Premium,
and it breaks Telegram's rules and consumer-protection law. This plan therefore also never
gates an AI's profile fields behind Premium (selling the "age" of a fictional character would
be monetising a fake).

## 2. What the user sees

1. They search, nobody is online. After `wait_seconds` (admin setting, default 60) the bot asks:
   "No one is online right now. Chat with a 🤖 AI companion while you wait? Messages are sent to
   [provider] to write the replies. [Yes] [Keep waiting]". Choosing *Keep waiting* changes nothing.
2. First time only, *Yes* also records consent (see 6). Declined users are never auto-paired.
3. The card says "🤖 AI companion" and shows the persona's made-up name, age (18+), city, bio.
   The first message repeats "I'm an AI".
4. `/next` or `/stop` leaves; `/next` searches for a real person again. If a real partner
   arrives while they chat with an AI, nothing is cut off: they `/next` when they want.
5. Reporting or blocking an AI is accepted politely and creates nothing to ban.

## 3. What the admin controls (new web page "AI companions")

* On/off switch (default **off**), plus an emergency "stop all AI chats now" button.
* **How many AI IDs run** = `max_slots` (0-N). Each concurrent AI chat uses one slot.
* `wait_seconds` before the offer, daily message cap per user, global daily message cap.
* Personas (name, age >= 18, gender, city, bio, language en/hi, system prompt, enabled).
* Provider, model, timeout; a "send a test message" button; today's numbers (AI chats, messages,
  provider errors, estimated cost). Actions go to the audit log (`ai_settings_save`, ...).

## 4. Data model (migration 014, additive only; 013 is the plan-expiry reminder table)

* `ai_settings` (single row), `ai_personas`, `ai_slots`, `ai_consent(user_id, accepted_at)`.
* Each slot is a row in `users` with a **reserved id range above 2^52** (Telegram ids never reach
  it; for example `5_000_000_000_000_000 + n`), status `active`, no profile. No profile means it is
  invisible to Browse, Matches, Likes, Boost and filters for free. The AI card is built from the
  persona instead (`PartnerCard.is_ai`).
* **Do not use negative ids.** The load-test safety check (`bot/db/repo_loadtest.py`) treats
  any negative user id not registered as a load-test user as "foreign" and blocks the test.
* The existing `one_active_session_per_user` rule keeps working because a slot is a real user row.
* The `admin_actions` CHECK is widened again (drop + add, same pattern as 007 and 012).

## 5. How the pieces connect (existing code to reuse)

* **Matching:** a small loop (same style as `maintenance_loop`) finds queue rows older than
  `wait_seconds`, **offers** AI (never forces), and on *Yes* pairs the user with a free slot inside
  the matchmaking advisory lock (`repo_chat.find_or_queue` / `lock_matchmaking`).
* **Relay:** in `handlers/chat.py::relay`, when `res.partner_id` is an AI slot, do not call
  `bot.send_message`; start a background reply task: typing action, ask the provider, send the
  reply with `telegram_send.deliver` (retries and admin alerts are already there).
* **Memory:** the last ~10 turns per chat live in RAM only (like `ContextBuffer`) and are dropped
  when the chat ends or the bot restarts. **Conversation text is never stored in the database.**
* **Skip AI ids** in `_announce_end`, `notify_partner_left`, restrictions and bans
  (`moderation`), admin statistics ("active users" excludes the range), `/deleteme` paths.
* **Alerts:** provider failures use the throttled `AdminAlerter` from Phase A.

## 6. Privacy and consent

* The bot sends the user's messages to a third party. So: one-time explicit consent, a plain
  sentence in `/terms` and `/about`, and the provider named. No message text in logs (the
  existing rule), none in the database.
* The AI is told never to ask for or accept contact details, money, or personal data, and its
  output goes through the existing contact/link filter (`content_rules`) plus new rules:
  no sexual content, no minors, no claiming to be human, no payment requests, max length.
* The AI never pushes Premium or payments.

## 7. Cost and safety controls

* Per-user daily cap, global daily cap, a global requests-per-minute limiter sized to the
  provider's limit, a hard timeout, a short reply length limit.
* **Circuit breaker:** after 5 provider failures in a row, AI is switched off for 5 minutes, a
  polite "busy" line is sent, and the admins are alerted (throttled). Users in an AI chat get a
  scripted "I have to go" and the chat ends cleanly.
* Estimated cost = messages per day x tokens per message x the provider's price. Fill the
  price in from the provider's own page on the day you decide; do not rely on a blog.

## 8. Provider options (the "free API" question)

All figures come from secondary articles written in 2026 that disagree with each other; **check
each provider's official pricing and terms page before choosing.** The code will hide the
provider behind one interface (`bot/services/llm.py`, OpenAI-compatible HTTP via the existing
`aiohttp`; keys in `.env`, never in code).

| Option | What reports say | Fit for private chats |
| --- | --- | --- |
| Google Gemini free tier | Flash / Flash-Lite only; limits were cut in late 2025; Pro models left the free tier in April 2026 | **Poor**: Google's terms say free-tier prompts and replies may be used to improve its products and read by human reviewers; paid tier excludes this; linking billing moves all usage to paid terms. Possible EEA/UK limits: unverified |
| Groq free tier | around 30 requests/min with per-model daily caps (changed mid-2026) | OK for a pilot; check its data terms |
| OpenRouter `:free` models | a small subset of models, roughly tens of requests/day on a free account | Too small for real traffic; each model has its own provider terms |
| Cloudflare Workers AI | about 10,000 "neurons" a day free | Small pilot only; check data terms |
| Anthropic | no free API tier (per one comparison) | Paid, small cost |
| Self-hosted small model | free per message, but needs RAM/GPU the VPS may not have | Best privacy; weak quality on CPU |

**Recommendation:** pilot with ONE slot on a free tier **that does not train on your data** and
with the consent text above, and only if its terms are acceptable; otherwise pay for a small model
(the cost per chat is small compared to a lost user). Do not use a free tier that trains on
intimate chats. This is the owner's decision (section 10).

## 9. Steps (each: code, tests, ruff, commit, push, STOP for approval)

* **B1** migration 014 + `repo_ai` + `AiSettingsService` + audit actions + migration tests.
* **B2** `llm.py` provider interface + a fake provider for tests + output filter + circuit breaker.
* **B3** admin web page (settings, personas, slots, test button, today's numbers) + auth/CSRF/audit tests.
* **B4** offer + consent + pairing + card + `/next` / `/stop` / report handling.
* **B5** relay branch, RAM memory, caps, typing indicator, stats exclusions, `/deleteme`.
* **B6** `/terms` + `/about` + README update, then the full suite on a fresh clone.

Tests never call a real provider. They cover: AI is always labelled; consent required; no
auto-pairing without *Yes*; no profile or browse exposure; no text in the database; caps and
circuit breaker; admin on/off and slot count; reserved ids never collide with real or load-test
ids; ban/report/restriction skip AI ids; Hindi and English texts.

**Rollout:** ship with AI **off**, enable 1 slot, watch alerts and numbers for a few days,
raise the slot count only after that. The kill switch must be tested before launch.

## 10. Decisions needed from the owner before B1

1. Provider and privacy stance (section 8): paid small model, a free tier that does not train, or self-hosted?
2. Consent: ask once with an explicit *Yes* (recommended) or on every offer?
3. Personas: how many, names, ages (18+), cities, languages, tone and topics to avoid.
4. Should an AI chat use the user's daily random-chat quota? (Recommended: no, only its own message cap.)
5. Defaults: `wait_seconds` (60?), per-user cap (e.g. 50 messages/day?), starting `max_slots` (1?).
6. Wording of the consent and of the `/terms` line about AI (to be approved before it ships).

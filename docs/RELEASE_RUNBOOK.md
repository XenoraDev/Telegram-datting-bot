# Release, deploy and rollback runbook

For the person who operates the server. **Nothing here runs by itself**: the repository installs no
cron entry and no web endpoint can deploy or roll back. Everything below is done over SSH, by an
authorised operator, with `scripts/release.sh`.

## 1. The idea in six lines

* A release is an **annotated git tag** (`v2.0.0-rc1`, … `v2.0.0`, `v2.0.1`, `v2.1.0`) **and the full
  40-character commit** it points to. Production deploys that pair, never the head of a branch.
* The tag and the SHA must agree, the commit must be on `origin/claude/dating-bot-v2`, and the
  working tree must be clean. A moved tag or a typo cannot deploy something else.
* Images are built **before** anything running is touched, and tagged `dating-bot/bot:<tag>` /
  `dating-bot/web:<tag>`; they carry the tag, the commit and the build time (admin → Dashboard →
  Release, `docker inspect`, the first log line).
* Migrations run first, in a one-off container, while the old bot keeps serving. They are
  **backward compatible** (section 3), so the previous release can keep running on the new schema.
* **Rollback = start the previous KNOWN-GOOD application release again.** The database is never
  rolled back by this tooling. If the database cannot take the older code, the rollback **refuses**.
* Every step is written to a ledger (`releases/history.jsonl`, 600, not in git).

Never, in any of this: `docker compose down`, `down -v`, deleting a volume, `docker volume/system
prune`, `docker rmi`, `restore.sh`, editing `.env`, touching Apache, or touching the load-test stack.
`release.sh` refuses to run any of them and the db service is never recreated.

## 2. Versions

| Tag | Meaning |
| --- | --- |
| `v2.0.0-rc0` | the **bootstrap** release (section 5): today's production code + version identity + schema tolerance, **no migration** |
| `v2.0.0-rc1`, `-rc2`, … | release candidates; Phase 4 testing and the final audit run on the exact tag |
| `v2.0.0` | the launch release: the **same commit** as the last passing candidate |
| `v2.0.1` | fix, no migration |
| `v2.1.0` | feature or an additive migration |

Release candidates need `--allow-rc` to reach the server. Production identity is always
**tag + full SHA**; write both in the approval message ("deploy `v2.0.0` `<40 hex>`").

## 3. Migration policy (so that rollback stays possible)

**N-1 compatibility:** release N may only apply migrations that release N-1 code can run against.

* **Expand first.** New tables; new nullable or `DEFAULT`ed columns; widening a CHECK (as 017 / 020 /
  021 do for the audit actions); new indexes. These are `expand` in `release/migration_compat.json`.
* **Contract later.** Dropping or renaming a column or table, narrowing a CHECK, `SET NOT NULL`,
  retyping, or rewriting data are `contract`: they ship in a **later** release, only once the release
  before is no longer a rollback target. Never in the same release as the code that depends on them.
* **Renaming** a column = add the new one → write both → backfill → read the new one → (a release
  later) drop the old one.
* A migration that is compatible only while some data is in a state is `conditional` with a named,
  read-only guard (`bot/db/compat.py`, `GUARDS`). Example, migration 019: older code closes a
  ticket without clearing `support_tickets.live_until`, which 019's CHECK refuses, so a rollback
  across 019 is allowed only while no ticket has `live_until` set.
* **Every new migration needs an entry in `release/migration_compat.json`** (class + a real note);
  `tests/db/test_migration_compat.py` fails otherwise and a lint flags destructive statements that
  were labelled `expand`. Historical files are immutable (their sha256 is pinned); the manifest is
  where they are classified. 001–021 were reviewed one by one: 006 (narrows `users.language`) and 008
  (rewrites `plan_entitlements`) are `contract`, 019 is `conditional`, the rest are `expand`.
* `release.sh deploy` runs `python -m bot.db.migrate --plan` first and **refuses** a release that
  would apply `contract` or unclassified migrations (override: `--accept-no-rollback`, an informed
  decision that this release can not be rolled back).

The bot's migrator refuses to start on a schema it does not know, **unless** the release script sets
`ALLOW_NEWER_SCHEMA=1` for a rollback, and then only for exactly the next migration numbers after the
newest file it knows (nothing applied, nothing missing in between, checksums verified).

## 4. What downtime to expect (realistically)

There is **no zero-downtime claim**.

| Part | Reality |
| --- | --- |
| Telegram bot (long polling) | one process must poll (two = 409 Conflict). `up` stops the old container and starts the new one: **about 10–30 s** without a bot. Telegram keeps the updates (up to 24 h) and delivers them afterwards, so no message is lost. Lost: half-finished conversations and rate-limit counters (see README, "Restarts"). Docker only marks the new container healthy after its first health check (up to ~1 min); the script waits up to 3 min. |
| FastAPI web | one worker by design: **about 5–15 s**, Apache shows 502 meanwhile; admin sessions are in memory, so the admin signs in again. |
| PostgreSQL | not touched: same container, same volume. |

What shortens it: images are built first (the slow part runs while the old release serves),
migrations run in a one-off container beforehand, and bot and web are restarted one after the other.
Deploy at the quietest hour. Blue/green or rolling deploys are not worth their complexity here.

## 5. One-time: the bootstrap release (`v2.0.0-rc0`)

The code in production today refuses to start on a newer schema, so it can never be a rollback target
once migrations 019–021 are applied. The bootstrap release is today's code **plus** two small commits
that add version identity and schema tolerance, **with no migration**. After it, every later release
can be rolled back to it. This is done **once**, by the operator, with the owner's approval.

On a development machine (not the server). `PROD` is the commit production runs now (ask the person
who deployed it):

```bash
git fetch origin
git checkout -b release/bootstrap <PROD>
git cherry-pick 1e259e3 e71e78a        # version identity, then ALLOW_NEWER_SCHEMA + --status/--info
# run the targeted tests, then:
git tag -a v2.0.0-rc0 -m "bootstrap: version identity + schema tolerance, no migration"
git push origin release/bootstrap v2.0.0-rc0
```

On the server (at the quietest hour; nothing below deletes anything):

```bash
cd /path/to/dating-bot-v2
./scripts/backup.sh && ls -lh backups/ | tail -n 2            # a fresh, valid backup first
git fetch --tags origin && git status --short                  # clean tree
git checkout --detach v2.0.0-rc0
SHA=$(git rev-parse HEAD)
export RELEASE_TAG=v2.0.0-rc0 GIT_SHA=$SHA BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker compose --profile web build bot web                     # the running containers keep serving
docker compose --profile web up -d --no-deps --no-build bot    # then, after it is healthy:
docker compose --profile web up -d --no-deps --no-build web
docker compose ps                                              # bot and web "healthy"
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
  "$(docker compose ps -q bot)"                                # prints $SHA
```

Send `/ping` to the bot and sign in to the admin. Then record it (the script comes from the first
release candidate; checking it out only fetches the scripts, nothing is built):

```bash
git checkout --detach v2.0.0-rc1            # the candidate you are about to deploy
scripts/release.sh adopt v2.0.0-rc0 --expect-sha <SHA of v2.0.0-rc0>
```

`adopt` checks that the running containers carry exactly that tag and commit, are healthy, have their
images on this server and declare schema tolerance, and records `v2.0.0-rc0` as known-good.

## 6. Every release

```
RELEASE  ->  BACKUP  ->  VERIFY  ->  DEPLOY  ->  MIGRATE  ->  HEALTH CHECK  ->  VERIFY  ->  MARK KNOWN-GOOD
```

**Before the server (development side).** Targeted tests while building; a release-candidate tag
`vX.Y.Z-rcN` on the exact commit; Phase 4 full testing and the final audit **on that tag**; if they
pass, the final tag `vX.Y.Z` on the **same commit**. The approval message carries the tag and the
full SHA. Any new migration has its entry in `release/migration_compat.json`.

**On the server** (inside `tmux`, so a dropped SSH session cannot interrupt it):

```bash
cd /path/to/dating-bot-v2
scripts/release.sh status                       # what runs now, last known-good releases
scripts/release.sh preflight vX.Y.Z --expect-sha <40 hex>   # read-only: tag, SHA, branch, env keys, disk, db
scripts/release.sh deploy    vX.Y.Z --expect-sha <40 hex>   # asks you to type the tag
```

`deploy` does, in this order, and stops at the first failure:

1. **Preflight**: tag is annotated and equals the SHA, commit is on `origin/claude/dating-bot-v2`,
   tree clean, `.env` has the required keys (names only, values are never read out), enough disk, db
   healthy, the running release is recorded as known-good.
2. **Backup**: `scripts/backup.sh` (verified dump). The file name goes in the ledger.
3. **Checkout** the exact commit (detached).
4. **Build** bot and web. Running containers are untouched. The image must carry the SHA and declare
   schema tolerance.
5. **Plan**: pending migrations classified; `contract`/unclassified ones refuse the deploy.
6. **Migrate** in a one-off container (`run --rm --no-deps`); one transaction per file; the old bot
   keeps serving. A failure leaves nothing half-applied and nothing replaced.
7. **Restart** `bot`, then `web` (`up -d --no-deps --no-build`); the db is never recreated.
8. **Health** (up to 3 min): both containers `healthy` and running this exact commit, `/healthz` answers
   `ok`, the bot logged `starting long polling` with **no** `409 Conflict`, and the database is at the
   release's newest migration.

If step 8 fails, `deploy` makes **one** automatic application rollback (section 7). Otherwise it
prints "healthy". Then **verify by hand**: `/ping` to the bot, sign in to the admin (Dashboard →
Release shows the new tag), look at Support and Payments. Only then:

```bash
scripts/release.sh mark-good vX.Y.Z
```

A release is a rollback target only after `mark-good` (or `adopt`).

## 7. Rollback

```
ROLLBACK  ->  SELECT PREVIOUS VERSION  ->  VERIFY DB COMPATIBILITY  ->  RESTORE APP VERSION  ->  HEALTH CHECK  ->  VERIFY
```

```bash
scripts/release.sh status
scripts/release.sh rollback                  # the newest known-good release that is not running
scripts/release.sh rollback --to v2.0.0-rc1  # or one recorded known-good release, by tag
```

* **Select**: only releases recorded as known-good in the ledger are accepted. A branch, a commit id,
  a git tag that was never marked, or anything else is refused. There is no way to pass a ref.
* **Verify DB compatibility** (read-only, on the current image): every migration the target does not
  know must be `expand`, or `conditional` with its guard clear. `contract` or unclassified ones
  **refuse** the rollback with the reason. **Nothing is changed when it refuses.**
* **Restore**: the recorded images are started again (`up -d --no-deps --no-build`), with
  `ALLOW_NEWER_SCHEMA=1` only if the schema really is newer, and the checkout follows. Missing images
  need `--rebuild` (built from the **recorded** commit, never from a ref you type).
* **Health check**: the same as a deploy. If it fails, the script **stops** and says so
  (`rollback-failed`): no second automatic attempt. Look at `scripts/release.sh status` and
  `docker compose logs --tail=100 bot web`.
* **Verify** by hand (`/ping`, admin).

**If a rollback is refused** the database has moved on in a way the old code cannot follow. The
choices are a fix-forward release, or, as a **separate manual emergency decision** for data
corruption only, restoring a backup (`docs/BACKUP_RUNBOOK.md`, `restore.sh`): that loses everything
written since the backup. This tooling never does it.

## 8. When things go wrong

| Failure | What happens |
| --- | --- |
| Build fails | nothing replaced, ledger `failed-build`, checkout restored |
| Backup fails | nothing changed, `failed-backup` |
| Migration fails | rolled back by PostgreSQL, running release untouched, `failed-migrate` |
| Container does not start / unhealthy / web health fails | automatic application rollback, `failed-health` then `rolled-back` |
| Bot cannot poll (`409 Conflict`, token, Telegram down) | health fails → rollback. 409 means another process polls with this token (a dev machine? a second bot?) |
| Rollback refused by the DB gate | the new release is left running, `rollback-refused`; decide manually |
| Rollback also unhealthy | stops, `rollback-failed`, nothing else is attempted |
| SSH drops / Ctrl-C during a deploy | `interrupted` + the phase in the ledger; **nothing is rolled back automatically**. Run `status`, then decide (re-run `deploy`, or `rollback`) |
| Two release commands at once | the second refuses (lock) |

## 9. Security

* Only an SSH-authorised operator can run it; there is no web route, no webhook, no API.
* Tags match `^v[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$` and SHAs `^[0-9a-f]{40}$` before anything is
  run; nothing is `eval`ed; the rollback target is chosen from the ledger, not typed.
* `.env` is never read out, copied or overwritten (only the **names** of required keys are checked);
  build arguments are the tag, commit and time only.
* The ledger has no secrets; `operator` comes from `SUDO_USER`/`id -un`, restricted to a safe alphabet.
* The server needs only **read-only** access to the repository (a deploy key or token with read
  rights, kept in `~/.ssh` with mode 600, never in this repository). Signed tags are optional.
* Apache, aaPanel, the load-test stack and the database volume are not touched.

## 10. Command reference

```
scripts/release.sh status
scripts/release.sh history [N]
scripts/release.sh preflight <tag> --expect-sha <sha> [--allow-rc]
scripts/release.sh adopt     <tag> --expect-sha <sha> [--yes]
scripts/release.sh deploy    <tag> --expect-sha <sha> [--allow-rc] [--accept-no-rollback] [--yes]
scripts/release.sh mark-good <tag> [--yes]
scripts/release.sh rollback  [--to <tag>] [--rebuild] [--yes]
python -m bot.db.migrate --status | --info | --plan | --check-rollback N     # read-only helpers
```

`--yes` skips the typed confirmation (use it only from a reviewed procedure). Environment overrides
for unusual hosts: `RELEASE_BRANCH`, `RELEASE_REMOTE`, `HEALTH_TIMEOUT`, `MIN_FREE_MB`, and
`BOT_CONTAINER` / `WEB_CONTAINER` / `DB_CONTAINER` (as for `backup.sh`).

Ledger line (one JSON object per line): `ts`, `action` (`deploy`/`rollback`/`mark-good`/`adopt`), `tag`,
`sha`, `prev_tag`, `prev_sha`, `operator`, `result` (`started`, `healthy`, `known-good`,
`failed-backup|build|plan|migrate|health`, `rolled-back`, `rollback-refused`, `rollback-failed`,
`interrupted`), `schema` (newest migration of that release), `backup`, `note`.

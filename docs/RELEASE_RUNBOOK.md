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

## 5. One-time: the bootstrap release (`v2.0.0-rc0`): DONE

**Status (a historical fact, recorded here so nobody redoes it):** the bootstrap release is deployed.

| | |
| --- | --- |
| Tag | `v2.0.0-rc0` (annotated; on `origin`) |
| Production SHA | `5008368b7d637a74d064d51c780b62781d0d8f78` |
| Built on | `c9f922d` (the upgrade carry-over fix); production database: migrations 001–019 only |
| Contents | `c9f922d` + cherry-picked **equivalents** of the two bootstrap commits of the development branch: `1e259e3` (version identity) is `672982c` here, and `e71e78a` (migrator tolerance, `--status`, `--info`) is `5008368` |
| Migration in it | none |

`672982c` and `1e259e3` (and `5008368` and `e71e78a`) have the identical patch (same `git patch-id`),
so they are the same change under different commit ids. No commit of the bootstrap is to be applied
again. **No branch contains `5008368` yet**: it is reachable through the tag only (section 5a).

Why it exists: the code in production before it refuses to start on a newer schema, so it could never
be a rollback target once migrations 019–021 are applied. The bootstrap release is that code **plus**
version identity and schema tolerance, **with no migration**; every later release can be rolled back
to it. The procedure it followed is kept below for reference; it is not to be repeated.

On a development machine, as it was done (`PROD` = the commit production ran, `c9f922d`):

```bash
git fetch origin
git checkout -b release/bootstrap c9f922d
git cherry-pick 1e259e3 e71e78a        # version identity, then ALLOW_NEWER_SCHEMA + --status/--info
# (on that line they became 672982c and 5008368)
# run the targeted tests, then:
git tag -a v2.0.0-rc0 -m "bootstrap: version identity + schema tolerance, no migration"
git push origin v2.0.0-rc0
```

On the server (at the quietest hour; nothing below deletes anything):

```bash
cd /path/to/dating-bot-v2
./scripts/backup.sh && ls -lh backups/ | tail -n 2            # a fresh, valid backup first
git fetch --tags origin && git status --short                  # clean tree
git checkout --detach v2.0.0-rc0
SHA=$(git rev-parse HEAD)                                       # 5008368b7d637a74d064d51c780b62781d0d8f78
export RELEASE_TAG=v2.0.0-rc0 GIT_SHA=$SHA BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker compose --profile web build bot web                     # the running containers keep serving
docker compose --profile web up -d --no-deps --no-build bot    # then, after it is healthy:
docker compose --profile web up -d --no-deps --no-build web
docker compose ps                                              # bot and web "healthy"
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
  "$(docker compose ps -q bot)"                                # prints $SHA
```

**Still to do, once: record it in the ledger** (`adopt`). The bootstrap tree has no `release.sh`, so
run the script of the first release-line candidate against this checkout without moving the checkout
(`RELEASE_REPO`, section 5a), or check that tag out read-only first:

```bash
RELEASE_REPO=/path/to/dating-bot-v2 bash /path/to/release.sh \
  adopt v2.0.0-rc0 --expect-sha 5008368b7d637a74d064d51c780b62781d0d8f78
```

`adopt` checks that the running containers carry exactly that tag and commit, are healthy, have their
images `dating-bot/{bot,web}:v2.0.0-rc0` on this server and declare schema tolerance, and records
`v2.0.0-rc0` as known-good (newest migration 19). If a check fails it refuses and changes nothing.

## 5a. Two lines of history (until the S3 / Traffic integration)

There are two lines, and they are separate **on purpose**:

* **The release line**: starts at the tag `v2.0.0-rc0` (`5008368`) and carries only release-system
  work (the compatibility manifest and gate, `scripts/release.sh`, the admin release card, this
  runbook). It has migrations 001–019 and **no** migration 020 or 021. It is the branch
  `release/v2.0.0` on `origin`.
* **The development branch** `origin/claude/dating-bot-v2`: it also holds S3 (migration 020) and the
  traffic attribution (migration 021). Neither contains the other: their common ancestor is `c9f922d`.

What `release.sh` expects of the history:

* `deploy` requires the release commit to be an **ancestor of `origin/$RELEASE_BRANCH`**. The default
  is `claude/dating-bot-v2`, which does **not** contain the release line. For tags of the release line
  set `RELEASE_BRANCH` to the release-line branch (it must be pushed to `origin` first):

  ```bash
  RELEASE_BRANCH=release/v2.0.0 scripts/release.sh preflight v2.0.0-rc1 --expect-sha <40 hex> --allow-rc
  RELEASE_BRANCH=release/v2.0.0 scripts/release.sh deploy    v2.0.0-rc1 --expect-sha <40 hex> --allow-rc
  ```

  (`RELEASE_REMOTE` names the remote, default `origin`.) **Without `RELEASE_BRANCH` the script checks
  the development branch and refuses a release-line commit.** The tag must still be annotated and
  equal the expected SHA, and the working tree clean.
* `adopt`, `rollback`, `rollback --rebuild` and `mark-good` do **not** check ancestry. That the
  bootstrap commit `5008368` is on no branch, and that it is a cherry-pick, is therefore no problem
  for them.
* `RELEASE_REPO` is the checkout the script operates on (default: the checkout the script file lives
  in). It lets you run a copy of `release.sh` from elsewhere against the production checkout, e.g. for
  `adopt` while that checkout is still on the bootstrap tag. The script always copies itself to a
  temporary file first, so a checkout it performs cannot rewrite it while it runs.

The release-line commits are exactly `918da99`, `995c790`, `7818e4c`, `857f41b`, `74c18c8` and
`7579e44` (the later documentation fixes included), applied on top of `5008368`. They contain
**no migration file**. The tag `v2.0.0-rc0` changes nothing for later tags: `v2.0.0-rc1`, `-rc2`, …,
`v2.0.0` are ordinary annotated tags on the line they are made on (never move or reuse `v2.0.0-rc0`).

## 5b. Later, separately: the S3 / Traffic release (migrations 020 and 021)

Not part of the release-system release, and **not deployed by it**. The two migrations come from
exactly two commits of the development branch: `6cba5e6` (020, `sub_shorten`) and `3b99213` (021,
traffic attribution), with the code that uses them (S3: `9faa849`, `547a3db`, `af67d42`; traffic:
`0d36a86`, `6f25d54`, `554f4bf`, `0d5ece6`, `c0b3872`). They must come in together with their code,
never one commit at a time, and only by an explicit decision.

When that release is intended: merge the development branch into the release line (a test merge was
clean and lossless: the two bootstrap commits are patch-identical, so nothing is applied twice), then
tag a release candidate on the merge, run Phase 4 testing and the final audit on that exact tag, and
deploy it with the same procedure as any release. `deploy` will list `020` and `021` in its migration
plan; both are `expand` in `release/migration_compat.json` (the manifest already classifies them,
although their files are not on the release line), so the plan allows it and a later rollback to the
release before is permitted by the database gate (with `ALLOW_NEWER_SCHEMA=1`, which the script sets
only then). Afterwards merge the release line back into the development branch so there is one
lineage again.

## 6. Every release

```
RELEASE  ->  BACKUP  ->  VERIFY  ->  DEPLOY  ->  MIGRATE  ->  HEALTH CHECK  ->  VERIFY  ->  MARK KNOWN-GOOD
```

**Before the server (development side).** Targeted tests while building; a release-candidate tag
`vX.Y.Z-rcN` on the exact commit; Phase 4 full testing and the final audit **on that tag**; if they
pass, the final tag `vX.Y.Z` on the **same commit**. The approval message carries the tag and the
full SHA. Any new migration has its entry in `release/migration_compat.json`.

**Creating the release tag (the official procedure).** Tags are created by the GitHub Actions
workflow **Create release tag** (`.github/workflows/create-release-tag.yml`, logic in
`scripts/create_release_tag.sh`), not by hand, not in the GitHub web UI and not by the Claude Code
session (it can push branches but is refused tags: HTTP 403). The workflow uses the repository's own
`GITHUB_TOKEN`, with `contents: write` on its one job and nothing else.

1. The release commit is on `release/v2.0.0` and its tests and audit passed. Note the full 40-character SHA.
2. GitHub → **Actions → Create release tag → Run workflow**, **branch `release/v2.0.0`** (the job
   refuses to run from any other branch), `tag` = `v2.0.0-rc2`, `expected_sha` = the 40 hex, and
   **`dry_run` left ticked** first. A dry run validates against the real repository and writes nothing.
3. Run it again with `dry_run` unticked. The log ends with `verified: <tag> is an annotated tag object
   (...) whose peeled commit is <sha>`.
4. On the server or any checkout, confirm independently, then deploy as in the rest of this section:

```bash
git fetch --tags origin
scripts/release.sh check-tag vX.Y.Z-rcN --expect-sha <40 hex>   # suitable: yes
```

What it checks **before it writes anything** (any failure stops with `NOTHING WAS WRITTEN`): `tag` is
`vX.Y.Z` or `vX.Y.Z-rcN`; `expected_sha` is 40 lowercase hex and a real commit; that commit is part of
`origin/release/v2.0.0` (no commit outside the release branch can be tagged); it contains the files
`release.sh` requires; the tag exists neither in the checkout nor on `origin`. Then exactly two
creations, in this order: the annotated tag **object** (`POST git/tags`), then `refs/tags/<tag>` pointing
at it (`POST git/refs`). GitHub refuses to create a ref that already exists, so nothing is ever
overwritten. Afterwards it reads `origin` back: the ref exists, the object type is `tag`, and it peels to
`expected_sha`.

It never deletes, moves, force-updates or replaces a tag, and it has no repair step: if the read-back
fails (or the ref was refused because somebody created the tag meanwhile) it says so and stops. A person
then investigates; the unreferenced tag object GitHub may have created is harmless. A wrong tag that
exists is **not** fixed by running the workflow again (it refuses, the tag exists); use the replace
procedure below, by a person who may push tags, only when the lightweight tag points at the right commit.

**The tag must be annotated, and it is checked, not assumed.** Do not create it any other way. If you
must, on a development machine: `git tag -a vX.Y.Z-rcN -m "..." <40 hex>` and
`git push origin refs/tags/vX.Y.Z-rcN`. **A tag made in the GitHub web UI** ("Releases → Choose a tag → Create new tag", or a release drafted
from it) **is lightweight**, and `release.sh` refuses it. Never loosen that check. Before approving,
run the read-only diagnosis anywhere the repository is checked out (it needs no docker, takes no lock,
fetches nothing and changes nothing):

```bash
git fetch --tags origin     # only brings tags that are new to this checkout
scripts/release.sh check-tag vX.Y.Z-rcN --expect-sha <40 hex>
```

It prints the tag state (`missing` / `lightweight` / `annotated`), the tag object, the object it
points at, the **peeled commit**, the expected commit, what `origin` holds, and `suitable: yes|no`
with the reason; exit status 1 if it is not suitable. Deployable means: annotated, pointing directly at
a commit, peeled commit equal to the expected SHA, and (when `origin` can be read) `origin` agrees.

**If the tag is lightweight** (`deploy`, `preflight` and `adopt` stop on it before any write, and
`check-tag` prints this procedure with your tag and SHA filled in). The script never deletes, moves
or force-updates a tag. This procedure is printed **only when the lightweight tag already points at
the expected SHA**; if it points at a different commit the output is `STOP. The tag currently points
to <current>, not <expected>. Do not re-tag or move it. Investigate the mismatch first.` and no
retag command is shown. When it does point at the expected SHA, a person who may push tags replaces
it **in one step, never delete first**:

```bash
git fetch origin
git rev-parse <40 hex>^{commit}                      # prints exactly <40 hex>
git tag -a -f -m "vX.Y.Z-rcN" vX.Y.Z-rcN <40 hex>    # -f replaces only YOUR LOCAL tag
git push --force-with-lease=refs/tags/vX.Y.Z-rcN:<40 hex> origin refs/tags/vX.Y.Z-rcN
git ls-remote origin 'refs/tags/vX.Y.Z-rcN*'         # expect a ...^{} line equal to <40 hex>
```

(`--force-with-lease` names the value `origin` is expected to hold, the commit for a lightweight tag,
so the push fails rather than overwrite a tag somebody changed meanwhile.) If `origin` refuses it
(branch/tag protection or a GitHub ruleset on `v*`), an owner has to allow it or push it; do not
work around the refusal. Then, on the server: `git fetch --force origin
refs/tags/vX.Y.Z-rcN:refs/tags/vX.Y.Z-rcN`. A plain `git fetch --tags` never updates a tag that
already exists in the checkout, so a stale lightweight copy would keep being refused.

**On the server** (inside `tmux`, so a dropped SSH session cannot interrupt it):

```bash
cd /path/to/dating-bot-v2
# for tags of the release line: RELEASE_BRANCH=<release-line branch> in front of each command (section 5a)
scripts/release.sh status                       # what runs now, last known-good releases
scripts/release.sh check-tag vX.Y.Z --expect-sha <40 hex>   # read-only: annotated? peeled commit? origin agrees?
scripts/release.sh preflight vX.Y.Z --expect-sha <40 hex>   # read-only: tag, SHA, branch, env keys, disk, db
scripts/release.sh deploy    vX.Y.Z --expect-sha <40 hex>   # asks you to type the tag
```

`deploy` does, in this order, and stops at the first failure:

1. **Preflight**: tag is annotated and its peeled commit equals the SHA (a lightweight tag stops
   here, see above), commit is on `origin/$RELEASE_BRANCH` (`release/v2.0.0` for this line, section
   5a), tree clean, `.env` has the required keys (names only, values are never read out), enough
   disk, db healthy, **the running release is already recorded as known-good**. If it is not (the
   first deploy after the bootstrap), `deploy` refuses: run `release.sh adopt <running tag>
   --expect-sha <its sha>` once **before** the first deploy (section 5), or pass
   `--accept-no-rollback` knowing there will be nothing to roll back to.
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
for unusual hosts: `RELEASE_BRANCH` (the branch the release commit must be on; see 5a), `RELEASE_REMOTE`,
`RELEASE_REPO` (the checkout to operate on), `HEALTH_TIMEOUT`, `MIN_FREE_MB`, and
`BOT_CONTAINER` / `WEB_CONTAINER` / `DB_CONTAINER` (as for `backup.sh`).

Ledger line (one JSON object per line): `ts`, `action` (`deploy`/`rollback`/`mark-good`/`adopt`), `tag`,
`sha`, `prev_tag`, `prev_sha`, `operator`, `result` (`started`, `healthy`, `known-good`,
`failed-backup|build|plan|migrate|health`, `rolled-back`, `rollback-refused`, `rollback-failed`,
`interrupted`), `schema` (newest migration of that release), `backup`, `note`.

#!/usr/bin/env bash
# Release, deploy, rollback and history for the Docker Compose stack. Run it ON THE SERVER, by an
# operator, over SSH. It is not reachable from the web and accepts no arbitrary refs or commands.
#
#   scripts/release.sh status
#   scripts/release.sh history [N]
#   scripts/release.sh check-tag <tag> --expect-sha <40 hex>      # read-only tag diagnosis, no docker
#   scripts/release.sh preflight <tag> --expect-sha <40 hex>
#   scripts/release.sh adopt     <tag> --expect-sha <40 hex>      # record what is ALREADY running
#   scripts/release.sh deploy    <tag> --expect-sha <40 hex> [--allow-rc] [--accept-no-rollback] [--yes]
#   scripts/release.sh mark-good <tag> [--yes]
#   scripts/release.sh rollback  [--to <tag>] [--rebuild] [--yes]
#
# A release is an annotated git tag (vX.Y.Z or vX.Y.Z-rcN) AND the full 40-character commit it
# points to: both must match, so a moved tag or a typo can never deploy something else. Never the
# head of a branch. Rollback restores the application of a previously recorded KNOWN-GOOD release;
# the database is never rolled back by this script (see docs/RELEASE_RUNBOOK.md).
#
# Never run here: docker compose down / down -v, volume or image removal or prune, anything that
# recreates the db service, restore.sh. The .env file is only checked for the NAMES of the keys.
set -euo pipefail

# A git checkout (deploy / rollback) rewrites this very file: run from a private copy.
if [[ -z "${RELEASE_SH_COPY:-}" ]]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    copy="$(mktemp "${TMPDIR:-/tmp}/release.XXXXXX")"
    cp "${BASH_SOURCE[0]}" "$copy"
    RELEASE_SH_COPY="$copy" RELEASE_REPO="${RELEASE_REPO:-$here}" exec bash "$copy" "$@"
fi
trap 'rm -f "$RELEASE_SH_COPY"' EXIT
cd "$RELEASE_REPO"
. scripts/_common.sh

RELEASE_BRANCH="${RELEASE_BRANCH:-claude/dating-bot-v2}"
RELEASE_REMOTE="${RELEASE_REMOTE:-origin}"
RELEASE_DIR="${RELEASE_DIR:-$RELEASE_REPO/releases}"
LEDGER="$RELEASE_DIR/history.jsonl"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-3}"
MIN_FREE_MB="${MIN_FREE_MB:-2048}"
BACKUP_SCRIPT="${BACKUP_SCRIPT:-scripts/backup.sh}"
ENV_FILE_PATH="${ENV_FILE:-.env}"
REQUIRED_ENV_KEYS=(BOT_TOKEN ADMIN_IDS POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB ADMIN_DOMAIN
    ADMIN_SESSION_SECRET ADMIN_BOT_USERNAME)

TAG_RE='^v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-rc[0-9]{1,3})?$'
SHA_RE='^[0-9a-f]{40}$'

say() { printf '%s\n' "$*"; }
die() { printf 'release: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- input validation

validate_tag() { [[ "${1:-}" =~ $TAG_RE ]] || die "not a release tag (expected vX.Y.Z or vX.Y.Z-rcN): '${1:-}'"; }
validate_sha() { [[ "${1:-}" =~ $SHA_RE ]] || die "--expect-sha must be the full 40-character lowercase commit id"; }

# ---------------------------------------------------------------- the ledger (append-only)
# One JSON object per line. Every value is validated to a safe alphabet before it is written, so
# the lines can be read back with sed and can never contain a quote, a backslash or a newline.

safe() { local v="${1:-}"; [[ "$v" =~ ^[A-Za-z0-9._@:/,=+-]*$ ]] || v="invalid"; printf '%s' "${v:0:120}"; }

operator() { safe "${SUDO_USER:-$(id -un 2>/dev/null || echo unknown)}"; }

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

ledger_add() {  # action tag sha prev_tag prev_sha result schema backup note
    mkdir -p "$RELEASE_DIR"; chmod 700 "$RELEASE_DIR"
    [[ -f "$LEDGER" ]] || : > "$LEDGER"
    chmod 600 "$LEDGER"
    printf '{"ts":"%s","action":"%s","tag":"%s","sha":"%s","prev_tag":"%s","prev_sha":"%s","operator":"%s","result":"%s","schema":"%s","backup":"%s","note":"%s"}\n' \
        "$(now)" "$(safe "$1")" "$(safe "$2")" "$(safe "$3")" "$(safe "${4:-}")" "$(safe "${5:-}")" \
        "$(operator)" "$(safe "$6")" "$(safe "${7:-}")" "$(safe "${8:-}")" "$(safe "${9:-}")" >> "$LEDGER"
}

field() { sed -n 's/.*"'"$2"'":"\([^"]*\)".*/\1/p' <<<"$1"; }

ledger_lines() { [[ -f "$LEDGER" ]] && cat "$LEDGER" || true; }

# The recorded known-good releases, oldest first: "tag sha schema" per line (latest record wins).
known_good() {
    local line
    while IFS= read -r line; do
        [[ "$(field "$line" result)" == known-good ]] || continue
        printf '%s %s %s\n' "$(field "$line" tag)" "$(field "$line" sha)" "$(field "$line" schema)"
    done < <(ledger_lines)
}

record_for() {  # tag sha result  -> the newest ledger line with that tag, sha and result
    local line out=""
    while IFS= read -r line; do
        [[ "$(field "$line" tag)" == "$1" && "$(field "$line" sha)" == "$2" \
            && "$(field "$line" result)" == "$3" ]] && out="$line"
    done < <(ledger_lines)
    printf '%s' "$out"
}

# ---------------------------------------------------------------- docker / compose

compose_ready() {
    command -v docker >/dev/null 2>&1 || die "docker is not installed"
    detect_compose
    ((${#COMPOSE[@]})) || die "neither 'docker compose' nor 'docker-compose' works here"
}

# Only what this script needs. No down, rm, volume, prune, -v, --remove-orphans: ever.
compose() {
    local sub="${1:-}" arg
    case "$sub" in build | run | up | ps | config | logs) ;; *) die "internal: 'compose $sub' is not allowed" ;; esac
    for arg in "$@"; do
        case "$arg" in -v | --volumes | --remove-orphans | down | rm) die "internal: '$arg' is not allowed" ;; esac
    done
    "${COMPOSE[@]}" --profile web "$@"
}

container_of() {  # bot | web
    local override=""
    [[ "$1" == bot ]] && override="${BOT_CONTAINER:-}"
    [[ "$1" == web ]] && override="${WEB_CONTAINER:-}"
    if [[ -n "$override" ]]; then printf '%s' "$override"; return 0; fi
    compose ps -q "$1" 2>/dev/null | head -n1 | tr -d '\r'
}

label_of() {  # container-or-image label
    docker inspect --format '{{ index .Config.Labels "'"$2"'" }}' "$1" 2>/dev/null | tr -d '\r' || true
}

# "sha|version" of what a container is running ("|" if it is not running or is unlabeled)
running_of() {
    local cid
    cid="$(container_of "$1")"
    [[ -n "$cid" ]] || { printf '|'; return 0; }
    printf '%s|%s' "$(label_of "$cid" org.opencontainers.image.revision)" \
        "$(label_of "$cid" org.opencontainers.image.version)"
}

health_of() {  # container -> "running/healthy" style text
    docker inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null | tr -d '\r' || echo "missing/none"
}

db_latest() {  # the newest applied migration NUMBER, or empty
    local db v var
    for var in POSTGRES_USER POSTGRES_DB DB_CONTAINER; do load_env_var "$var"; done
    db="$(db_container 2>/dev/null)" || return 0
    v="$(docker exec "$db" psql -U "${POSTGRES_USER:-}" -d "${POSTGRES_DB:-}" -tAc \
        "SELECT coalesce(max(version), '') FROM schema_migrations" 2>/dev/null | tr -d '[:space:]' || true)"
    [[ "$v" =~ ^[0-9]{3} ]] && printf '%s' "$((10#${v:0:3}))"
    return 0
}

image_of() { printf 'dating-bot/%s:%s' "$1" "$2"; }

image_exists() { docker image inspect "$(image_of "$1" "$2")" >/dev/null 2>&1; }

# "latest_migration=N / schema_tolerant=1" of an image, without a database
image_info() {  # service tag -> key=value lines
    docker run --rm --entrypoint python "$(image_of "$1" "$2")" -m bot.db.migrate --info 2>/dev/null | tr -d '\r'
}

kv() { sed -n "s/^$2=//p" <<<"$1" | head -n1; }

# ---------------------------------------------------------------- arguments

TAG="" EXPECT_SHA="" TO_TAG="" TO_GIVEN=0 HISTORY_N=10
ALLOW_RC=0 ASSUME_YES=0 REBUILD=0 NO_ROLLBACK_OK=0

parse_args() {  # <positional-name> then the arguments
    local want="$1"; shift
    while (($#)); do
        case "$1" in
            --expect-sha) (($# >= 2)) || die "--expect-sha needs a value"; EXPECT_SHA="$2"; shift 2 ;;
            --to) (($# >= 2)) || die "--to needs a value"; TO_TAG="$2"; TO_GIVEN=1; shift 2 ;;
            --allow-rc) ALLOW_RC=1; shift ;;
            --yes) ASSUME_YES=1; shift ;;
            --rebuild) REBUILD=1; shift ;;
            --accept-no-rollback) NO_ROLLBACK_OK=1; shift ;;
            -*) die "unknown option: $1" ;;
            *)
                case "$want" in
                    tag) [[ -z "$TAG" ]] || die "only one tag may be given"; TAG="$1" ;;
                    count) HISTORY_N="$1" ;;
                    none) die "unexpected argument: $1" ;;
                esac
                shift ;;
        esac
    done
}

confirm() {  # <what to type> <message>
    [[ "$ASSUME_YES" == 1 ]] && return 0
    [[ -t 0 ]] || die "this needs a terminal to confirm (or pass --yes)"
    say "$2"
    local answer
    read -r -p "Type '$1' to continue: " answer
    [[ "$answer" == "$1" ]] || die "not confirmed, nothing was changed"
}

lock() {
    mkdir -p "$RELEASE_DIR"; chmod 700 "$RELEASE_DIR"
    command -v flock >/dev/null 2>&1 || die "flock is required (util-linux)"
    exec 9> "$RELEASE_DIR/.lock"
    flock -n 9 || die "another release command is running (lock: $RELEASE_DIR/.lock)"
}

# ---------------------------------------------------------------- checks that change nothing

env_check() {
    [[ -f "$ENV_FILE_PATH" ]] || die "$ENV_FILE_PATH is missing"
    local key missing=()
    for key in "${REQUIRED_ENV_KEYS[@]}"; do
        grep -Eq "^${key}=." "$ENV_FILE_PATH" || missing+=("$key")
    done
    ((${#missing[@]} == 0)) || die "$ENV_FILE_PATH has no value for: ${missing[*]} (names only; values are never read out)"
}

# ---------------------------------------------------------------- tag diagnosis (read-only)
# Only git cat-file / rev-parse / ls-remote: nothing is created, moved, deleted or fetched here.
# The state of the LOCAL tag is what deploy and adopt act on, so that is what decides.
# Sets TD_STATE (missing|lightweight|annotated|invalid), TD_OBJECT (what refs/tags/<tag> names),
# TD_TARGET + TD_TARGET_TYPE (what an annotated tag object points at), TD_PEELED (the commit),
# TD_SUITABLE (yes|no) and TD_REASON.

tag_diagnose() {  # tag expected-sha
    local tag="$1" want="$2" ref="refs/tags/$1" kind
    TD_STATE=missing; TD_OBJECT=-; TD_TARGET=-; TD_TARGET_TYPE=-; TD_PEELED=-; TD_SUITABLE=no
    TD_REASON="$tag is not an annotated tag in this repository (it does not exist here)"
    git rev-parse -q --verify "$ref" >/dev/null 2>&1 || return 0
    TD_OBJECT="$(git rev-parse "$ref")"
    kind="$(git cat-file -t "$ref" 2>/dev/null || true)"
    case "$kind" in
        commit)
            TD_STATE=lightweight; TD_PEELED="$TD_OBJECT"
            TD_REASON="$tag is a LIGHTWEIGHT tag (a bare reference to a commit), not an annotated tag"
            [[ "$TD_PEELED" == "$want" ]] || TD_REASON="$TD_REASON, and it points to a DIFFERENT commit" ;;
        tag)
            TD_STATE=annotated
            TD_TARGET="$(git cat-file tag "$ref" | sed -n '1s/^object //p')"
            TD_TARGET_TYPE="$(git cat-file tag "$ref" | sed -n '2s/^type //p')"
            TD_PEELED="$(git rev-parse -q --verify "$ref^{commit}" 2>/dev/null || echo -)"
            if [[ "$TD_TARGET_TYPE" != commit ]]; then
                TD_REASON="the annotated tag $tag points at a ${TD_TARGET_TYPE:-?}, not directly at a commit"
            elif [[ "$TD_PEELED" != "$want" ]]; then
                TD_REASON="tag $tag points to $TD_PEELED, not the expected $want: refusing"
            else
                TD_SUITABLE=yes; TD_REASON="annotated tag, peeled commit equals the expected commit"
            fi ;;
        *)
            TD_STATE=invalid
            TD_REASON="refs/tags/$tag names a ${kind:-unreadable} object, not an annotated tag" ;;
    esac
}

# What origin holds, for the diagnosis only. Sets TD_REMOTE_STATE (annotated|lightweight|missing|
# unreachable) and TD_REMOTE_PEELED. It never blocks by itself when it cannot be read.
remote_tag_diagnose() {  # tag
    local tag="$1" out
    TD_REMOTE_STATE=unreachable; TD_REMOTE_PEELED=-
    out="$(git ls-remote "$RELEASE_REMOTE" "refs/tags/$tag" "refs/tags/$tag^{}" 2>/dev/null)" || return 0
    TD_REMOTE_PEELED="$(awk -v r="refs/tags/$tag^{}" '$2 == r { print $1 }' <<<"$out")"
    if [[ -n "$TD_REMOTE_PEELED" ]]; then
        TD_REMOTE_STATE=annotated
    else
        TD_REMOTE_PEELED="$(awk -v r="refs/tags/$tag" '$2 == r { print $1 }' <<<"$out")"
        if [[ -n "$TD_REMOTE_PEELED" ]]; then
            TD_REMOTE_STATE=lightweight
        else
            TD_REMOTE_STATE=missing; TD_REMOTE_PEELED=-
        fi
    fi
}

tag_remediation() {  # tag expected-sha current-tag-object
    local tag="$1" sha="$2" obj="${3:-<current tag object>}"
    cat <<EOF
Production releases require an ANNOTATED tag (git tag -a). release.sh never deletes, moves or
force-updates a tag, and nothing was changed. To fix it, a person who may push tags does this on a
development machine. It REPLACES the tag in one step (never delete first), and only if origin still
holds the tag you saw:

  git fetch origin
  git rev-parse $sha^{commit}           # must print exactly $sha
  git tag -a -f -m "$tag" $tag $sha     # -f replaces only your LOCAL tag; it names exactly $sha
  git push --force-with-lease=refs/tags/$tag:$obj origin refs/tags/$tag
  git ls-remote origin 'refs/tags/$tag*'   # expect a refs/tags/$tag^{} line equal to $sha

(A tag made in the GitHub web UI, or by a release made from it, is lightweight.) Then, on this
server: git fetch --force origin refs/tags/$tag:refs/tags/$tag   # 'git fetch --tags' alone never
updates a tag that already exists here. Check with:
  scripts/release.sh check-tag $tag --expect-sha $sha
EOF
}

# The conversion procedure is only ever shown for a lightweight tag that already names the expected
# commit. If it names another commit, re-tagging would MOVE a published tag: say STOP instead.
tag_mismatch_notice() {  # current-sha expected-sha
    printf 'STOP. The tag currently points to %s, not %s. Do not re-tag or move it. Investigate the mismatch first.\n' "$1" "$2"
}

tag_advice() {  # tag expected-sha current-sha-of-the-lightweight-ref
    if [[ "$3" == "$2" ]]; then tag_remediation "$1" "$2" "$3"; else tag_mismatch_notice "$3" "$2"; fi
}

tag_refusal() {  # tag expected-sha: die with the reason, plus the advice for a lightweight tag
    local msg="$TD_REASON"
    if [[ "$TD_STATE" == lightweight ]]; then
        msg+=$'\n'"$(tag_advice "$1" "$2" "$TD_OBJECT")"
    fi
    die "$msg"
}

git_check() {  # tag sha
    local tag="$1" sha="$2"
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git checkout"
    [[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "the working tree has uncommitted changes: refusing"
    git fetch --quiet --tags "$RELEASE_REMOTE" || die "git fetch from '$RELEASE_REMOTE' failed"
    git fetch --quiet "$RELEASE_REMOTE" "$RELEASE_BRANCH" || die "cannot fetch $RELEASE_REMOTE/$RELEASE_BRANCH"
    tag_diagnose "$tag" "$sha"
    [[ "$TD_SUITABLE" == yes ]] || tag_refusal "$tag" "$sha"
    git merge-base --is-ancestor "$sha" "$RELEASE_REMOTE/$RELEASE_BRANCH" \
        || die "$sha is not part of $RELEASE_REMOTE/$RELEASE_BRANCH: refusing"
    local path
    for path in Dockerfile docker-compose.yml scripts/backup.sh release/migration_compat.json; do
        git cat-file -e "refs/tags/$tag:$path" 2>/dev/null || die "$tag does not contain $path"
    done
}

host_check() {
    local free_kb
    free_kb="$(df -Pk . | awk 'NR==2 {print $4}')"
    [[ "$free_kb" =~ ^[0-9]+$ ]] && ((free_kb / 1024 >= MIN_FREE_MB)) \
        || die "less than ${MIN_FREE_MB} MB free on this disk: refusing to build"
    compose config -q >/dev/null 2>&1 || die "docker compose config is invalid"
    local db
    db="$(db_container 2>/dev/null)" || die "the db container is not running"
    [[ "$(health_of "$db")" == running/healthy ]] || die "the db container is not healthy ($(health_of "$db"))"
}

preflight_checks() {
    compose_ready
    env_check
    git_check "$1" "$2"
    host_check
}

# ---------------------------------------------------------------- read-only commands

cmd_status() {
    compose_ready
    local head tag dirty bot web bl
    head="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    tag="$(git describe --tags --exact-match 2>/dev/null || echo '-')"
    dirty="clean"; [[ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]] || dirty="UNCOMMITTED CHANGES"
    say "checkout : $tag  $head  ($dirty)"
    bot="$(running_of bot)"; web="$(running_of web)"
    say "bot      : ${bot#*|} ${bot%%|*}  $(health_of "$(container_of bot)")"
    say "web      : ${web#*|} ${web%%|*}  $(health_of "$(container_of web)")"
    say "database : latest applied migration $(db_latest || true)"
    say "known-good (oldest first):"
    known_good | sed 's/^/  /' | tail -n 5
    say "recent ledger:"
    ledger_lines | tail -n 5 | while IFS= read -r bl; do
        say "  $(field "$bl" ts) $(field "$bl" action) $(field "$bl" tag) $(field "$bl" result) by $(field "$bl" operator)"
    done
}

cmd_history() {
    [[ "$HISTORY_N" =~ ^[0-9]{1,4}$ ]] || die "history takes a number"
    local line
    ledger_lines | tail -n "$HISTORY_N" | while IFS= read -r line; do
        say "$(field "$line" ts)  $(field "$line" action)  $(field "$line" tag)  $(field "$line" sha)  $(field "$line" result)  prev=$(field "$line" prev_tag)  schema=$(field "$line" schema)  by $(field "$line" operator)  backup=$(field "$line" backup)  $(field "$line" note)"
    done
}

cmd_check_tag() {  # read-only: no lock, no docker, no fetch, no ledger, no directory is created
    validate_tag "$TAG"; validate_sha "$EXPECT_SHA"
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git checkout"
    tag_diagnose "$TAG" "$EXPECT_SHA"
    remote_tag_diagnose "$TAG"
    if [[ "$TD_SUITABLE" == yes ]]; then
        if [[ "$TD_REMOTE_STATE" == lightweight ]]; then
            TD_SUITABLE=no
            TD_REASON="the local tag is annotated but $RELEASE_REMOTE holds a lightweight $TAG"
        elif [[ "$TD_REMOTE_STATE" == annotated && "$TD_REMOTE_PEELED" != "$EXPECT_SHA" ]]; then
            TD_SUITABLE=no
            TD_REASON="$RELEASE_REMOTE holds $TAG pointing to $TD_REMOTE_PEELED, not the expected $EXPECT_SHA"
        fi
    fi
    say "tag             : $TAG"
    say "state (local)   : $TD_STATE"
    say "tag object      : $TD_OBJECT"
    say "tag target      : $TD_TARGET ($TD_TARGET_TYPE)"
    say "peeled commit   : $TD_PEELED"
    say "expected commit : $EXPECT_SHA"
    say "state (remote)  : $TD_REMOTE_STATE  peeled $TD_REMOTE_PEELED  [$RELEASE_REMOTE]"
    say "suitable        : $TD_SUITABLE"
    say "reason          : $TD_REASON"
    if [[ "$TD_SUITABLE" != yes ]]; then
        # the lightweight ref that would be replaced: origin's if it is lightweight, else the local one
        if [[ "$TD_REMOTE_STATE" == lightweight ]]; then
            tag_advice "$TAG" "$EXPECT_SHA" "$TD_REMOTE_PEELED"
        elif [[ "$TD_STATE" == lightweight && "$TD_REMOTE_STATE" != annotated ]]; then
            tag_advice "$TAG" "$EXPECT_SHA" "$TD_OBJECT"
        fi
        say "check-tag: NOT suitable for deployment. Nothing was changed."
        return 1
    fi
    say "check-tag: suitable for deployment. Nothing was changed."
}

cmd_preflight() {
    validate_tag "$TAG"; validate_sha "$EXPECT_SHA"
    [[ "$TAG" != *-rc* || "$ALLOW_RC" == 1 ]] || die "$TAG is a release candidate: add --allow-rc to put it on this server"
    preflight_checks "$TAG" "$EXPECT_SHA"
    say "preflight ok for $TAG ($EXPECT_SHA): nothing was changed"
}

# ---------------------------------------------------------------- health verification

HEALTH_WHY=""

wait_healthy() {  # <expected sha> <since (UTC)> [<expected newest migration>]
    local sha="$1" since="$2" schema="${3:-}" deadline svc cid h rev bot_cid logs latest
    deadline=$((SECONDS + HEALTH_TIMEOUT))
    while :; do
        HEALTH_WHY=""
        for svc in bot web; do
            cid="$(container_of "$svc")"
            if [[ -z "$cid" ]]; then HEALTH_WHY="the $svc container is missing"; continue; fi
            h="$(health_of "$cid")"
            [[ "$h" == running/healthy ]] || HEALTH_WHY="the $svc container is $h"
            rev="$(label_of "$cid" org.opencontainers.image.revision)"
            [[ "$rev" == "$sha" ]] || HEALTH_WHY="the $svc container runs '$rev', not $sha"
        done
        [[ -z "$HEALTH_WHY" ]] && break
        ((SECONDS < deadline)) || return 1
        sleep "$HEALTH_INTERVAL"
    done
    load_env_var ADMIN_DOMAIN; load_env_var ADMIN_WEB_PORT
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 5 -H "Host: ${ADMIN_DOMAIN:-localhost}" \
            "http://127.0.0.1:${ADMIN_WEB_PORT:-8000}/healthz" 2>/dev/null | grep -q '^ok' \
            || { HEALTH_WHY="web /healthz did not answer ok"; return 1; }
    fi
    bot_cid="$(container_of bot)"
    logs="$(docker logs --since "$since" "$bot_cid" 2>&1 | tr -d '\r' || true)"
    grep -q 'starting long polling' <<<"$logs" || { HEALTH_WHY="the bot did not reach 'starting long polling'"; return 1; }
    if grep -Eq 'Conflict: terminated by other getUpdates' <<<"$logs"; then
        HEALTH_WHY="Telegram answered 409 Conflict: another process is polling with this bot token"
        return 1
    fi
    if [[ -n "$schema" ]]; then
        latest="$(db_latest || true)"
        [[ "$latest" == "$schema" ]] || { HEALTH_WHY="the database is at migration '${latest}', expected ${schema}"; return 1; }
    fi
    return 0
}

# ---------------------------------------------------------------- deploy

# State for the interrupt handler and the failure paths of one deploy / rollback.
ACTION="" CUR_TAG="" CUR_SHA="" PRV_TAG="" PRV_SHA="" PHASE="" RESTORE_REF="" SAFE_TO_RESTORE=0 BACKUP_NAME=""

on_interrupt() {
    trap - INT TERM HUP
    if [[ -n "$ACTION" ]]; then
        ledger_add "$ACTION" "$CUR_TAG" "$CUR_SHA" "$PRV_TAG" "$PRV_SHA" interrupted "" "$BACKUP_NAME" "during:$PHASE"
        say "release: interrupted during '$PHASE'. Nothing is rolled back automatically: run 'release.sh status', then decide." >&2
    fi
    exit 130
}

restore_checkout() {  # put the working tree back where it was (only before containers were replaced)
    [[ "$SAFE_TO_RESTORE" == 1 && -n "$RESTORE_REF" ]] && git checkout -q "$RESTORE_REF" 2>/dev/null || true
}

fail() {  # <result> <message>
    ledger_add "$ACTION" "$CUR_TAG" "$CUR_SHA" "$PRV_TAG" "$PRV_SHA" "$1" "" "$BACKUP_NAME" "${3:-}"
    restore_checkout
    die "$2"
}

run_with_release() {  # <tag> <sha> <extra VAR=value...> -- compose args...   (never leaks anything else)
    local tag="$1" sha="$2"; shift 2
    local extra=()
    while (($#)) && [[ "$1" != -- ]]; do extra+=("$1"); shift; done
    shift
    ( export RELEASE_TAG="$tag" GIT_SHA="$sha" BUILD_TIME="${BUILD_TIME_VALUE:-$(now)}"
      for kv in "${extra[@]}"; do export "$kv"; done
      compose "$@" )
}

# Restore the application of a recorded release WITHOUT touching the database.
# Used by `rollback` and by the automatic rollback after a failed deploy. Never loops.
do_rollback() {  # <to_tag> <to_sha> <to_schema> <from_tag> <manual:0|1>
    local to_tag="$1" to_sha="$2" to_schema="$3" from_tag="$4" manual="$5" gate verdict newer="" tolerant info
    PHASE="rollback-gate"
    image_exists bot "$to_tag" && image_exists web "$to_tag" || {
        if [[ "$REBUILD" == 1 && "$manual" == 1 ]]; then
            say "rebuilding $to_tag from its recorded commit $to_sha ..."
            git cat-file -e "$to_sha^{commit}" 2>/dev/null || die "commit $to_sha is not in this repository"
            git checkout -q --detach "$to_sha"
            run_with_release "$to_tag" "$to_sha" -- build bot web || die "rebuild failed; nothing was changed"
        else
            say "release: the images for $to_tag are not on this server (use --rebuild to build them from $to_sha)" >&2
            return 3
        fi
    }
    info="$(image_info bot "$to_tag")"
    tolerant="$(kv "$info" schema_tolerant)"
    # The gate runs on the CURRENT (newer) image: it knows the manifest and reads the database.
    gate="$(run_with_release "$from_tag" "" -- run --rm --no-deps bot python -m bot.db.migrate --check-rollback "$to_schema" 2>&1 | tr -d '\r')" && verdict=0 || verdict=$?
    if ((verdict != 0)); then
        say "release: application rollback to $to_tag is NOT safe for this database:" >&2
        printf '%s\n' "$gate" | grep -E '^(blocked|allowed|newer)=' | sed 's/^/  /' >&2 || true
        say "  Nothing was changed. The database is never rolled back by this script; see docs/RELEASE_RUNBOOK.md." >&2
        return 4
    fi
    newer="$(kv "$gate" newer)"
    if [[ -n "$newer" && "$tolerant" != 1 ]]; then
        say "release: $to_tag cannot run on the newer schema ($newer): it does not declare schema tolerance" >&2
        return 4
    fi
    PHASE="rollback-restart"
    local since extra=()
    [[ -n "$newer" ]] && extra=(ALLOW_NEWER_SCHEMA=1)
    git checkout -q --detach "$to_sha"
    since="$(now)"
    run_with_release "$to_tag" "$to_sha" "${extra[@]}" -- up -d --no-deps --no-build bot || true
    run_with_release "$to_tag" "$to_sha" "${extra[@]}" -- up -d --no-deps --no-build web || true
    PHASE="rollback-health"
    if wait_healthy "$to_sha" "$since"; then
        return 0
    fi
    return 5
}

cmd_deploy() {
    validate_tag "$TAG"; validate_sha "$EXPECT_SHA"
    [[ "$TAG" != *-rc* || "$ALLOW_RC" == 1 ]] || die "$TAG is a release candidate: add --allow-rc to put it on this server"
    lock
    preflight_checks "$TAG" "$EXPECT_SHA"

    local bot web good prev_schema="" plan info new_schema tolerant since latest
    bot="$(running_of bot)"; web="$(running_of web)"
    PRV_SHA="${bot%%|*}"; PRV_TAG="${bot#*|}"
    [[ -n "$PRV_SHA" ]] || die "cannot tell which release is running (the bot container has no release labels or is not running): deploy the bootstrap release first (docs/RELEASE_RUNBOOK.md)"
    [[ "${web%%|*}" == "$PRV_SHA" ]] || die "the bot (${PRV_SHA}) and the web (${web%%|*}) run different releases: fix that first"
    [[ "$PRV_SHA" != "$EXPECT_SHA" ]] || die "$TAG is already running"
    good="$(record_for "$PRV_TAG" "$PRV_SHA" known-good)"
    if [[ -n "$good" ]]; then
        prev_schema="$(field "$good" schema)"
    elif [[ "$NO_ROLLBACK_OK" != 1 ]]; then
        die "the running release $PRV_TAG is not recorded as known-good, so there would be nothing to roll back to: run 'release.sh adopt' / 'mark-good' first, or pass --accept-no-rollback"
    fi

    confirm "$TAG" "Deploy $TAG ($EXPECT_SHA) over ${PRV_TAG:-?} ($PRV_SHA). Order: backup, build, migrate, restart bot, restart web. The bot is down ~10-30 s and the web ~5-15 s."

    ACTION=deploy; CUR_TAG="$TAG"; CUR_SHA="$EXPECT_SHA"
    RESTORE_REF="$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)"; SAFE_TO_RESTORE=1
    trap on_interrupt INT TERM HUP
    ledger_add deploy "$TAG" "$EXPECT_SHA" "$PRV_TAG" "$PRV_SHA" started
    BUILD_TIME_VALUE="$(now)"

    PHASE=backup
    say "== backup"
    local out
    out="$("$BACKUP_SCRIPT" 2>&1)" || { printf '%s\n' "$out" >&2; fail failed-backup "the backup failed: nothing was changed"; }
    printf '%s\n' "$out"
    BACKUP_NAME="$(basename "$(sed -n 's/^backup written: \(.*\) (.*)$/\1/p' <<<"$out" | tail -n1)")"
    [[ "$BACKUP_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || BACKUP_NAME="unknown"

    PHASE=checkout
    git checkout -q --detach "$EXPECT_SHA"
    [[ "$(git rev-parse HEAD)" == "$EXPECT_SHA" ]] || fail failed-checkout "checkout did not land on $EXPECT_SHA"

    PHASE=build
    say "== build (the running containers are not touched)"
    run_with_release "$TAG" "$EXPECT_SHA" -- build bot web || fail failed-build "the build failed: the running containers were not touched"
    [[ "$(label_of "$(image_of bot "$TAG")" org.opencontainers.image.revision)" == "$EXPECT_SHA" ]] \
        || fail failed-build "the built bot image does not carry $EXPECT_SHA"
    info="$(image_info bot "$TAG")"
    new_schema="$(kv "$info" latest_migration)"; tolerant="$(kv "$info" schema_tolerant)"
    [[ "$new_schema" =~ ^[0-9]+$ ]] || fail failed-build "cannot read the new image's newest migration"
    [[ "$tolerant" == 1 ]] || fail failed-build "this release does not declare schema tolerance: it could never be a rollback target"

    PHASE=plan
    say "== migration plan"
    plan="$(run_with_release "$TAG" "$EXPECT_SHA" -- run --rm --no-deps bot python -m bot.db.migrate --plan 2>&1 | tr -d '\r')" \
        || fail failed-plan "the migration plan failed (inconsistent database or files): nothing was changed
$plan"
    printf '%s\n' "$plan" | grep -E '^pending' | sed 's/^/  /'
    if [[ "$(kv "$plan" rollback_safe)" != 1 && "$NO_ROLLBACK_OK" != 1 ]]; then
        fail failed-plan "this release applies contract or unclassified migrations ($(kv "$plan" pending_contract)$(kv "$plan" pending_unlisted)): an application rollback would not be possible. Nothing was changed. Ship the destructive part in a LATER release, or pass --accept-no-rollback"
    fi

    PHASE=migrate
    say "== migrate (one transaction per file; the running bot keeps serving)"
    run_with_release "$TAG" "$EXPECT_SHA" -- run --rm --no-deps bot python -m bot.db.migrate \
        || fail failed-migrate "a migration failed and was rolled back by the database: the running containers were not touched"

    PHASE=restart
    SAFE_TO_RESTORE=0
    say "== restart bot, then web"
    since="$(now)"
    run_with_release "$TAG" "$EXPECT_SHA" -- up -d --no-deps --no-build bot || true
    run_with_release "$TAG" "$EXPECT_SHA" -- up -d --no-deps --no-build web || true

    PHASE=health
    say "== health (up to ${HEALTH_TIMEOUT}s)"
    if wait_healthy "$EXPECT_SHA" "$since" "$new_schema"; then
        ledger_add deploy "$TAG" "$EXPECT_SHA" "$PRV_TAG" "$PRV_SHA" healthy "$new_schema" "$BACKUP_NAME" ""
        trap - INT TERM HUP
        say "deployed $TAG ($EXPECT_SHA): healthy."
        say "Now check it by hand (send /ping to the bot, log in to the admin), then: scripts/release.sh mark-good $TAG"
        return 0
    fi
    say "release: the new release is NOT healthy: $HEALTH_WHY" >&2
    ledger_add deploy "$TAG" "$EXPECT_SHA" "$PRV_TAG" "$PRV_SHA" failed-health "$new_schema" "$BACKUP_NAME" "$HEALTH_WHY"
    if [[ -z "$prev_schema" ]]; then
        trap - INT TERM HUP
        die "no recorded known-good release to go back to (--accept-no-rollback was used): the new release is left running. Logs: docker compose logs --tail=100 bot web"
    fi
    say "== automatic application rollback to $PRV_TAG (the database is not touched)" >&2
    ACTION=rollback; CUR_TAG="$PRV_TAG"; CUR_SHA="$PRV_SHA"; PRV_TAG="$TAG"; PRV_SHA="$EXPECT_SHA"
    local rc=0
    do_rollback "$CUR_TAG" "$CUR_SHA" "$prev_schema" "$TAG" 0 || rc=$?
    trap - INT TERM HUP
    case "$rc" in
        0)
            ledger_add rollback "$CUR_TAG" "$CUR_SHA" "$PRV_TAG" "$PRV_SHA" rolled-back "$prev_schema" "$BACKUP_NAME" "after failed deploy of $PRV_TAG"
            die "the deploy of $PRV_TAG failed ($HEALTH_WHY) and $CUR_TAG was restored and is healthy"
            ;;
        5)
            ledger_add rollback "$CUR_TAG" "$CUR_SHA" "$PRV_TAG" "$PRV_SHA" rollback-failed "$prev_schema" "$BACKUP_NAME" "$HEALTH_WHY"
            die "ROLLBACK ALSO FAILED ($HEALTH_WHY). Stopping: no further automatic attempt. Look at: scripts/release.sh status; docker compose logs --tail=100 bot web. The newest backup is ${BACKUP_NAME}; restoring it is a separate manual decision (docs/RELEASE_RUNBOOK.md)."
            ;;
        *)
            ledger_add rollback "$CUR_TAG" "$CUR_SHA" "$PRV_TAG" "$PRV_SHA" rollback-refused "$prev_schema" "$BACKUP_NAME" "not possible safely"
            die "the deploy of $PRV_TAG failed ($HEALTH_WHY) and an application rollback is not possible safely (see above). The new release is left running. Decide manually: docs/RELEASE_RUNBOOK.md"
            ;;
    esac
}

# ---------------------------------------------------------------- rollback / mark-good / adopt

# Candidates for a rollback: recorded known-good releases other than the one running, oldest first,
# "tag sha schema" per line, one line per tag (the latest record of that tag).
rollback_candidates() {  # <running sha>
    known_good | awk -v running="$1" '{ last[$1] = $0; if (!($1 in seen)) { seen[$1] = 1; order[++n] = $1 } }
        END { for (i = 1; i <= n; i++) { split(last[order[i]], f, " "); if (f[2] != running) print last[order[i]] } }'
}

cmd_rollback() {
    lock
    compose_ready
    local bot run_sha run_tag list choice tag sha schema rc=0 line
    bot="$(running_of bot)"; run_sha="${bot%%|*}"; run_tag="${bot#*|}"
    [[ -n "$run_sha" && -n "$run_tag" ]] || die "cannot tell which release is running (no release labels): nothing to roll back from"
    list="$(rollback_candidates "$run_sha")"
    [[ -n "$list" ]] || die "no known-good release is recorded (other than the running one): nothing to roll back to"
    if [[ "$TO_GIVEN" == 1 ]]; then
        validate_tag "$TO_TAG"
        choice="$(awk -v t="$TO_TAG" '$1 == t { line = $0 } END { print line }' <<<"$list")"
        [[ -n "$choice" ]] || die "$TO_TAG is not a recorded known-good release. Choices:
$(sed 's/^/  /' <<<"$list")"
    else
        choice="$(tail -n1 <<<"$list")"
    fi
    read -r tag sha schema <<<"$choice"
    [[ "$tag" =~ $TAG_RE && "$sha" =~ $SHA_RE && "$schema" =~ ^[0-9]+$ ]] || die "the ledger entry for $tag is damaged: refusing"

    confirm "$tag" "Roll the APPLICATION back from $run_tag ($run_sha) to the known-good $tag ($sha). The database is NOT rolled back; it is checked for compatibility first."

    ACTION=rollback; CUR_TAG="$tag"; CUR_SHA="$sha"; PRV_TAG="$run_tag"; PRV_SHA="$run_sha"; PHASE=rollback
    trap on_interrupt INT TERM HUP
    ledger_add rollback "$tag" "$sha" "$run_tag" "$run_sha" started "$schema"
    do_rollback "$tag" "$sha" "$schema" "$run_tag" 1 || rc=$?
    trap - INT TERM HUP
    case "$rc" in
        0)
            ledger_add rollback "$tag" "$sha" "$run_tag" "$run_sha" rolled-back "$schema"
            say "rolled back to $tag ($sha): healthy. Check it by hand (/ping, admin login)."
            ;;
        5)
            ledger_add rollback "$tag" "$sha" "$run_tag" "$run_sha" rollback-failed "$schema" "" "$HEALTH_WHY"
            die "the rollback is NOT healthy ($HEALTH_WHY). Stopping: no further automatic attempt. Look at: scripts/release.sh status; docker compose logs --tail=100 bot web. A database restore is a separate manual emergency decision (docs/RELEASE_RUNBOOK.md)."
            ;;
        *)
            ledger_add rollback "$tag" "$sha" "$run_tag" "$run_sha" rollback-refused "$schema" "" "not possible safely"
            die "rollback to $tag was refused (see above). Nothing was changed."
            ;;
    esac
}

cmd_mark_good() {
    validate_tag "$TAG"
    lock
    compose_ready
    local line sha schema bot web
    line="$(awk -v t="$TAG" 'BEGIN { RS = "\n" } index($0, "\"tag\":\"" t "\"") && index($0, "\"result\":\"healthy\"") { last = $0 } END { print last }' <<<"$(ledger_lines)")"
    [[ -n "$line" ]] || die "no healthy deploy of $TAG is recorded: nothing to mark"
    sha="$(field "$line" sha)"; schema="$(field "$line" schema)"
    bot="$(running_of bot)"; web="$(running_of web)"
    [[ "${bot%%|*}" == "$sha" && "${bot#*|}" == "$TAG" && "${web%%|*}" == "$sha" && "${web#*|}" == "$TAG" ]] \
        || die "$TAG ($sha) is not what the bot and web are running now (bot: ${bot#*|} ${bot%%|*}, web: ${web#*|} ${web%%|*})"
    [[ "$(health_of "$(container_of bot)")" == running/healthy && "$(health_of "$(container_of web)")" == running/healthy ]] \
        || die "the bot or the web is not healthy right now: not marking $TAG as known-good"
    confirm "$TAG" "Mark $TAG ($sha) as KNOWN-GOOD: it becomes a rollback target."
    ledger_add mark-good "$TAG" "$sha" "$(field "$line" prev_tag)" "$(field "$line" prev_sha)" known-good "$schema" "$(field "$line" backup)" "marked after manual verification"
    say "$TAG ($sha) is now recorded as known-good."
}

cmd_adopt() {  # record a release that is ALREADY running (the first one, deployed by hand)
    validate_tag "$TAG"; validate_sha "$EXPECT_SHA"
    # before the lock and before any docker call; a tag that is not in this checkout is still allowed (bootstrap)
    tag_diagnose "$TAG" "$EXPECT_SHA"
    [[ "$TD_STATE" == missing || "$TD_SUITABLE" == yes ]] || tag_refusal "$TAG" "$EXPECT_SHA"
    lock
    compose_ready
    local bot web info schema tolerant
    bot="$(running_of bot)"; web="$(running_of web)"
    [[ "${bot%%|*}" == "$EXPECT_SHA" && "${bot#*|}" == "$TAG" ]] || die "the bot is not running $TAG ($EXPECT_SHA) (it runs: ${bot#*|} ${bot%%|*})"
    [[ "${web%%|*}" == "$EXPECT_SHA" && "${web#*|}" == "$TAG" ]] || die "the web is not running $TAG ($EXPECT_SHA) (it runs: ${web#*|} ${web%%|*})"
    [[ "$(health_of "$(container_of bot)")" == running/healthy && "$(health_of "$(container_of web)")" == running/healthy ]] \
        || die "the bot or the web is not healthy right now: not adopting"
    image_exists bot "$TAG" && image_exists web "$TAG" || die "the images dating-bot/{bot,web}:$TAG are not on this server, so $TAG could never be restored"
    info="$(image_info bot "$TAG")"
    schema="$(kv "$info" latest_migration)"; tolerant="$(kv "$info" schema_tolerant)"
    [[ "$schema" =~ ^[0-9]+$ ]] || die "cannot read the newest migration of the $TAG image"
    [[ "$tolerant" == 1 ]] || die "$TAG does not declare schema tolerance: it can not be a safe rollback target"
    confirm "$TAG" "Record the RUNNING release $TAG ($EXPECT_SHA, newest migration $schema) as known-good."
    ledger_add adopt "$TAG" "$EXPECT_SHA" "" "" known-good "$schema" "" "adopted: already running"
    say "$TAG ($EXPECT_SHA) is recorded as known-good."
}

# ---- main ----
cmd="${1:-}"; shift || true
case "$cmd" in
    status) parse_args none "$@"; cmd_status ;;
    history) parse_args count "$@"; cmd_history ;;
    check-tag) parse_args tag "$@"; cmd_check_tag ;;
    preflight) parse_args tag "$@"; lock; cmd_preflight ;;
    deploy) parse_args tag "$@"; cmd_deploy ;;
    adopt) parse_args tag "$@"; cmd_adopt ;;
    mark-good) parse_args tag "$@"; cmd_mark_good ;;
    rollback) parse_args none "$@"; cmd_rollback ;;
    *) die "usage: release.sh status | history [N] | check-tag <tag> --expect-sha <sha> | preflight <tag> --expect-sha <sha> | adopt | deploy | mark-good | rollback" ;;
esac
exit 0

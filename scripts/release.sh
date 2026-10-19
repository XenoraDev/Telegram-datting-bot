#!/usr/bin/env bash
# Release, deploy, rollback and history for the Docker Compose stack. Run it ON THE SERVER, by an
# operator, over SSH. It is not reachable from the web and accepts no arbitrary refs or commands.
#
#   scripts/release.sh status
#   scripts/release.sh history [N]
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
    [[ -f "$LEDGER" ]] || { : > "$LEDGER"; chmod 600 "$LEDGER"; }
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

TAG="" EXPECT_SHA="" TO_TAG="" HISTORY_N=10
ALLOW_RC=0 ASSUME_YES=0 REBUILD=0 NO_ROLLBACK_OK=0

parse_args() {  # <positional-name> then the arguments
    local want="$1"; shift
    while (($#)); do
        case "$1" in
            --expect-sha) (($# >= 2)) || die "--expect-sha needs a value"; EXPECT_SHA="$2"; shift 2 ;;
            --to) (($# >= 2)) || die "--to needs a value"; TO_TAG="$2"; shift 2 ;;
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

git_check() {  # tag sha
    local tag="$1" sha="$2" kind actual
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git checkout"
    [[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "the working tree has uncommitted changes: refusing"
    git fetch --quiet --tags "$RELEASE_REMOTE" || die "git fetch from '$RELEASE_REMOTE' failed"
    git fetch --quiet "$RELEASE_REMOTE" "$RELEASE_BRANCH" || die "cannot fetch $RELEASE_REMOTE/$RELEASE_BRANCH"
    kind="$(git cat-file -t "refs/tags/$tag" 2>/dev/null || true)"
    [[ "$kind" == tag ]] || die "$tag is not an annotated tag in this repository"
    actual="$(git rev-parse "refs/tags/$tag^{commit}")"
    [[ "$actual" == "$sha" ]] || die "tag $tag points to $actual, not the expected $sha: refusing"
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

cmd_preflight() {
    validate_tag "$TAG"; validate_sha "$EXPECT_SHA"
    [[ "$TAG" != *-rc* || "$ALLOW_RC" == 1 ]] || die "$TAG is a release candidate: add --allow-rc to put it on this server"
    preflight_checks "$TAG" "$EXPECT_SHA"
    say "preflight ok for $TAG ($EXPECT_SHA): nothing was changed"
}

# ---- main ----
cmd="${1:-}"; shift || true
case "$cmd" in
    status) parse_args none "$@"; cmd_status ;;
    history) parse_args count "$@"; cmd_history ;;
    preflight) parse_args tag "$@"; lock; cmd_preflight ;;
    *) die "usage: release.sh status | history [N] | preflight <tag> --expect-sha <sha> | adopt | deploy | mark-good | rollback" ;;
esac
exit 0

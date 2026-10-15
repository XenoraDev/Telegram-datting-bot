# Shared helpers for backup.sh / restore.sh. Sourced, not executed.
#
# Why this exists: `docker compose exec -T` is rejected by some Docker/Compose setups
# (e.g. aaPanel-managed hosts: "unknown shorthand flag: 'T' in -T"). Nothing here depends on
# `compose exec`. The compose CLI is only used to FIND the container (`ps -q`, `stop`, `start`);
# the actual pg_dump / psql run through plain `docker exec`, which never allocates a TTY unless
# `-t` is given, so it is non-interactive and pipe-safe.
#
# Overrides (set in .env or the shell) for hosts where even compose is unusable:
#   DB_CONTAINER=<container name or id>   skip compose, use this Postgres container
#   BOT_CONTAINER=<container name or id>  stop/start this container instead of `compose stop/start bot`

load_env_var() {
    # Read ONE variable from .env if it is not already set in the environment.
    # Never `source` the file; strip optional surrounding quotes.
    local var="$1" value
    [[ -n "${!var:-}" || ! -f .env ]] && return 0
    value="$(grep -E "^${var}=" .env | tail -n1 | cut -d= -f2- || true)"
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    if [[ -n "$value" ]]; then export "$var=$value"; fi
    return 0
}

# COMPOSE is an array: the plugin (`docker compose`), else legacy `docker-compose`, else empty.
detect_compose() {
    COMPOSE=()
    if docker compose version >/dev/null 2>&1; then
        COMPOSE=(docker compose)
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE=(docker-compose)
    fi
}

# Prints the id/name of the running db container, or exits with a clear message.
db_container() {
    if [[ -n "${DB_CONTAINER:-}" ]]; then
        echo "$DB_CONTAINER"
        return 0
    fi
    if ((${#COMPOSE[@]} == 0)); then
        echo "neither 'docker compose' nor 'docker-compose' works here; set DB_CONTAINER=<postgres container name> in .env" >&2
        return 1
    fi
    local cid
    cid="$("${COMPOSE[@]}" ps -q db 2>/dev/null | head -n1 || true)"
    if [[ -z "$cid" ]]; then
        echo "the 'db' container is not running (try: ${COMPOSE[*]} up -d db), or set DB_CONTAINER in .env" >&2
        return 1
    fi
    echo "$cid"
}

bot_stop() {
    if [[ -n "${BOT_CONTAINER:-}" ]]; then
        docker stop "$BOT_CONTAINER" >/dev/null
    elif ((${#COMPOSE[@]})); then
        "${COMPOSE[@]}" stop bot
    else
        echo "cannot stop the bot: no compose CLI found; set BOT_CONTAINER=<bot container name> in .env" >&2
        return 1
    fi
}

bot_start() {
    if [[ -n "${BOT_CONTAINER:-}" ]]; then
        docker start "$BOT_CONTAINER" >/dev/null
    elif ((${#COMPOSE[@]})); then
        "${COMPOSE[@]}" start bot
    else
        return 1
    fi
}

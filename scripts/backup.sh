#!/usr/bin/env bash
# Dump the database from the running compose "db" service to a compressed file.
#   ./scripts/backup.sh
# Cron example (daily 03:15):
#   15 3 * * * cd /path/to/dating-bot-v2 && ./scripts/backup.sh >> backups/backup.log 2>&1
# Env (from .env or the shell): POSTGRES_USER, POSTGRES_DB, BACKUP_DIR (default ./backups),
# BACKUP_KEEP_DAYS (default 14). Backups should be copied OFF this server too.
set -euo pipefail

cd "$(dirname "$0")/.."

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
for var in POSTGRES_USER POSTGRES_DB BACKUP_DIR BACKUP_KEEP_DAYS; do load_env_var "$var"; done

: "${POSTGRES_USER:?POSTGRES_USER is not set (.env or environment)}"
: "${POSTGRES_DB:?POSTGRES_DB is not set (.env or environment)}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
[[ "$BACKUP_KEEP_DAYS" =~ ^[0-9]+$ ]] || { echo "BACKUP_KEEP_DAYS must be a number" >&2; exit 2; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$BACKUP_DIR/${POSTGRES_DB}_${stamp}.sql.gz"
tmp="$final.partial"
trap 'rm -f "$tmp"' EXIT

# --clean/--if-exists make the dump self-contained so restore.sh can replay it onto a live db.
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --clean --if-exists --no-owner --no-privileges | gzip -9 > "$tmp"

# Verify before publishing: valid gzip and non-trivial SQL.
gzip -t "$tmp"
if ! gzip -dc "$tmp" | grep -q "PostgreSQL database dump"; then
    echo "backup looks empty/invalid, keeping nothing" >&2
    exit 1
fi
chmod 600 "$tmp"
mv "$tmp" "$final"
trap - EXIT

# Rotation: delete our own backups older than N days.
find "$BACKUP_DIR" -maxdepth 1 -type f -name "${POSTGRES_DB}_*.sql.gz" -mtime "+$BACKUP_KEEP_DAYS" -delete

echo "backup written: $final ($(du -h "$final" | cut -f1))"

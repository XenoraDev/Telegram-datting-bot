#!/usr/bin/env bash
# Restore a backup made by backup.sh into the running compose "db" service.
#   ./scripts/restore.sh backups/dating_bot_20250101T031500Z.sql.gz --yes
# DESTRUCTIVE: replaces the current contents of the public schema. The bot is stopped during the
# restore and started again afterwards. Requires the explicit --yes flag.
set -euo pipefail

cd "$(dirname "$0")/.."

file="${1:-}"
confirm="${2:-}"
if [[ -z "$file" || "$confirm" != "--yes" ]]; then
    echo "usage: $0 <backup.sql.gz> --yes   (this OVERWRITES the current database)" >&2
    exit 2
fi
[[ -f "$file" ]] || { echo "file not found: $file" >&2; exit 2; }
gzip -t "$file" || { echo "not a valid gzip file: $file" >&2; exit 2; }

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
for var in POSTGRES_USER POSTGRES_DB; do load_env_var "$var"; done
: "${POSTGRES_USER:?POSTGRES_USER is not set (.env or environment)}"
: "${POSTGRES_DB:?POSTGRES_DB is not set (.env or environment)}"

echo "stopping bot..."
docker compose stop bot
trap 'echo "restore FAILED: the transaction was rolled back, database unchanged. The bot is still stopped; start it with: docker compose start bot" >&2' ERR

echo "restoring $file into $POSTGRES_DB..."
# Wipe the public schema first (inside the same transaction as the replay) so tables that are
# NOT in the backup do not survive; if anything fails, the transaction rolls back untouched.
{
    echo "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    gzip -dc "$file"
} | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v ON_ERROR_STOP=1 --single-transaction -q -o /dev/null

echo "starting bot..."
docker compose start bot
echo "restore complete"

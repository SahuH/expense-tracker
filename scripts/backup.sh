#!/usr/bin/env bash
# Weekly Postgres backup — add to cron:
#   0 2 * * 0 /home/ubuntu/expense-tracker-v2/scripts/backup.sh >> /home/ubuntu/backups/backup.log 2>&1

set -euo pipefail

BACKUP_DIR="${HOME}/backups"
DATE=$(date +%Y%m%d)
FILENAME="firefly_${DATE}.sql.gz"

# Load .env if present
ENV_FILE="$(dirname "$0")/../.env"
if [[ -f "$ENV_FILE" ]]; then
    set -o allexport
    source "$ENV_FILE"
    set +o allexport
fi

POSTGRES_USER="${POSTGRES_USER:-firefly}"
POSTGRES_DB="${POSTGRES_DB:-firefly}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
CONTAINER_NAME="${COMPOSE_PROJECT_NAME:-expense-tracker-v2}-postgres-1"

mkdir -p "$BACKUP_DIR"

echo "[$(date -u +%FT%TZ)] Starting backup: $FILENAME"

docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER_NAME" \
    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
    | gzip > "${BACKUP_DIR}/${FILENAME}"

echo "[$(date -u +%FT%TZ)] Backup complete: ${BACKUP_DIR}/${FILENAME} ($(du -sh "${BACKUP_DIR}/${FILENAME}" | cut -f1))"

# Retain last 12 backups
ls -1t "${BACKUP_DIR}"/firefly_*.sql.gz | tail -n +13 | xargs -r rm --
echo "[$(date -u +%FT%TZ)] Pruned old backups. Kept: $(ls "${BACKUP_DIR}"/firefly_*.sql.gz | wc -l)"

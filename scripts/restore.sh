#!/usr/bin/env bash
# Restore Postgres from a gzipped backup file.
# Usage: ./scripts/restore.sh ~/backups/firefly_20240601.sql.gz

set -euo pipefail

BACKUP_FILE="${1:-}"
if [[ -z "$BACKUP_FILE" || ! -f "$BACKUP_FILE" ]]; then
    echo "Usage: $0 <path/to/firefly_YYYYMMDD.sql.gz>"
    exit 1
fi

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

echo "WARNING: This will DROP and recreate the '${POSTGRES_DB}' database."
read -rp "Type 'yes' to continue: " CONFIRM
[[ "$CONFIRM" == "yes" ]] || { echo "Aborted."; exit 0; }

echo "[$(date -u +%FT%TZ)] Dropping database..."
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER_NAME" \
    psql -U "$POSTGRES_USER" -c "DROP DATABASE IF EXISTS ${POSTGRES_DB};"
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER_NAME" \
    psql -U "$POSTGRES_USER" -c "CREATE DATABASE ${POSTGRES_DB} OWNER ${POSTGRES_USER};"

echo "[$(date -u +%FT%TZ)] Restoring from ${BACKUP_FILE}..."
gunzip -c "$BACKUP_FILE" | docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER_NAME" \
    psql -U "$POSTGRES_USER" "$POSTGRES_DB"

echo "[$(date -u +%FT%TZ)] Restore complete."

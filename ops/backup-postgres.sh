#!/usr/bin/env sh
set -eu

OUTPUT_DIR="${OUTPUT_DIR:-./backups}"
DB_NAME="${DB_NAME:-fras}"
DB_USER="${POSTGRES_USER:?POSTGRES_USER is required}"

mkdir -p "$OUTPUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILE="$OUTPUT_DIR/$DB_NAME-$STAMP.dump"

docker exec fras_db pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$BACKUP_FILE"
echo "Backup written to $BACKUP_FILE"

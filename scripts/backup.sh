#!/bin/bash
# =============================================================================
# Consistent backup of the database and the application files.
# =============================================================================
# Run inside the application container:
#
#   docker compose exec app /app/scripts/backup.sh
#
# Produces, in /app/backups (the "app-backups" volume):
#   bmshop-<timestamp>-db.sql.gz     consistent MySQL dump (gzip)
#   bmshop-<timestamp>-files.tar.gz  /app/instance (generated documents, ...)
#   bmshop-<timestamp>.sha256        checksums, for verifying a restore
#   bmshop-<timestamp>.manifest.txt  what, when and how to restore
#
# The dump is taken with --single-transaction so the shop can keep selling
# while it runs.  Nothing here deletes data except the retention pruning of
# old backup files themselves (BACKUP_RETENTION_DAYS, default 30).
# =============================================================================
set -euo pipefail

log()  { printf '[backup] %s\n' "$*"; }
fail() { printf '[backup] ERROR: %s\n' "$*" >&2; exit 1; }

BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
DB_HOST="${DB_HOST:-db}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
BASE="bmshop-${STAMP}"

: "${MYSQL_USER:?MYSQL_USER is not set}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD is not set}"
: "${MYSQL_DATABASE:?MYSQL_DATABASE is not set}"

mkdir -p "$BACKUP_DIR"
db_file="${BACKUP_DIR}/${BASE}-db.sql.gz"
files_file="${BACKUP_DIR}/${BASE}-files.tar.gz"
manifest="${BACKUP_DIR}/${BASE}.manifest.txt"

log "Dumping database '${MYSQL_DATABASE}' ..."
# --skip-ssl: see the note in scripts/docker-init.sh.  The MariaDB client in
# this image cannot verify the MySQL 8 self-signed CA; the traffic never leaves
# the private Docker network.
if ! mysqldump --skip-ssl --host="$DB_HOST" --user="$MYSQL_USER" --password="$MYSQL_PASSWORD" \
        --single-transaction --quick --no-tablespaces --hex-blob \
        --default-character-set=utf8mb4 \
        "$MYSQL_DATABASE" | gzip -9 > "$db_file"; then
    rm -f "$db_file"
    fail "mysqldump failed - no backup was written"
fi

log "Archiving application files (instance/) ..."
tar -czf "$files_file" -C /app instance

log "Writing checksums and manifest ..."
( cd "$BACKUP_DIR" && sha256sum "$(basename "$db_file")" "$(basename "$files_file")" \
    > "${BASE}.sha256" )

cat > "$manifest" <<EOF
Building Materials Shop Management System - backup
==================================================
Created (UTC)   : $(date -u '+%Y-%m-%d %H:%M:%S')
Database        : ${MYSQL_DATABASE}
Database host   : ${DB_HOST}
Application env : ${APP_ENV:-unknown}
Files           : $(basename "$db_file"), $(basename "$files_file")
Checksums       : $(basename "$BASE").sha256

Restore (see README, "Backup and restore"):
  1. docker compose stop app
  2. docker compose run --rm --no-deps app /app/scripts/restore.sh ${BASE}
  3. docker compose up -d app
EOF

log "Pruning backups older than ${RETENTION_DAYS} days ..."
find "$BACKUP_DIR" -maxdepth 1 -name 'bmshop-*' -type f -mtime "+${RETENTION_DAYS}" -delete

log "Backup complete:"
ls -lh "$BACKUP_DIR" | grep -v '^total' | sed 's/^/[backup]   /'

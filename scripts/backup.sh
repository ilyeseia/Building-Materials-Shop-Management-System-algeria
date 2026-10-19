#!/bin/bash
# =============================================================================
# Consistent backup of the database and the application files.
# =============================================================================
# Run inside the application container:
#
#   docker compose exec -T app /app/scripts/backup.sh
#
# Produces, in /app/backups (the "app-backups" volume):
#   bmshop-<timestamp>-db.sql.gz     consistent MySQL dump (gzip)
#   bmshop-<timestamp>-files.tar.gz  /app/instance (generated documents, ...)
#   bmshop-<timestamp>.sha256        checksums, for verifying a restore
#   bmshop-<timestamp>.manifest.txt  what, when and how to restore
#   latest.json                      last successful backup (for the status page)
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
SUFFIX="${BACKUP_SUFFIX:-}"
case "$SUFFIX" in
    *[!A-Za-z0-9_-]*) printf '[backup] ERROR: invalid BACKUP_SUFFIX\n' >&2; exit 1 ;;
esac
BASE="bmshop-${STAMP}${SUFFIX:+-${SUFFIX}}"

: "${MYSQL_USER:?MYSQL_USER is not set}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD is not set}"
: "${MYSQL_DATABASE:?MYSQL_DATABASE is not set}"

command -v mysqldump >/dev/null 2>&1 || fail "mysqldump is not installed"
command -v mysql >/dev/null 2>&1 || fail "mysql client is not installed"
command -v gzip >/dev/null 2>&1 || fail "gzip is not installed"
command -v tar >/dev/null 2>&1 || fail "tar is not installed"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is not installed"

mkdir -p "$BACKUP_DIR"
db_file="${BACKUP_DIR}/${BASE}-db.sql.gz"
files_file="${BACKUP_DIR}/${BASE}-files.tar.gz"
manifest="${BACKUP_DIR}/${BASE}.manifest.txt"
sha_file="${BACKUP_DIR}/${BASE}.sha256"
db_tmp="${db_file}.part"
files_tmp="${files_file}.part"

cleanup() {
    rm -f "$db_tmp" "$files_tmp"
}
trap cleanup EXIT

log "Checking free space in ${BACKUP_DIR} ..."
available_kb="$(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $4}')"
[ -n "$available_kb" ] || fail "could not determine free space"
if [ "$available_kb" -lt 524288 ]; then
    fail "less than 512 MiB free in ${BACKUP_DIR}; refusing to start a backup"
fi

log "Dumping database '${MYSQL_DATABASE}' ..."
# --skip-ssl: see the note in scripts/docker-init.sh.  The MariaDB client in
# this image cannot verify the MySQL 8 self-signed CA; the traffic never leaves
# the private Docker network.
if ! mysqldump --skip-ssl --host="$DB_HOST" --user="$MYSQL_USER" --password="$MYSQL_PASSWORD" \
        --single-transaction --quick --no-tablespaces --hex-blob \
        --default-character-set=utf8mb4 \
        "$MYSQL_DATABASE" | gzip -9 > "$db_tmp"; then
    fail "mysqldump failed - no backup was written"
fi

# A truncated gzip or a dump without mysqldump's completion marker is not a
# backup.  Refuse to publish it as one.
gzip -t "$db_tmp" || fail "the dump is not a valid gzip file"
if ! gzip -cd "$db_tmp" | tail -n 5 | grep -q -- '-- Dump completed'; then
    fail "the dump has no completion marker - it is truncated"
fi
mv "$db_tmp" "$db_file"

log "Archiving application files (instance/) ..."
tar -czf "$files_tmp" -C /app instance
tar -tzf "$files_tmp" >/dev/null || fail "the files archive is not readable"
mv "$files_tmp" "$files_file"

log "Writing checksums and manifest ..."
( cd "$BACKUP_DIR" && sha256sum "$(basename "$db_file")" "$(basename "$files_file")" \
    > "$sha_file" )

schema_revision="$(
    mysql --skip-ssl --host="$DB_HOST" --user="$MYSQL_USER" --password="$MYSQL_PASSWORD" \
        --skip-column-names --batch "$MYSQL_DATABASE" \
        -e 'SELECT version_num FROM alembic_version LIMIT 1;' 2>/dev/null || true
)"
table_count="$(
    mysql --skip-ssl --host="$DB_HOST" --user="$MYSQL_USER" --password="$MYSQL_PASSWORD" \
        --skip-column-names --batch "$MYSQL_DATABASE" \
        -e 'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE();' \
        2>/dev/null || true
)"
db_bytes="$(stat -c %s "$db_file" 2>/dev/null || wc -c < "$db_file")"
files_bytes="$(stat -c %s "$files_file" 2>/dev/null || wc -c < "$files_file")"
created_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

cat > "$manifest" <<EOF
Building Materials Shop Management System - backup
==================================================
Base name       : ${BASE}
Created (UTC)   : ${created_at}
Database        : ${MYSQL_DATABASE}
Database host   : ${DB_HOST}
Application env : ${APP_ENV:-unknown}
Schema revision : ${schema_revision:-unknown}
Tables          : ${table_count:-unknown}
Files           : $(basename "$db_file"), $(basename "$files_file")
Checksums       : $(basename "$sha_file")

Restore (see README, "Backup and restore"):
  1. docker compose stop app
  2. docker compose run --rm --no-deps app /app/scripts/restore.sh ${BASE}
  3. docker compose up -d app

Verify without touching production (from the project directory):
  ./scripts/verify-backup.sh ${BASE}
EOF

# Machine-readable status for the administrator dashboard.  Written last and
# atomically so a half-finished run can never look successful.
latest_tmp="${BACKUP_DIR}/latest.json.part"
cat > "$latest_tmp" <<EOF
{
  "base": "${BASE}",
  "created_at": "${created_at}",
  "database": "${MYSQL_DATABASE}",
  "schema_revision": "${schema_revision:-}",
  "table_count": "${table_count:-}",
  "db_bytes": ${db_bytes:-0},
  "files_bytes": ${files_bytes:-0},
  "status": "complete"
}
EOF
mv "$latest_tmp" "${BACKUP_DIR}/latest.json"

log "Pruning backups older than ${RETENTION_DAYS} days ..."
find "$BACKUP_DIR" -maxdepth 1 -name 'bmshop-*' -type f -mtime "+${RETENTION_DAYS}" -delete

log "Backup complete:"
ls -lh "$BACKUP_DIR" | grep -v '^total' | sed 's/^/[backup]   /'
printf 'BACKUP_BASE=%s\n' "$BASE"

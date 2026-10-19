#!/bin/bash
# =============================================================================
# Restore a backup created by scripts/backup.sh.
# =============================================================================
# DESTRUCTIVE: the dump drops and recreates the tables of the live database.
# The application must not be writing while it runs.
#
# Recommended sequence (from the project directory on the host):
#
#   docker compose stop app
#   docker compose run --rm --no-deps app /app/scripts/restore.sh <backup>
#   docker compose up -d app
#
# With no argument the script lists the available backups and exits.
# Requires typing YES, then RESTORE, to proceed.
# =============================================================================
set -euo pipefail

log()  { printf '[restore] %s\n' "$*"; }
fail() { printf '[restore] ERROR: %s\n' "$*" >&2; exit 1; }

BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
DB_HOST="${DB_HOST:-db}"

: "${MYSQL_USER:?MYSQL_USER is not set}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD is not set}"
: "${MYSQL_DATABASE:?MYSQL_DATABASE is not set}"

mysql_run() {
    # --skip-ssl: see the note in scripts/docker-init.sh (MariaDB client vs the
    # self-signed CA of MySQL 8, inside the private Docker network only).
    mysql --skip-ssl --host="$DB_HOST" --user="$MYSQL_USER" --password="$MYSQL_PASSWORD" "$@"
}

list_backups() {
    echo "Available backups in ${BACKUP_DIR}:"
    local found=0
    for manifest in $(ls -1t "$BACKUP_DIR"/*.manifest.txt 2>/dev/null); do
        found=1
        printf '  %s\n' "$(basename "$manifest" .manifest.txt)"
        sed 's/^/      /' "$manifest" | head -6
    done
    [ "$found" = 1 ] || echo "  (none)"
}

NAME="${1:-}"
if [ -z "$NAME" ]; then
    list_backups
    echo
    echo "Usage: $0 <backup-name>        (e.g. bmshop-20260101-120000)"
    exit 0
fi

# Accept the base name, the full dump file name, or a path inside BACKUP_DIR.
case "$NAME" in
    */)   fail "expected a backup name, not a directory" ;;
    *.sql.gz) db_file="$NAME"; base="$(basename "$NAME" -db.sql.gz)" ;;
    *)    db_file="${BACKUP_DIR}/${NAME}-db.sql.gz"; base="$NAME" ;;
esac
[ -f "$db_file" ] || fail "no such backup file: $db_file (run without arguments to list backups)"

files_file="${BACKUP_DIR}/${base}-files.tar.gz"
sha_file="${BACKUP_DIR}/${base}.sha256"

# --- Integrity check before touching anything -----------------------------
if [ -f "$sha_file" ]; then
    log "Verifying checksums ..."
    if ! ( cd "$BACKUP_DIR" && sha256sum --check "$(basename "$sha_file")" --status ); then
        fail "checksum verification failed - the backup may be corrupt"
    fi
    log "Checksums are valid."
else
    log "No .sha256 file for this backup - skipping integrity check."
fi

# --- Explicit confirmation ------------------------------------------------
cat <<EOF

  ============================================================
   DESTRUCTIVE OPERATION
  ============================================================
   Backup      : ${base}
   Database    : ${MYSQL_DATABASE} @ ${DB_HOST}
   Effect      : every table in the dump is dropped and recreated
                 from the backup.  Current contents are lost.
  ============================================================

EOF
read -r -p "Type YES to continue: " answer
[ "$answer" = "YES" ] || { log "Cancelled."; exit 0; }
read -r -p "Type RESTORE to confirm: " answer
[ "$answer" = "RESTORE" ] || { log "Cancelled."; exit 0; }

# --- Restore --------------------------------------------------------------
log "Restoring ${base} ..."
before_tables="$(mysql_run --skip-column-names --execute 'SHOW TABLES;' "$MYSQL_DATABASE" | wc -l)"
log "Tables before restore: ${before_tables}"

if ! gunzip -c "$db_file" | mysql_run "$MYSQL_DATABASE"; then
    fail "restore failed - the database may be in an inconsistent state"
fi

after_tables="$(mysql_run --skip-column-names --execute 'SHOW TABLES;' "$MYSQL_DATABASE" | wc -l)"
log "Tables after restore : ${after_tables}"

if [ -f "$files_file" ]; then
    log "Restoring application files (instance/) ..."
    tar -xzf "$files_file" -C /app
fi

cat <<EOF

[restore] Completed: ${base}

Next steps:
  1. Start (or restart) the application:  docker compose up -d app
  2. Sign in and check today's documents.
  3. If anything looks wrong, restore the previous backup the same way.
EOF

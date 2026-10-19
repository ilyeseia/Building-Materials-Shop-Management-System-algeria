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
#
# Safety design:
#   * the checksum file is mandatory (override only with
#     ALLOW_UNVERIFIED_BACKUP=1 for an emergency);
#   * a fresh backup of the *current* database and files is taken immediately
#     before the restore, and the restore aborts if that backup fails;
#   * the database is checked with mysqlcheck and the dump table count is
#     compared with the restored table count;
#   * a restore report is written to the backup volume.
# =============================================================================
set -euo pipefail

log()  { printf '[restore] %s\n' "$*"; }
fail() { printf '[restore] ERROR: %s\n' "$*" >&2; exit 1; }

BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
DB_HOST="${DB_HOST:-db}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
REPORT="${BACKUP_DIR}/restore-${STAMP}.report.txt"

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
manifest="${BACKUP_DIR}/${base}.manifest.txt"

# --- Integrity check before touching anything -----------------------------
if [ -f "$sha_file" ]; then
    log "Verifying checksums ..."
    if ! ( cd "$BACKUP_DIR" && sha256sum --check "$(basename "$sha_file")" --status ); then
        fail "checksum verification failed - the backup may be corrupt"
    fi
    log "Checksums are valid."
elif [ "${ALLOW_UNVERIFIED_BACKUP:-0}" = "1" ]; then
    log "WARNING: no .sha256 file; continuing because ALLOW_UNVERIFIED_BACKUP=1."
else
    fail "no .sha256 file for this backup; refusing an unverified restore (set ALLOW_UNVERIFIED_BACKUP=1 only for an emergency)"
fi

if [ -f "$files_file" ] && ! tar -tzf "$files_file" >/dev/null; then
    fail "the application files archive is not readable: $files_file"
fi

# --- Explicit confirmation ------------------------------------------------
cat <<EOF

  ============================================================
   DESTRUCTIVE OPERATION
  ============================================================
   Backup      : ${base}
   Database    : ${MYSQL_DATABASE} @ ${DB_HOST}
   Effect      : every table in the dump is dropped and recreated
                 from the backup.  Current contents are replaced.
   Safety copy : a fresh backup of the current state will be taken
                 before anything is overwritten.
  ============================================================

EOF
read -r -p "Type YES to continue: " answer
[ "$answer" = "YES" ] || { log "Cancelled."; exit 0; }
read -r -p "Type RESTORE to confirm: " answer
[ "$answer" = "RESTORE" ] || { log "Cancelled."; exit 0; }

# --- Safety backup of the current state -----------------------------------
log "Creating a safety backup of the current database and files ..."
if ! safety_output="$(BACKUP_SUFFIX=pre-restore /app/scripts/backup.sh)"; then
    fail "the pre-restore safety backup failed; the database was NOT changed"
fi
printf '%s\n' "$safety_output"
safety_base="$(printf '%s\n' "$safety_output" | sed -n 's/^BACKUP_BASE=//p' | tail -n 1)"
[ -n "$safety_base" ] || fail "could not determine the safety backup name; the database was NOT changed"
log "Safety backup is ${safety_base}."

# --- Restore --------------------------------------------------------------
log "Restoring ${base} ..."
before_tables="$(mysql_run --skip-column-names --execute 'SHOW TABLES;' "$MYSQL_DATABASE" | wc -l)"
log "Tables before restore: ${before_tables}"

dump_tables="$(gzip -cd "$db_file" | grep -c '^CREATE TABLE' || true)"
[ -n "$dump_tables" ] || dump_tables=0
log "Tables in the dump    : ${dump_tables}"

if ! gunzip -c "$db_file" | mysql_run "$MYSQL_DATABASE"; then
    cat > "$REPORT" <<EOF
Restore FAILED
==============
Backup        : ${base}
Started (UTC) : $(date -u '+%Y-%m-%d %H:%M:%S')
Safety backup : ${safety_base}
Result        : mysql import failed; the database may be inconsistent.
Next step     : restore ${safety_base} with this script, then investigate.
EOF
    fail "restore failed - the database may be in an inconsistent state; safety backup: ${safety_base}"
fi

after_tables="$(mysql_run --skip-column-names --execute 'SHOW TABLES;' "$MYSQL_DATABASE" | wc -l)"
log "Tables after restore : ${after_tables}"

[ "$after_tables" -gt 0 ] || fail "restore produced an empty database; safety backup: ${safety_base}"
if [ "$dump_tables" -gt 0 ] && [ "$after_tables" -ne "$dump_tables" ]; then
    fail "table count mismatch (dump ${dump_tables}, restored ${after_tables}); safety backup: ${safety_base}"
fi

log "Checking restored tables (mysqlcheck) ..."
if ! mysqlcheck --skip-ssl --host="$DB_HOST" --user="$MYSQL_USER" --password="$MYSQL_PASSWORD" \
        --check "$MYSQL_DATABASE" >/tmp/bmshop-restore-check.log 2>&1; then
    cat /tmp/bmshop-restore-check.log
    fail "mysqlcheck reported an error; safety backup: ${safety_base}"
fi

revision="$(mysql_run --skip-column-names --execute 'SELECT version_num FROM alembic_version LIMIT 1;' "$MYSQL_DATABASE" 2>/dev/null || true)"
[ -n "$revision" ] || fail "alembic_version is missing after restore; safety backup: ${safety_base}"

negative_stock="$(mysql_run --skip-column-names --execute \
    'SELECT COUNT(*) FROM stock_levels WHERE quantity < 0 OR reserved < 0 OR reserved > quantity;' \
    "$MYSQL_DATABASE" 2>/dev/null || echo 'unknown')"
log "Schema revision      : ${revision}"
log "Negative stock rows  : ${negative_stock}"

if [ -f "$files_file" ] && [ "${RESTORE_FILES:-1}" = "1" ]; then
    log "Restoring application files (instance/) ..."
    tar -xzf "$files_file" -C /app
elif [ -f "$files_file" ]; then
    log "Skipping application files because RESTORE_FILES=0 (database-only drill)."
fi

cat > "$REPORT" <<EOF
Restore completed
=================
Backup          : ${base}
Finished (UTC)  : $(date -u '+%Y-%m-%d %H:%M:%S')
Schema revision : ${revision}
Tables before   : ${before_tables}
Tables restored : ${after_tables}
Negative stock  : ${negative_stock}
Safety backup   : ${safety_base}
Checks          : checksum, gzip, table count, mysqlcheck, alembic_version
EOF

cat <<EOF

[restore] Completed: ${base}
[restore] Report   : ${REPORT}
[restore] Safety   : ${safety_base}

Next steps:
  1. Start (or restart) the application:  docker compose up -d app
  2. Sign in and check today's documents.
  3. If anything looks wrong, restore the safety backup:
       docker compose stop app
       docker compose run --rm --no-deps app /app/scripts/restore.sh ${safety_base}
       docker compose up -d app
EOF

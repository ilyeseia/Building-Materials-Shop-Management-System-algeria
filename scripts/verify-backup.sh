#!/bin/bash
# =============================================================================
# Verify a backup by restoring it into an isolated scratch database.
# =============================================================================
# Run from the project directory on the host:
#
#   ./scripts/verify-backup.sh                  # latest backup
#   ./scripts/verify-backup.sh <backup-base>    # specific backup
#
# This script NEVER writes to the live database.  It creates a temporary
# database named bmshop_verify_<timestamp>, imports the dump, runs structural
# and integrity checks, prints a report and drops the scratch database again.
#
# The application may keep running.  Because the live shop can receive writes
# while the dump is imported, this script does not compare live row counts with
# restored row counts; it checks that the dump is complete, restorable and
# internally consistent.  For a byte-for-byte data comparison, stop the app
# during the drill.
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="${COMPOSE:-docker compose}"
NAME="${1:-}"

log()  { printf '[verify] %s\n' "$*"; }
fail() { printf '[verify] ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "docker is not installed"

if [ -z "$NAME" ]; then
    manifest="$($COMPOSE exec -T app sh -c 'ls -1t /app/backups/*.manifest.txt 2>/dev/null | head -1' | tr -d '\r')"
    [ -n "$manifest" ] || fail "no backups found in /app/backups"
    NAME="$(basename "$manifest" .manifest.txt)"
    log "Using latest backup: ${NAME}"
fi

case "$NAME" in
    *[!A-Za-z0-9._-]*) fail "invalid backup name: ${NAME}" ;;
esac

SOURCE_DB="$($COMPOSE exec -T db printenv MYSQL_DATABASE | tr -d '\r')"
case "$SOURCE_DB" in
    ''|*[!A-Za-z0-9_]*) fail "refusing to use an unexpected database name" ;;
esac

SCRATCH="bmshop_verify_$(date -u +%Y%m%d%H%M%S)_$$"
cleanup() {
    $COMPOSE exec -T db sh -c "MYSQL_PWD=\"\$MYSQL_ROOT_PASSWORD\" mysql --ssl-mode=DISABLED -uroot -e 'DROP DATABASE IF EXISTS ${SCRATCH};'" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- Backup integrity -----------------------------------------------------
log "Verifying checksum of ${NAME} ..."
$COMPOSE exec -T app sh -c "cd /app/backups && sha256sum --check '${NAME}.sha256' --status" \
    || fail "checksum verification failed"

dump_tables="$($COMPOSE exec -T app sh -c "gzip -cd '/app/backups/${NAME}-db.sql.gz' | grep -c '^CREATE TABLE'" | tr -d '\r' || true)"
[ -n "$dump_tables" ] || dump_tables=0
[ "$dump_tables" -gt 0 ] || fail "the dump contains no CREATE TABLE statements"

# --- Create an isolated database and import -------------------------------
log "Creating isolated database ${SCRATCH} ..."
$COMPOSE exec -T db sh -c "MYSQL_PWD=\"\$MYSQL_ROOT_PASSWORD\" mysql --ssl-mode=DISABLED -uroot -e 'CREATE DATABASE ${SCRATCH} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;'" \
    || fail "could not create the scratch database"

log "Importing ${NAME} into ${SCRATCH} ..."
if ! $COMPOSE exec -T app sh -c "gunzip -c '/app/backups/${NAME}-db.sql.gz'" \
        | $COMPOSE exec -T db sh -c "MYSQL_PWD=\"\$MYSQL_ROOT_PASSWORD\" mysql --ssl-mode=DISABLED -uroot '${SCRATCH}'"; then
    fail "the dump could not be imported"
fi

# --- Checks ---------------------------------------------------------------
db_query() {
    local database="$1" sql="$2"
    $COMPOSE exec -T db sh -c "MYSQL_PWD=\"\$MYSQL_ROOT_PASSWORD\" mysql --ssl-mode=DISABLED -uroot --batch --skip-column-names '${database}' -e \"${sql}\""
}

restored_tables="$(db_query "$SCRATCH" "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '${SCRATCH}';" | tr -d '\r')"
log "Tables in dump / restored: ${dump_tables} / ${restored_tables}"
[ "$restored_tables" -eq "$dump_tables" ] || fail "table count mismatch"

revision="$(db_query "$SCRATCH" 'SELECT version_num FROM alembic_version LIMIT 1;' | tr -d '\r')"
[ -n "$revision" ] || fail "alembic_version is missing from the restored database"
log "Schema revision          : ${revision}"

log "Checking restored tables (CHECK TABLE) ..."
table_list="$(db_query "$SCRATCH" "SELECT GROUP_CONCAT(table_name ORDER BY table_name SEPARATOR ', ') FROM information_schema.tables WHERE table_schema = '${SCRATCH}';" | tr -d '\r')"
check_output="$(db_query "$SCRATCH" "CHECK TABLE ${table_list};")"
if ! printf '%s\n' "$check_output" | awk -F'\t' 'NF >= 4 && ($3 != "status" || $4 != "OK") { print; bad=1 } END { exit bad }'; then
    fail "CHECK TABLE reported an error; the restored database is not usable"
fi
log "All restored tables passed CHECK TABLE."

negative_stock="$(db_query "$SCRATCH" 'SELECT COUNT(*) FROM stock_levels WHERE quantity < 0 OR reserved < 0 OR reserved > quantity;' | tr -d '\r')"
[ "$negative_stock" = "0" ] || fail "restored stock contains negative or over-reserved quantities"

orphan_levels="$(db_query "$SCRATCH" 'SELECT COUNT(*) FROM stock_levels sl LEFT JOIN products p ON p.id = sl.product_id LEFT JOIN warehouses w ON w.id = sl.warehouse_id WHERE p.id IS NULL OR w.id IS NULL;' | tr -d '\r')"
[ "$orphan_levels" = "0" ] || fail "restored stock_levels contain orphan rows"

overallocated="$(db_query "$SCRATCH" 'SELECT COUNT(*) FROM (SELECT a.payment_id, SUM(a.amount) AS allocated FROM payment_allocations a GROUP BY a.payment_id) x JOIN payments p ON p.id = x.payment_id WHERE x.allocated > p.amount + 0.001;' | tr -d '\r')"
[ "$overallocated" = "0" ] || fail "restored payments contain overallocated rows"

fk_count="$(db_query "$SCRATCH" "SELECT COUNT(*) FROM information_schema.referential_constraints WHERE constraint_schema = '${SCRATCH}';" | tr -d '\r')"
[ "$fk_count" -gt 0 ] || fail "restored database has no foreign keys"
log "Foreign keys             : ${fk_count}"

printf '%-24s %s\n' "Table" "Rows"
for table in products categories stock_levels stock_movements customers suppliers sales_orders sales_invoices supplier_invoices payments payment_allocations audit_events users; do
    count="$(db_query "$SCRATCH" "SELECT COUNT(*) FROM ${table};" | tr -d '\r')"
    printf '%-24s %s\n' "$table" "$count"
done

cat <<EOF

[verify] PASS
[verify] Backup        : ${NAME}
[verify] Scratch DB    : ${SCRATCH} (dropped automatically)
[verify] Schema        : ${revision}
[verify] Checks        : checksum, gzip, table count, CHECK TABLE, foreign keys,
                         stock invariants, payment allocations, key row counts
[verify] The live database was not modified.
EOF

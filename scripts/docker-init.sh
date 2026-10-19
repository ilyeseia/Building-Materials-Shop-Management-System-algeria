#!/bin/bash
# =============================================================================
# First-run initialisation (idempotent, non-destructive).
# =============================================================================
# Run once inside the application container after `docker compose up -d`:
#
#   docker compose exec app /app/scripts/docker-init.sh
#
# What it does:
#   1. waits for the database to accept connections;
#   2. applies the versioned migrations (flask db upgrade);
#   3. creates any missing table and seeds reference data (flask init-db);
#   4. optionally creates the administrator from ADMIN_USERNAME/ADMIN_PASSWORD.
#
# What it never does: drop a table, delete a row or overwrite an existing user.
# Environment: SEED_DEMO_DATA=1 adds the demo catalogue; ADMIN_PASSWORD is
# optional (leaving it unset is safer - see the message at the end).
# =============================================================================
set -euo pipefail

log()  { printf '[init] %s\n' "$*"; }
fail() { printf '[init] ERROR: %s\n' "$*" >&2; exit 1; }

DB_HOST="${DB_HOST:-db}"
: "${MYSQL_USER:?MYSQL_USER is not set}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD is not set}"
: "${MYSQL_DATABASE:?MYSQL_DATABASE is not set}"

log "Waiting for the database at ${DB_HOST} ..."
# --skip-ssl is required because the client shipped in this image is MariaDB's
# and rejects the self-signed CA that MySQL 8 generates for itself.  The
# connection stays inside the private Docker network (the database publishes no
# port), and the application's own driver connects without TLS as well.
ready=0
for attempt in $(seq 1 60); do
    if mysqladmin --host="$DB_HOST" --user="$MYSQL_USER" \
            --password="$MYSQL_PASSWORD" --skip-ssl ping >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[ "$ready" = 1 ] || fail "database did not become ready within two minutes"
log "Database is accepting connections."

log "Applying migrations (flask db upgrade) ..."
flask db upgrade

log "Ensuring schema and reference data (flask init-db) ..."
if [ "${SEED_DEMO_DATA:-0}" = "1" ]; then
    flask init-db --demo
else
    flask init-db
fi

admin_user="${ADMIN_USERNAME:-admin}"
if [ -n "${ADMIN_PASSWORD:-}" ]; then
    if output="$(flask create-user --username "$admin_user" --role admin \
            --password "$ADMIN_PASSWORD" 2>&1)"; then
        log "Administrator '${admin_user}' created."
    else
        log "Administrator '${admin_user}' was not created:"
        printf '%s\n' "$output" | sed 's/^/[init]   /'
        log "(an existing account is never modified)"
    fi
else
    log "ADMIN_PASSWORD is not set, so no administrator was created."
    log "Create one interactively (recommended):"
    log "  docker compose exec app flask create-user --username admin --role admin"
fi

log "Initialisation finished."

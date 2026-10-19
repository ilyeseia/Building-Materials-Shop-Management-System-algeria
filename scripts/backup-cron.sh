#!/usr/bin/env bash
# =============================================================================
# Scheduled backup wrapper (run on the HOST, not inside the container)
# =============================================================================
# Runs scripts/backup.sh inside the app container, then the optional off-site
# copy and notification.  It never deletes the database and never runs a
# restore.
#
# Install a daily cron entry with:
#   ./scripts/install-backup-schedule.sh
#
# Environment (put in the host environment or the installed cron line):
#   BACKUP_REMOTE          rclone destination, e.g. "b2:bmshop-backups"
#   BACKUP_NOTIFY_WEBHOOK  optional JSON webhook (Slack-compatible {text: ...})
# =============================================================================
set -uo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$APP_DIR" || { printf '[backup-cron] ERROR: cannot cd to %s\n' "$APP_DIR" >&2; exit 1; }

# Optional host-only environment file for BACKUP_REMOTE and
# BACKUP_NOTIFY_WEBHOOK.  Keep it mode 600; do not commit it.
if [ -f "$APP_DIR/backup.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$APP_DIR/backup.env"
    set +a
fi

notify() {
    if [ -x "$APP_DIR/scripts/notify.sh" ]; then
        "$APP_DIR/scripts/notify.sh" "$1" "$2" || true
    fi
}

printf '[backup-cron] %s starting in %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$APP_DIR"
if docker compose exec -T app /app/scripts/backup.sh; then
    if ! "$APP_DIR/scripts/backup-offsite.sh"; then
        notify failure "Database backup succeeded but the off-site copy failed."
        printf '[backup-cron] ERROR: off-site copy failed\n' >&2
        exit 1
    fi
    if [ -n "${BACKUP_REMOTE:-}" ]; then
        notify success "Database backup and off-site copy completed."
    else
        notify success "Database backup completed. Off-site copy skipped: BACKUP_REMOTE is not configured."
    fi
    printf '[backup-cron] completed\n'
else
    notify failure "Database backup failed; run 'docker compose logs app' and inspect /app/backups/latest-failure.json."
    printf '[backup-cron] ERROR: backup failed\n' >&2
    exit 1
fi

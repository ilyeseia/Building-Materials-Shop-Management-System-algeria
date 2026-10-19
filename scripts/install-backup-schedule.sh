#!/usr/bin/env bash
# =============================================================================
# Install (or print) a daily backup cron entry for the current user.
# =============================================================================
#   ./scripts/install-backup-schedule.sh --print   # show the line, change nothing
#   ./scripts/install-backup-schedule.sh           # install / replace the entry
#
# Override the schedule or host settings:
#   BACKUP_CRON="30 1 * * *" ./scripts/install-backup-schedule.sh
#   BACKUP_REMOTE="remote:path" BACKUP_NOTIFY_WEBHOOK="https://..." ./scripts/...
#
# The cron entry runs scripts/backup-cron.sh on the host and appends its output
# to backups-cron.log.  For off-site/notification settings, create a mode-600
# backup.env in the project directory (see docs/DEPLOYMENT.md); cron does not
# inherit your interactive shell environment.
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
SCHEDULE="${BACKUP_CRON:-15 2 * * *}"
LOG_FILE="$APP_DIR/backups-cron.log"
MARKER="# bmshop-backup"

LINE="$SCHEDULE cd '$APP_DIR' && ./scripts/backup-cron.sh >> '$LOG_FILE' 2>&1 $MARKER"

if [ "${1:-}" = "--print" ]; then
    printf '%s\n' "$LINE"
    exit 0
fi

command -v crontab >/dev/null 2>&1 || {
    printf '[schedule] ERROR: crontab is not available. Add this line manually:\n%s\n' "$LINE" >&2
    exit 1
}

CURRENT="$(crontab -l 2>/dev/null || true)"
{
    if [ -n "$CURRENT" ]; then
        printf '%s\n' "$CURRENT" | grep -vF "$MARKER" || true
    fi
    printf '%s\n' "$LINE"
} | crontab -

printf '[schedule] installed for user %s:\n%s\n' "$(id -un)" "$LINE"
printf '[schedule] host prerequisites: docker compose, rclone (only for off-site), curl (only for notifications).\n'

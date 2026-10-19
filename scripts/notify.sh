#!/usr/bin/env bash
# =============================================================================
# Optional backup notification (Slack-compatible webhook)
# =============================================================================
# Usage: notify.sh success|failure "message"
# Configure BACKUP_NOTIFY_WEBHOOK on the host.  The webhook URL is never printed.
# Notification failure is non-fatal: the backup result remains authoritative.
# =============================================================================
set -uo pipefail

STATUS="${1:-unknown}"
MESSAGE="${2:-}"
WEBHOOK="${BACKUP_NOTIFY_WEBHOOK:-}"

if [ -z "$WEBHOOK" ]; then
    printf '[notify] skipped: BACKUP_NOTIFY_WEBHOOK is not configured\n'
    exit 0
fi
if ! command -v curl >/dev/null 2>&1; then
    printf '[notify] skipped: curl is not installed\n' >&2
    exit 0
fi

# Keep the payload single-line and JSON-safe without echoing any secret.
SAFE="$(printf '%s: %s' "$STATUS" "$MESSAGE" | tr '\n' ' ' | sed 's/\\/\\\\/g; s/"/\\"/g')"
if printf '{"text":"%s"}' "$SAFE" \
    | curl -fsS --max-time 10 -X POST \
        -H 'Content-Type: application/json' \
        --data-binary @- "$WEBHOOK" >/dev/null; then
    printf '[notify] sent\n'
else
    printf '[notify] WARNING: notification webhook failed (backup status is unchanged)\n' >&2
fi

#!/usr/bin/env bash
# =============================================================================
# Optional off-site copy of the newest backups
# =============================================================================
# The backups live inside the Docker volume, so this script copies them out with
# `docker compose cp` into a private temporary directory and uploads that
# directory with rclone.  It does NOT delete local backups.
#
# Configure on the host:
#   BACKUP_REMOTE="remote:path"     # rclone destination (required)
#   rclone config                    # one-time credential setup
#
# If BACKUP_REMOTE is empty the script reports "skipped" and exits 0, so a
# scheduled job keeps working while the destination is being procured.
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
REMOTE="${BACKUP_REMOTE:-}"

if [ -z "$REMOTE" ]; then
    printf '[offsite] skipped: BACKUP_REMOTE is not configured\n'
    exit 0
fi

command -v docker >/dev/null 2>&1 || { printf '[offsite] ERROR: docker is not installed\n' >&2; exit 1; }
command -v rclone >/dev/null 2>&1 || { printf '[offsite] ERROR: rclone is not installed\n' >&2; exit 1; }

umask 077
STAGING="$(mktemp -d)"
cleanup() { rm -rf "$STAGING"; }
trap cleanup EXIT

cd "$APP_DIR"
printf '[offsite] exporting backups from the app container ...\n'
docker compose cp app:/app/backups/. "$STAGING/"

printf '[offsite] uploading to %s ...\n' "$REMOTE"
rclone copy --checksum \
    --include 'bmshop-*' \
    --include 'latest.json' \
    --include 'latest-failure.json' \
    "$STAGING" "$REMOTE"

printf '[offsite] verifying remote objects ...\n'
rclone check --one-way \
    --include 'bmshop-*' \
    --include 'latest.json' \
    "$STAGING" "$REMOTE" >/dev/null

printf '[offsite] complete\n'

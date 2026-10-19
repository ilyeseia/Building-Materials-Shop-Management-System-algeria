#!/usr/bin/env bash
# =============================================================================
# Reproducible source release
# =============================================================================
# Usage:
#   ./scripts/release.sh              # uses the VERSION file
#   ./scripts/release.sh 1.2.0        # explicit version (must exist in VERSION)
#   RUN_TESTS=1 ./scripts/release.sh  # run the test suite first
#
# The release is a `git archive` of the current commit, so it never contains
# .env, instance data, backups, __pycache__ or any untracked file.  It writes:
#   dist/bmshop-<version>.tar.gz
#   dist/bmshop-<version>.tar.gz.sha256
#   dist/bmshop-<version>.RELEASE.json
#
# Build the Docker image from this commit with:
#   docker compose build \
#     --build-arg APP_VERSION=<version> \
#     --build-arg BUILD_COMMIT=$(git rev-parse --short HEAD) \
#     --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail() { printf '[release] ERROR: %s\n' "$*" >&2; exit 1; }
log()  { printf '[release] %s\n' "$*"; }

FILE_VERSION="$(tr -d '[:space:]' < VERSION)"
VERSION="${1:-$FILE_VERSION}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.]+)?$ ]] \
    || fail "invalid version '$VERSION' (expected e.g. 1.2.0)"
[ "$VERSION" = "$FILE_VERSION" ] \
    || fail "VERSION contains '$FILE_VERSION', not '$VERSION'; update it in the same commit"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "not a git checkout"
if ! git diff --quiet || ! git diff --cached --quiet; then
    fail "working tree has uncommitted changes; commit them before releasing"
fi
if [ -n "$(git ls-files --others --exclude-standard)" ]; then
    fail "untracked files present; add or remove them before releasing"
fi

if [ "${RUN_TESTS:-0}" = "1" ]; then
    log "Running the test suite ..."
    python -m pytest -q
fi

COMMIT="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"
CREATED="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
DIST="${RELEASE_DIR:-dist}"
PREFIX="bmshop-$VERSION"
ARCHIVE="$DIST/$PREFIX.tar.gz"

mkdir -p "$DIST"
log "Archiving $COMMIT as $ARCHIVE ..."
git archive --format=tar.gz --prefix="$PREFIX/" --output="$ARCHIVE" HEAD

CHECKSUM="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
printf '%s  %s\n' "$CHECKSUM" "$(basename "$ARCHIVE")" > "$ARCHIVE.sha256"

cat > "$DIST/$PREFIX.RELEASE.json" <<EOF
{
  "product": "Building Materials Shop Management System",
  "version": "$VERSION",
  "commit": "$COMMIT",
  "short_commit": "$SHORT",
  "created_at": "$CREATED",
  "archive": "$(basename "$ARCHIVE")",
  "sha256": "$CHECKSUM",
  "tests": "python -m pytest -q",
  "notes": "Source archive only; no .env, instance data, backups or secrets are included."
}
EOF

log "Wrote:"
log "  $ARCHIVE"
log "  $ARCHIVE.sha256"
log "  $DIST/$PREFIX.RELEASE.json"
log "SHA256: $CHECKSUM"

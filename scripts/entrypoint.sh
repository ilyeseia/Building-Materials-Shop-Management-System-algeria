#!/bin/sh
# =============================================================================
# Container entry point: serve the application with Gunicorn.
# =============================================================================
# This script never touches the database schema.  Creating the schema, seeding
# reference data and migrations are explicit operator steps - see
# scripts/docker-init.sh and README (Docker deployment).
#
# Environment (all optional, sane defaults):
#   GUNICORN_WORKERS     number of worker processes      (default 3)
#   GUNICORN_LOG_LEVEL   gunicorn log level              (default info)
#   GUNICORN_TIMEOUT     request timeout in seconds      (default 60)
#   PORT                 internal listen port            (default 8000)
#   FORWARDED_ALLOW_IPS  proxies trusted for X-Forwarded-* headers
#                        (default 127.0.0.1; set to "*" only when the
#                        container is reachable exclusively through the proxy)
# =============================================================================
set -eu

exec gunicorn \
    --workers "${GUNICORN_WORKERS:-3}" \
    --bind "0.0.0.0:${PORT:-8000}" \
    --timeout "${GUNICORN_TIMEOUT:-60}" \
    --graceful-timeout 25 \
    --keep-alive 5 \
    --max-requests 1500 \
    --max-requests-jitter 100 \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
    --access-logfile - \
    --error-logfile - \
    --log-level "${GUNICORN_LOG_LEVEL:-info}" \
    wsgi:app

# =============================================================================
# Building Materials Shop Management System - production image
# =============================================================================
# Multi-stage build:
#   builder - installs Python dependencies into /opt/venv (toolchain stays here)
#   runtime - slim image, non-root user, Gunicorn, MySQL client for the scripts
#
# Build : docker compose build
# Run   : docker compose up -d
# =============================================================================

# ---- Stage 1: Python dependencies -------------------------------------------
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Only needed when a dependency has no pre-built wheel for this platform.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install -r requirements.txt \
 && pip install "gunicorn==23.0.0"

# ---- Stage 2: runtime --------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Africa/Algiers \
    APP_ENV=production \
    FLASK_APP=wsgi:app

# curl                  - container health check (GET /healthz)
# default-mysql-client  - mysql / mysqladmin / mysqldump used by scripts/
# tzdata                - Africa/Algiers for the OS clock
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        default-mysql-client \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -fs /usr/share/zoneinfo/Africa/Algiers /etc/localtime \
    && echo "Africa/Algiers" > /etc/timezone

ARG UID=10001
ARG GID=10001
RUN groupadd --gid "${GID}" appgroup \
 && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Application code only; .dockerignore keeps the build context clean.
# The code stays owned by root: the runtime user may execute it but cannot
# modify it.  Only the two paths below are writable by the application.
COPY config.py wsgi.py requirements.txt ./
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# Writable locations (mounted as volumes by compose).
RUN mkdir -p /app/instance /app/backups \
 && chmod +x /app/scripts/*.sh \
 && chown -R appuser:appgroup /app/instance /app/backups

# Bytecode is pre-compiled at build time so that the (read-only) code tree does
# not need to be written at run time; PYTHONDONTWRITEBYTECODE is set above.
RUN python -m compileall -q /app/app /app/migrations /app/config.py /app/wsgi.py

USER appuser
EXPOSE 8000

# 200 only when the process answers *and* the database responds (GET /healthz).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# Gunicorn via scripts/entrypoint.sh (honours GUNICORN_WORKERS).
# The development server is never used here.
CMD ["/app/scripts/entrypoint.sh"]

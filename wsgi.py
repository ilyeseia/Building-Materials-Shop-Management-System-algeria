"""WSGI entry point for a production server: ``wsgi:app``.

Example (Waitress)::

    waitress-serve --listen=127.0.0.1:8000 wsgi:app

Example (Gunicorn)::

    gunicorn --workers 4 --bind 127.0.0.1:8000 wsgi:app

Production refuses to start without ``SECRET_KEY`` and ``DATABASE_URL``; see
``docs/DEPLOYMENT.md``.
"""
from __future__ import annotations

from app import create_app

app = create_app()

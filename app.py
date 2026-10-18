"""Development entry point: ``python app.py``.

For production, serve ``wsgi:app`` with a real WSGI server (Waitress or Gunicorn)
— see ``docs/DEPLOYMENT.md``.  This file deliberately:

* binds to ``127.0.0.1`` by default (the audited revision exposed the development
  server on ``0.0.0.0`` with the debugger enabled, which is remote code execution
  waiting to happen);
* never enables ``debug`` unless explicitly asked for through the environment;
* prints the resolved configuration so the operator can see what was started.
"""
from __future__ import annotations

import os

from app import create_app

app = create_app()


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "5000"))
    debug = _bool_env("FLASK_DEBUG", app.config.get("DEBUG", False))
    if host not in {"127.0.0.1", "localhost"} and debug:
        raise SystemExit(
            "Refusing to start the debug server on a non-loopback address. "
            "Use a WSGI server for network access (docs/DEPLOYMENT.md)."
        )
    app.logger.info("Starting %s on %s:%s (debug=%s)", app.config.get("APP_NAME"), host, port, debug)
    app.run(host=host, port=port, debug=debug)

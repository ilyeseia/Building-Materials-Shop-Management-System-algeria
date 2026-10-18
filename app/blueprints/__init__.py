"""Blueprint registration.

Each module owns one functional area and is registered under a URL prefix.  Only
the ``auth`` and ``static`` endpoints are reachable without a session; every other
blueprint protects its routes with ``@requires(...)``.
"""
from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from app.blueprints import (
        audit,
        auth,
        catalog,
        dashboard,
        finance,
        fleet,
        inventory,
        parties,
        purchasing,
        reports,
        sales,
        settings as settings_bp,
        users,
        weighbridge,
    )

    for module in (
        auth, dashboard, parties, catalog, inventory, sales, purchasing,
        finance, weighbridge, fleet, reports, settings_bp, users, audit,
    ):
        app.register_blueprint(module.bp)

    # Security context processor + 429 handler.
    from app import security

    security.init_app(app)

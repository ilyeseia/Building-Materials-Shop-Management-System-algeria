"""Application factory.

``create_app()`` builds the Flask application: configuration, extensions,
blueprints, template helpers, security headers, error handling and CLI commands.

Run locally with ``python app.py`` (development) or serve ``wsgi:app`` with
Waitress/Gunicorn in production.  See ``docs/DEPLOYMENT.md``.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from decimal import Decimal

from flask import Flask, g, jsonify, render_template, request, session
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException

from config import BASE_DIR, INSTANCE_DIR, WILAYAS, get_config
from app.extensions import csrf, db, login_manager, migrate
from app.i18n import (
    CATALOGUES,
    LANGUAGES,
    format_date,
    format_datetime,
    format_money,
    format_number,
    format_percent,
    format_quantity,
    format_quantity_with_unit,
    get_locale,
    is_rtl,
    localized_name,
    text_direction,
    translate,
    translate_error,
    wilaya_choices,
    wilaya_name,
)


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    config_class = get_config(config_name)
    app.config.from_object(config_class)

    _configure_instance_folder(app)
    _assert_production_secrets(app)
    _configure_logging(app)

    db.init_app(app)
    migrate.init_app(app, db, directory=str(BASE_DIR / "migrations"))
    login_manager.init_app(app)
    csrf.init_app(app)

    from app.blueprints import register_blueprints

    register_blueprints(app)

    _register_template_helpers(app)
    _register_request_hooks(app)
    _register_error_handlers(app)
    _register_cli(app)

    return app


# ---------------------------------------------------------------------------
# Bootstrapping helpers
# ---------------------------------------------------------------------------

def _configure_instance_folder(app: Flask) -> None:
    os.makedirs(app.instance_path, exist_ok=True)
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)


def _assert_production_secrets(app: Flask) -> None:
    """Refuse to boot production without a real secret key and database URL.

    The audited revision fell back to a hard-coded secret committed to the public
    repository, which made every session forgeable.  Failing loudly is the only
    safe behaviour.
    """
    if app.config.get("ENVIRONMENT") != "production":
        return
    missing = []
    if not os.environ.get("SECRET_KEY"):
        missing.append("SECRET_KEY")
    if not os.environ.get("DATABASE_URL"):
        missing.append("DATABASE_URL")
    if missing:
        raise RuntimeError(
            "Refusing to start in production without: " + ", ".join(missing)
            + ". Set them in the environment (see .env.example)."
        )


def _configure_logging(app: Flask) -> None:
    level = logging.DEBUG if app.config.get("DEBUG") else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    app.logger.setLevel(level)


def _register_template_helpers(app: Flask) -> None:
    def now_date() -> str:
        """Today's date in the configured business timezone (Africa/Algiers)."""
        from app.services.timeutil import today_local

        return format_date(today_local())

    app.jinja_env.globals.update(
        t=translate,
        translate_error=translate_error,
        now_date=now_date,
        get_locale=get_locale,
        is_rtl=is_rtl,
        localized_name=localized_name,
        text_direction=text_direction,
        LANGUAGES=LANGUAGES,
        CATALOGUES=CATALOGUES,
        WILAYAS=WILAYAS,
        wilaya_name=wilaya_name,
        wilaya_choices=wilaya_choices,
        app_name=app.config["APP_NAME"],
    )
    app.jinja_env.filters.update(
        money=format_money,
        qty=format_quantity,
        qty_unit=format_quantity_with_unit,
        num=format_number,
        percent=format_percent,
        d=format_date,
        dt=format_datetime,
    )
    app.jinja_env.policies["json.dumps_kwargs"] = {"sort_keys": False}


def _register_request_hooks(app: Flask) -> None:
    from app.services.audit import record_login_event
    from app.services.settings import get_settings

    @app.before_request
    def _prepare_request():
        session.permanent = True
        g.locale = get_locale()
        if request.endpoint in {"static"}:
            return None
        g.settings = get_settings()
        # A disabled account must lose access immediately, not at next sign-in.
        from flask_login import current_user

        if current_user.is_authenticated and not current_user.is_active:
            from flask_login import logout_user

            logout_user()
            session.clear()
        return None

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=()")
        # The interface ships its own CSS/JS and never loads from a CDN, so a
        # strict policy is affordable — and the application works offline.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'self'",
        )
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @login_manager.user_loader
    def _load_user(user_id: str):
        from app.models import User

        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None


def _register_error_handlers(app: Flask) -> None:
    def _wants_json() -> bool:
        return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"

    @app.errorhandler(CSRFError)
    def _handle_csrf(error):  # noqa: ANN001
        app.logger.warning("CSRF failure on %s: %s", request.path, error.description)
        if _wants_json():
            return jsonify({"error": translate("error.csrf"), "code": "csrf"}), 400
        return render_template("errors/400.html", message=translate("error.csrf")), 400

    @app.errorhandler(404)
    def _handle_404(error):  # noqa: ANN001
        if _wants_json():
            return jsonify({"error": translate("error.not_found"), "code": "not_found"}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(403)
    def _handle_403(error):  # noqa: ANN001
        if _wants_json():
            return jsonify({"error": translate("error.403"), "code": "forbidden"}), 403
        return render_template("errors/403.html"), 403

    @app.errorhandler(413)
    def _handle_413(error):  # noqa: ANN001
        if _wants_json():
            return jsonify({"error": translate("error.413"), "code": "too_large"}), 413
        return render_template("errors/400.html", message=translate("error.413")), 413

    @app.errorhandler(Exception)
    def _handle_unexpected(error):  # noqa: ANN001
        if isinstance(error, HTTPException):
            return error
        app.logger.exception("Unhandled error on %s", request.path)
        db.session.rollback()
        if _wants_json():
            return jsonify({"error": translate("error.server"), "code": "server_error"}), 500
        return render_template("errors/500.html"), 500


def _register_cli(app: Flask) -> None:
    from app.cli import register_cli

    register_cli(app)

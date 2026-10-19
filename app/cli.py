"""Command-line interface (``flask <command>``).

Commands are the only supported way to create schema, seed data, manage users and
move data in and out of the system.  None of them is destructive by default, and
none of them runs automatically at start-up — in particular the application never
recreates the database when it boots, which was one of the audited defects
(an unauthenticated ``GET /recreate-db`` route and ``debug=True`` on ``0.0.0.0``).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import click
from flask import current_app
from sqlalchemy import inspect, text

from app.extensions import db
from app.models import User
from app.models.security import ALL_ROLES, Role
from app.services import numbering, seed as seed_service, settings as settings_service
from app.services.timeutil import utcnow

BACKUP_DIR_NAME = "backups"


# ---------------------------------------------------------------------------
# Schema and seed data
# ---------------------------------------------------------------------------

def register_cli(app) -> None:
    @app.cli.command("init-db")
    @click.option("--demo", is_flag=True, help="Also insert a small demo catalogue.")
    def init_db(demo: bool) -> None:
        """Create the schema (never drops anything) and seed reference data."""
        db.create_all()
        created = seed_service.seed_reference_data()
        if demo:
            created.update(seed_service.seed_demo_data())
        db.session.commit()
        click.echo(f"Schema ready. Seed summary: {created}")
        settings = settings_service.get_settings()
        click.echo(
            f"Settings row: business_name={settings.business_name!r} "
            f"currency={settings.currency} timezone={settings.timezone}"
        )

    @app.cli.command("seed")
    @click.option("--demo", is_flag=True, help="Also insert the demo catalogue.")
    def seed(demo: bool) -> None:
        """Idempotently insert reference data (units, categories, warehouse)."""
        created = seed_service.seed_reference_data()
        if demo:
            created.update(seed_service.seed_demo_data())
        db.session.commit()
        click.echo(f"Seeded: {created}")

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.option("--full-name", default="", help="Display name.")
    @click.option("--role", type=click.Choice(sorted(ALL_ROLES)), default=Role.VIEWER)
    @click.password_option(confirmation_prompt=True)
    def create_user(username: str, full_name: str, role: str, password: str) -> None:
        """Create a user (refuses a password that fails the policy)."""
        from app.security import password_policy_errors

        errors = password_policy_errors(password, username=username)
        if errors:
            from app.i18n import translate

            # There is no request context in a CLI command, so the language is
            # explicit (the keys are also the operator's diagnostics).
            raise click.ClickException(
                "Password rejected: "
                + "; ".join(translate(key, language="en") for key in errors)
            )
        if db.session.execute(db.select(User).filter_by(username=username)).scalars().first():
            raise click.ClickException(f"User '{username}' already exists.")
        user = User(username=username, full_name=full_name or username, role=role, is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"User '{username}' created with role '{role}'.")

    @app.cli.command("reset-password")
    @click.option("--username", prompt=True)
    @click.password_option(confirmation_prompt=True)
    def reset_password(username: str, password: str) -> None:
        """Set a new password for an existing user."""
        from app.security import password_policy_errors

        user = db.session.execute(db.select(User).filter_by(username=username)).scalars().first()
        if user is None:
            raise click.ClickException(f"No such user: {username}")
        errors = password_policy_errors(password, username=username)
        if errors:
            from app.i18n import translate

            # Explicit language: a CLI command has no request context.
            raise click.ClickException(
                "Password rejected: "
                + "; ".join(translate(key, language="en") for key in errors)
            )
        user.set_password(password)
        db.session.commit()
        click.echo(f"Password updated for '{username}'.")

    # -----------------------------------------------------------------------
    # Legacy upgrade
    # -----------------------------------------------------------------------

    @app.cli.command("migrate-legacy")
    @click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
    def migrate_legacy(yes: bool) -> None:
        """Upgrade a database created by the pre-refactor application.

        Back up first: ``flask backup``.  The command rebuilds the tables that
        collide with the new schema and copies the legacy rows across; it aborts
        without changing anything if no legacy tables are found.
        """
        from app.legacy import detect_legacy, migrate_legacy as run

        found = detect_legacy()
        if not found:
            click.echo("No legacy tables detected. Nothing to do.")
            return
        click.echo(f"Legacy tables detected: {', '.join(found)}")
        if not yes and not click.confirm(
            "This rewrites colliding tables. Make sure you have a backup. Continue?"
        ):
            raise click.Abort()
        report = run()
        db.session.commit()
        click.echo(json.dumps(report.as_dict(), indent=2, ensure_ascii=False, default=str))

    # -----------------------------------------------------------------------
    # Backup / restore
    # -----------------------------------------------------------------------

    @app.cli.command("backup")
    @click.option("--output", type=click.Path(dir_okay=False), default=None,
                  help="Destination file (default: instance/backups/backup-<timestamp>.json)")
    @click.option("--sqlite-copy", is_flag=True,
                  help="For SQLite, copy the database file byte-for-byte instead of JSON.")
    def backup(output: str | None, sqlite_copy: bool) -> None:
        """Write a portable backup of every table (or a SQLite file copy)."""
        backup_dir = Path(current_app.instance_path) / BACKUP_DIR_NAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dialect = db.engine.dialect.name

        if sqlite_copy and dialect == "sqlite":
            source = Path(db.engine.url.database or "")
            destination = Path(output) if output else backup_dir / f"backup-{stamp}.db"
            destination.write_bytes(source.read_bytes())
            click.echo(f"SQLite copy written to {destination}")
            return

        destination = Path(output) if output else backup_dir / f"backup-{stamp}.json"
        payload = _export_all()
        destination.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        click.echo(f"Backup written to {destination} ({payload['meta']['rows']} rows)")

    @app.cli.command("restore")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
    def restore(path: str, yes: bool) -> None:
        """Restore a JSON backup, replacing the contents of every table."""
        if not yes and not click.confirm(
            "Restore replaces the current data. Continue?"
        ):
            raise click.Abort()
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        inserted = _import_all(payload)
        db.session.commit()
        click.echo(f"Restored {inserted} rows from {path}")

    # -----------------------------------------------------------------------
    # Diagnostics
    # -----------------------------------------------------------------------

    @app.cli.command("check")
    def check() -> None:
        """Report configuration, database connectivity and PDF/Arabic readiness."""
        from app.legacy import detect_legacy
        from app.services import pdf as pdf_service

        click.echo(f"Environment      : {current_app.config.get('ENVIRONMENT')}")
        click.echo(f"Debug            : {current_app.config.get('DEBUG')}")
        click.echo(f"Database dialect : {db.engine.dialect.name}")
        secret_set = bool(current_app.config.get("SECRET_KEY")) and (
            current_app.config.get("SECRET_KEY") != "dev-only-insecure-secret"
        )
        click.echo(f"Real secret set  : {secret_set}")
        try:
            db.session.execute(text("SELECT 1"))
            tables = inspect(db.engine).get_table_names()
            click.echo(f"Tables           : {len(tables)}")
        except Exception as error:  # pragma: no cover - diagnostic path
            click.echo(f"Database error   : {error}")
        font = pdf_service.find_font()
        click.echo(f"Arabic PDF font  : {font.name if font else 'NONE FOUND (use HTML print view)'}")
        click.echo(f"Arabic shaping   : {'available' if pdf_service.shaping_available() else 'missing (pip install arabic-reshaper python-bidi)'}")
        legacy = detect_legacy()
        click.echo(f"Legacy tables    : {', '.join(legacy) if legacy else 'none'}")

    @app.cli.command("version")
    def version_command() -> None:
        """Print the release identity (safe to run in production)."""
        click.echo(f"Version     : {current_app.config.get('APP_VERSION')}")
        click.echo(f"Commit      : {current_app.config.get('BUILD_COMMIT') or 'unknown'}")
        click.echo(f"Build date  : {current_app.config.get('BUILD_DATE') or 'unknown'}")
        click.echo(f"Environment : {current_app.config.get('ENVIRONMENT')}")

    @app.cli.command("doctor")
    def doctor() -> None:
        """Run installation and operational checks; exit 1 only on a hard failure."""
        from app.services import system_status
        from app.i18n import translate

        status = system_status.get_system_status()
        click.echo(f"Version     : {status['app']['version']} ({status['app']['commit']})")
        click.echo(f"Environment : {status['app']['environment']}")
        click.echo("")
        counts = {system_status.PASS: 0, system_status.WARN: 0, system_status.FAIL: 0}
        for check in status["checks"]:
            counts[check["status"]] = counts.get(check["status"], 0) + 1
            label = translate(f"system.check.{check['key']}", language="en")
            click.echo(f"[{check['status'].upper():4}] {label}: {check['detail']}")
        click.echo("")
        click.echo(
            f"Summary: {counts[system_status.PASS]} pass, "
            f"{counts[system_status.WARN]} warning, {counts[system_status.FAIL]} fail"
        )
        click.echo(f"Overall: {status['overall'].upper()}")
        if counts[system_status.FAIL]:
            raise click.exceptions.Exit(1)

    @app.cli.command("license-status")
    def license_status_command() -> None:
        """Print the optional license metadata.  Never locks anything."""
        from app.services import licensing

        status = licensing.load_license()
        click.echo(f"Status      : {status.status}")
        click.echo(f"Licensee    : {status.licensee or '-'}")
        click.echo(f"Edition     : {status.edition}")
        click.echo(f"Issued      : {status.issued_at or '-'}")
        click.echo(f"Expires     : {status.expires_at or 'perpetual'}")
        if status.days_remaining is not None:
            click.echo(f"Days left   : {status.days_remaining}")
        click.echo(f"Signature   : {'valid' if status.signature_valid else ('unverified' if status.signature_valid is None else 'invalid')}")
        click.echo(f"Enforcement : none (a missing or expired license never disables the application)")
        click.echo(f"Detail      : {status.detail}")

    @app.cli.command("ensure-sequences")
    def ensure_sequences() -> None:
        """Create any missing document-number sequence."""
        numbering.ensure_sequences()
        db.session.commit()
        click.echo("Document sequences are up to date.")


# ---------------------------------------------------------------------------
# Portable export / import
# ---------------------------------------------------------------------------

_ENCODED = {"decimal", "date", "datetime"}
_TYPE_KEY = "__type__"
_VALUE_KEY = "__value__"


def _encode(value):
    if isinstance(value, Decimal):
        return {_TYPE_KEY: "decimal", _VALUE_KEY: str(value)}
    if isinstance(value, datetime):
        return {_TYPE_KEY: "datetime", _VALUE_KEY: value.isoformat()}
    if isinstance(value, date):
        return {_TYPE_KEY: "date", _VALUE_KEY: value.isoformat()}
    if isinstance(value, (bytes, bytearray)):
        return value.decode("latin-1")
    return value


def _decode(value):
    if isinstance(value, dict) and value.get(_TYPE_KEY) in _ENCODED:
        kind = value[_TYPE_KEY]
        raw = value[_VALUE_KEY]
        if kind == "decimal":
            return Decimal(raw)
        if kind == "datetime":
            return datetime.fromisoformat(raw)
        return date.fromisoformat(raw)
    return value


def _export_all() -> dict:
    tables: dict[str, list[dict]] = {}
    rows = 0
    for table in db.metadata.sorted_tables:
        columns = [column.name for column in table.columns]
        result = db.session.execute(db.select(table)).mappings().all()
        records = []
        for record in result:
            records.append({name: _encode(record[name]) for name in columns})
        tables[table.name] = records
        rows += len(records)
    return {
        "meta": {
            "created_at": utcnow().isoformat(),
            "app": current_app.config.get("APP_NAME"),
            "rows": rows,
        },
        "tables": tables,
    }


def _import_all(payload: dict) -> int:
    tables = payload.get("tables") or {}
    inserted = 0
    # Delete children first, then insert parents first.
    for table in reversed(db.metadata.sorted_tables):
        if table.name in tables:
            db.session.execute(table.delete())
    for table in db.metadata.sorted_tables:
        records = tables.get(table.name)
        if not records:
            continue
        pending = []
        for record in records:
            pending.append({name: _decode(value) for name, value in record.items()
                            if name in table.columns})
        if pending:
            db.session.execute(table.insert(), pending)
            inserted += len(pending)
    db.session.flush()
    return inserted

"""Administrator status, installation checks and backup visibility.

This module is deliberately read-only.  It answers three questions for an
operator without exposing a secret:

* which release is running (version, commit, environment);
* whether the database, migrations, writable paths and backups are healthy;
* what the optional license metadata says (informational only).

Every probe is defensive: a database outage or an unreadable backup file yields a
``FAIL``/``WARN`` entry instead of a 500 page.  The status screen is restricted to
``settings.manage`` because it reveals infrastructure details; it never renders a
password, API key or connection string.
"""
from __future__ import annotations

import json
import platform
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any

import sqlalchemy
from flask import current_app
from sqlalchemy import text

from app.extensions import db
from app.services import licensing
from app.services.timeutil import today_local, utcnow

PASS = "pass"
WARN = "warn"
FAIL = "fail"

CHECK_ORDER = (
    "secret",
    "database",
    "migrations",
    "instance_writable",
    "backup_directory",
    "backup_age",
    "admin_user",
    "settings",
    "ai_provider",
    "license",
    "secure_cookies",
    "pdf_font",
)


@dataclass
class Check:
    key: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "status": self.status, "detail": self.detail}


def _config(key: str, default: Any = None) -> Any:
    return current_app.config.get(key, default)


def _backup_dir() -> Path:
    return Path(str(_config("BACKUP_DIR", "backups")))


def _instance_dir() -> Path:
    from config import INSTANCE_DIR

    return Path(str(_config("INSTANCE_DIR", INSTANCE_DIR)))


def _writable(directory: Path) -> tuple[bool, str]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".status-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, str(directory)
    except OSError as error:
        return False, type(error).__name__


def _database_probe() -> tuple[bool, str]:
    try:
        with db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, ""
    except Exception as error:  # noqa: BLE001 - diagnostic boundary
        return False, type(error).__name__


def _database_revision() -> str | None:
    try:
        with db.engine.connect() as connection:
            return connection.execute(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            ).scalar()
    except Exception:  # noqa: BLE001
        return None


def _head_revision() -> str | None:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from config import BASE_DIR

        config = Config()
        config.set_main_option("script_location", str(BASE_DIR / "migrations"))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # noqa: BLE001
        return None


def _database_summary() -> dict[str, Any]:
    summary: dict[str, Any] = {
        "dialect": db.engine.dialect.name,
        "server_version": "",
        "revision": None,
        "head_revision": None,
        "table_count": None,
        "size_mb": None,
        "connected": False,
    }
    ok, detail = _database_probe()
    summary["connected"] = ok
    if not ok:
        summary["error"] = detail
        return summary
    try:
        with db.engine.connect() as connection:
            if summary["dialect"] == "mysql":
                summary["server_version"] = str(
                    connection.execute(text("SELECT VERSION()")).scalar() or ""
                )
                size = connection.execute(
                    text(
                        "SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) "
                        "FROM information_schema.tables WHERE table_schema = DATABASE()"
                    )
                ).scalar()
                summary["size_mb"] = float(size) if size is not None else None
            elif summary["dialect"] == "sqlite":
                summary["server_version"] = sqlalchemy.__version__
                database = db.engine.url.database
                if database and database != ":memory:" and Path(database).exists():
                    summary["size_mb"] = round(Path(database).stat().st_size / 1024 / 1024, 2)
        summary["table_count"] = len(sqlalchemy.inspect(db.engine).get_table_names())
    except Exception as error:  # noqa: BLE001
        summary["error"] = type(error).__name__
    summary["revision"] = _database_revision()
    summary["head_revision"] = _head_revision()
    return summary


def _parse_utc(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def backup_status() -> dict[str, Any]:
    directory = _backup_dir()
    payload: dict[str, Any] = {
        "directory": str(directory),
        "exists": directory.exists(),
        "writable": False,
        "latest": None,
        "age_hours": None,
        "stale": True,
        "stale_after_hours": int(_config("BACKUP_STALE_HOURS", 48)),
        "checksum_present": False,
        "last_failure": None,
        "error": "",
    }
    try:
        payload["writable"], _ = _writable(directory)
        failure_file = directory / "latest-failure.json"
        if failure_file.exists():
            failure = json.loads(failure_file.read_text(encoding="utf-8"))
            if isinstance(failure, dict):
                payload["last_failure"] = {
                    "created_at": failure.get("created_at"),
                    "reason": str(failure.get("reason") or "")[:300],
                }
        latest_file = directory / "latest.json"
        if not latest_file.exists():
            return payload
        data = json.loads(latest_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("latest.json is not an object")
        created = _parse_utc(data.get("created_at"))
        age_hours = None
        if created is not None:
            now = datetime.now(timezone.utc)
            age_hours = round((now - created.astimezone(timezone.utc)).total_seconds() / 3600, 1)
        base = str(data.get("base") or "")
        payload.update(
            {
                "latest": {
                    "base": base,
                    "created_at": data.get("created_at"),
                    "schema_revision": data.get("schema_revision"),
                    "table_count": data.get("table_count"),
                    "db_bytes": data.get("db_bytes"),
                    "files_bytes": data.get("files_bytes"),
                    "status": data.get("status"),
                },
                "age_hours": age_hours,
                "stale": age_hours is None or age_hours > payload["stale_after_hours"],
                "checksum_present": bool(base) and (directory / f"{base}.sha256").exists(),
            }
        )
    except (OSError, ValueError, TypeError) as error:
        payload["error"] = type(error).__name__
    return payload


def disk_status() -> dict[str, Any]:
    target = _instance_dir()
    try:
        usage = shutil.disk_usage(target)
        return {
            "path": str(target),
            "total_gb": round(usage.total / 1024**3, 1),
            "free_gb": round(usage.free / 1024**3, 1),
            "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
        }
    except OSError as error:
        return {"path": str(target), "error": type(error).__name__}


def _package_version(name: str) -> str:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return "unknown"


def _admin_count() -> int:
    try:
        from app.models import Role, User

        return int(
            db.session.query(User)
            .filter(User.role == Role.ADMIN, User.is_active.is_(True))
            .count()
        )
    except Exception:  # noqa: BLE001
        return 0


def _ai_summary() -> dict[str, Any]:
    from app.services import ai as ai_service
    from app.services.settings import get_settings

    provider = str(_config("AI_PROVIDER", "local"))
    configured = provider == "local" or bool(_config("AI_API_KEY"))
    return {
        "feature_enabled": ai_service.is_enabled(get_settings()),
        "provider": provider,
        "configured": configured,
        "model": _config("AI_MODEL") if provider != "local" else None,
        "timeout_seconds": int(_config("AI_TIMEOUT_SECONDS", 20)),
        "max_tool_rounds": int(_config("AI_MAX_TOOL_ROUNDS", 3)),
    }


def run_checks() -> list[Check]:
    """Installation and operational checks, ordered for a status page."""
    checks: list[Check] = []
    environment = str(_config("ENVIRONMENT", "development"))
    production = environment == "production"

    secret = _config("SECRET_KEY")
    if secret and str(secret) != "dev-only-insecure-secret" and len(str(secret)) >= 32:
        checks.append(Check("secret", PASS, "configured"))
    elif production:
        checks.append(Check("secret", FAIL, "production requires a strong SECRET_KEY"))
    else:
        checks.append(Check("secret", WARN, "development secret in use"))

    connected, error = _database_probe()
    checks.append(Check("database", PASS if connected else FAIL, error or "SELECT 1"))

    database = _database_summary()
    revision, head = database["revision"], database["head_revision"]
    if revision and head and revision == head:
        checks.append(Check("migrations", PASS, revision))
    elif revision and head:
        checks.append(Check("migrations", WARN, f"{revision} != head {head}"))
    else:
        checks.append(Check("migrations", WARN, "no alembic revision recorded"))

    writable, detail = _writable(_instance_dir())
    checks.append(Check("instance_writable", PASS if writable else FAIL, detail))

    backups = backup_status()
    checks.append(
        Check(
            "backup_directory",
            PASS if backups["writable"] else FAIL,
            backups["directory"],
        )
    )
    if backups["latest"] and not backups["stale"]:
        checks.append(Check("backup_age", PASS, f"{backups['age_hours']} h"))
    elif backups["latest"]:
        checks.append(Check("backup_age", WARN, f"{backups['age_hours']} h (stale)"))
    else:
        checks.append(Check("backup_age", WARN, "no successful backup recorded"))

    admins = _admin_count()
    checks.append(
        Check(
            "admin_user",
            PASS if admins else WARN,
            f"{admins} active administrator(s)",
        )
    )

    from app.services.settings import get_settings

    settings = get_settings()
    if settings and settings.currency and settings.timezone:
        checks.append(Check("settings", PASS, f"{settings.currency} / {settings.timezone}"))
    else:
        checks.append(Check("settings", WARN, "currency or timezone missing"))

    ai = _ai_summary()
    if not ai["feature_enabled"]:
        checks.append(Check("ai_provider", PASS, "workspace disabled"))
    elif ai["configured"]:
        checks.append(Check("ai_provider", PASS, ai["provider"]))
    else:
        checks.append(Check("ai_provider", WARN, "external provider has no API key"))

    license_status = licensing.load_license()
    if license_status.status in {
        licensing.STATUS_COMMUNITY,
        licensing.STATUS_VALID,
        licensing.STATUS_UNVERIFIED,
    }:
        checks.append(Check("license", PASS, license_status.status))
    else:
        checks.append(Check("license", WARN, license_status.status))

    if production and not _config("SESSION_COOKIE_SECURE"):
        checks.append(Check("secure_cookies", WARN, "Secure cookies disabled"))
    else:
        checks.append(Check("secure_cookies", PASS, "Secure cookies enabled" if production else "development"))

    try:
        from app.services import pdf as pdf_service

        font = pdf_service.find_font()
        checks.append(
            Check("pdf_font", PASS if font else WARN, font.name if font else "no Arabic font found")
        )
    except Exception:  # noqa: BLE001
        checks.append(Check("pdf_font", WARN, "font probe failed"))

    order = {key: index for index, key in enumerate(CHECK_ORDER)}
    checks.sort(key=lambda check: order.get(check.key, len(order)))
    return checks


def get_system_status() -> dict[str, Any]:
    """Everything the administrator status page needs, with no secrets."""
    settings = None
    try:
        from app.services.settings import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001
        pass
    database = _database_summary()
    backups = backup_status()
    checks = run_checks()
    worst = FAIL if any(check.status == FAIL for check in checks) else (
        WARN if any(check.status == WARN for check in checks) else PASS
    )
    return {
        "app": {
            "name": str(_config("APP_NAME", "")),
            "version": str(_config("APP_VERSION", "0.0.0")),
            "commit": str(_config("BUILD_COMMIT", "") or "unknown"),
            "build_date": str(_config("BUILD_DATE", "") or "unknown"),
            "environment": str(_config("ENVIRONMENT", "development")),
            "python": platform.python_version(),
            "flask": _package_version("Flask"),
            "sqlalchemy": _package_version("SQLAlchemy"),
            "timezone": str(settings.timezone if settings else _config("TIMEZONE", "")),
            "currency": str(settings.currency if settings else _config("CURRENCY", "")),
        },
        "database": database,
        "backups": backups,
        "disk": disk_status(),
        "license": licensing.load_license().as_dict(),
        "ai": _ai_summary(),
        "checks": [check.as_dict() for check in checks],
        "overall": worst,
        "generated_at": utcnow().isoformat() + "Z",
        "today": today_local().isoformat(),
        "writable_paths": {
            "instance": str(_instance_dir()),
            "backups": str(_backup_dir()),
        },
    }

"""System status page and installation checks."""
from __future__ import annotations

import json

from app.extensions import db
from app.models import Role
from app.services import system_status


def test_status_page_requires_settings_manage(client, make_user, login, app, tmp_path):
    app.config.update(
        BACKUP_DIR=str(tmp_path / "backups"),
        INSTANCE_DIR=str(tmp_path / "instance"),
        LICENSE_FILE=str(tmp_path / "license.json"),
    )
    viewer = make_user("status-viewer", Role.VIEWER)
    db.session.commit()
    login(viewer)
    assert client.get("/settings/system").status_code == 403


def test_status_page_renders_for_admin(auth_client, app, tmp_path):
    app.config.update(
        BACKUP_DIR=str(tmp_path / "backups"),
        INSTANCE_DIR=str(tmp_path / "instance"),
        LICENSE_FILE=str(tmp_path / "license.json"),
    )
    with auth_client.session_transaction() as session:
        session["language"] = "en"
    response = auth_client.get("/settings/system")
    assert response.status_code == 200
    assert b"System status" in response.data
    assert app.config["APP_VERSION"].encode() in response.data
    # The secret itself must never be rendered.
    assert app.config["SECRET_KEY"].encode() not in response.data


def test_status_never_contains_secrets(app, tmp_path):
    app.config.update(
        BACKUP_DIR=str(tmp_path / "backups"),
        INSTANCE_DIR=str(tmp_path / "instance"),
        LICENSE_FILE=str(tmp_path / "license.json"),
    )
    status = system_status.get_system_status()
    text = json.dumps(status, default=str)
    assert str(app.config["SECRET_KEY"]) not in text
    assert "AI_API_KEY" not in text
    assert "DATABASE_URL" not in text
    assert status["app"]["version"]
    assert status["overall"] in {"pass", "warn", "fail"}
    keys = {check["key"] for check in status["checks"]}
    assert {"database", "migrations", "backup_age", "license"}.issubset(keys)


def test_backup_status_reads_latest_json(app, tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    (directory / "latest.json").write_text(
        json.dumps(
            {
                "base": "bmshop-test",
                "created_at": "2026-10-19T12:00:00Z",
                "schema_revision": "b2c3d4e5f6a7",
                "table_count": "53",
                "db_bytes": 1000,
                "files_bytes": 500,
                "status": "complete",
            }
        ),
        encoding="utf-8",
    )
    (directory / "bmshop-test.sha256").write_text("deadbeef", encoding="utf-8")
    app.config["BACKUP_DIR"] = str(directory)
    status = system_status.backup_status()
    assert status["latest"]["base"] == "bmshop-test"
    assert status["checksum_present"] is True
    assert status["age_hours"] is not None


def test_doctor_command_reports_checks(app, tmp_path):
    app.config.update(
        BACKUP_DIR=str(tmp_path / "backups"),
        INSTANCE_DIR=str(tmp_path / "instance"),
        LICENSE_FILE=str(tmp_path / "license.json"),
    )
    result = app.test_cli_runner().invoke(args=["doctor"])
    assert "Summary:" in result.output
    assert "Overall:" in result.output


def test_version_and_license_cli(app, tmp_path):
    app.config["LICENSE_FILE"] = str(tmp_path / "license.json")
    runner = app.test_cli_runner()
    version_result = runner.invoke(args=["version"])
    assert version_result.exit_code == 0
    assert "Version" in version_result.output
    license_result = runner.invoke(args=["license-status"])
    assert license_result.exit_code == 0
    assert "Enforcement : none" in license_result.output

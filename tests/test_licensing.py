"""Transparent licensing: status only, never enforcement."""
from __future__ import annotations

import json
from datetime import date, timedelta

from app.extensions import db
from app.services import licensing


def _write(tmp_path, payload):
    path = tmp_path / "license.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_missing_license_is_community(app, tmp_path):
    app.config["LICENSE_FILE"] = str(tmp_path / "missing.json")
    status = licensing.load_license()
    assert status.status == licensing.STATUS_COMMUNITY
    assert status.enforceable is False


def test_valid_unverified_license_never_enforces(app, tmp_path):
    path = _write(
        tmp_path,
        {
            "schema_version": 1,
            "licensee": "SARL Test",
            "edition": "professional",
            "issued_at": "2026-01-01",
            "expires_at": (date.today() + timedelta(days=365)).isoformat(),
            "features": ["ai", "fleet"],
        },
    )
    app.config["LICENSE_FILE"] = str(path)
    app.config["LICENSE_SECRET"] = None
    status = licensing.load_license()
    assert status.status == licensing.STATUS_UNVERIFIED
    assert status.licensee == "SARL Test"
    assert status.expires_at is not None
    assert status.enforceable is False


def test_expired_license_keeps_working(app, tmp_path):
    path = _write(
        tmp_path,
        {
            "schema_version": 1,
            "licensee": "SARL Test",
            "edition": "professional",
            "issued_at": "2020-01-01",
            "expires_at": "2021-01-01",
        },
    )
    app.config["LICENSE_FILE"] = str(path)
    status = licensing.load_license()
    assert status.status == licensing.STATUS_EXPIRED
    assert status.enforceable is False


def test_malformed_license_is_informational_only(app, tmp_path):
    path = tmp_path / "license.json"
    path.write_text("{not json", encoding="utf-8")
    app.config["LICENSE_FILE"] = str(path)
    status = licensing.load_license()
    assert status.status == licensing.STATUS_INVALID
    assert status.enforceable is False


def test_signed_license_and_tampering(app, tmp_path):
    payload = {
        "schema_version": 1,
        "licensee": "SARL Signée",
        "edition": "enterprise",
        "issued_at": "2026-01-01",
        "expires_at": None,
        "features": ["ai"],
    }
    secret = "test-license-secret"
    payload["signature"] = licensing.sign_license(payload, secret)
    path = _write(tmp_path, payload)
    app.config.update(LICENSE_FILE=str(path), LICENSE_SECRET=secret)
    status = licensing.load_license()
    assert status.status == licensing.STATUS_VALID
    assert status.signature_valid is True
    assert status.enforceable is False

    payload["licensee"] = "Tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    status = licensing.load_license()
    assert status.status == licensing.STATUS_INVALID
    assert status.signature_valid is False

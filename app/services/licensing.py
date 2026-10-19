"""Transparent, non-enforcing license metadata.

The commercial edition is identified by an optional JSON file.  **Nothing in the
application enforces this file.**  A missing, expired, tampered or unreadable
license never disables a feature, deletes data, locks an administrator out or
prevents sign-in.  It is displayed on the administrator status page and printed
by ``flask license-status`` so an operator can see what was purchased and when it
needs renewal.

The optional HMAC signature makes accidental edits and casual tampering visible;
it is deliberately not a copy-protection mechanism.  A real commercial contract
also has legal terms, support and delivery obligations, which software cannot
enforce by itself.  See ``docs/COMMERCIAL.md`` for the honest scope.

File format (``LICENSE_FILE``, default ``instance/license.json``)::

    {
      "schema_version": 1,
      "licensee": "Example SARL",
      "edition": "professional",
      "issued_at": "2026-10-01",
      "expires_at": "2027-10-01",
      "features": ["ai", "fleet"],
      "signature": "<hex HMAC-SHA256 of the canonical body>"
    }

``expires_at`` may be ``null`` for a perpetual license.  ``LICENSE_SECRET`` is
read from the environment; it is never stored in the database or written to a
page.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from flask import current_app

from app.services.timeutil import today_local

STATUS_COMMUNITY = "community"
STATUS_VALID = "valid"
STATUS_UNVERIFIED = "unverified"
STATUS_EXPIRED = "expired"
STATUS_INVALID = "invalid"

_SCHEMA_VERSION = 1
_MAX_BYTES = 64 * 1024


@dataclass
class LicenseStatus:
    status: str
    licensee: str = ""
    edition: str = "community"
    issued_at: date | None = None
    expires_at: date | None = None
    days_remaining: int | None = None
    features: list[str] = field(default_factory=list)
    signed: bool = False
    signature_valid: bool | None = None
    detail: str = ""
    path: str = ""

    @property
    def enforceable(self) -> bool:
        # Explicit and permanent: this project has no lockout code path.
        return False

    @property
    def is_paid(self) -> bool:
        return self.status in {STATUS_VALID, STATUS_UNVERIFIED, STATUS_EXPIRED}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "licensee": self.licensee,
            "edition": self.edition,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "days_remaining": self.days_remaining,
            "features": list(self.features),
            "signed": self.signed,
            "signature_valid": self.signature_valid,
            "detail": self.detail,
            "path": self.path,
            "enforceable": False,
        }


def _path() -> Path | None:
    raw = current_app.config.get("LICENSE_FILE")
    return Path(str(raw)) if raw else None


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("date is not a string")
    return date.fromisoformat(value)


def load_license(path: Path | None = None, *, today: date | None = None) -> LicenseStatus:
    """Read the optional license file.  Never raises, never enforces."""
    target = path or _path()
    if target is None:
        return LicenseStatus(status=STATUS_COMMUNITY, detail="no license path configured")
    if not target.exists():
        return LicenseStatus(
            status=STATUS_COMMUNITY,
            detail="no license file; the community edition continues to work",
            path=str(target),
        )

    try:
        if target.stat().st_size > _MAX_BYTES:
            return LicenseStatus(
                status=STATUS_INVALID,
                detail="license file is larger than 64 KiB",
                path=str(target),
            )
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("license must be a JSON object")
        if int(payload.get("schema_version", 0)) != _SCHEMA_VERSION:
            raise ValueError("unsupported license schema version")
        licensee = str(payload.get("licensee") or "")[:160]
        edition = str(payload.get("edition") or "professional")[:40]
        features = [str(item)[:40] for item in (payload.get("features") or [])][:50]
        issued_at = _parse_date(payload.get("issued_at"))
        expires_at = _parse_date(payload.get("expires_at"))
        signature = payload.get("signature")
        body = {key: value for key, value in payload.items() if key != "signature"}
        secret = current_app.config.get("LICENSE_SECRET")
        signature_valid: bool | None = None
        if secret:
            expected = hmac.new(
                str(secret).encode("utf-8"), _canonical(body), hashlib.sha256
            ).hexdigest()
            signature_valid = bool(signature) and hmac.compare_digest(
                str(signature), expected
            )
            if not signature_valid:
                return LicenseStatus(
                    status=STATUS_INVALID,
                    licensee=licensee,
                    edition=edition,
                    issued_at=issued_at,
                    expires_at=expires_at,
                    features=features,
                    signed=bool(signature),
                    signature_valid=False,
                    detail="signature does not match LICENSE_SECRET",
                    path=str(target),
                )

        now = today or today_local()
        days = (expires_at - now).days if expires_at else None
        if expires_at and expires_at < now:
            return LicenseStatus(
                status=STATUS_EXPIRED,
                licensee=licensee,
                edition=edition,
                issued_at=issued_at,
                expires_at=expires_at,
                days_remaining=days,
                features=features,
                signed=bool(signature),
                signature_valid=signature_valid,
                detail="license has expired; the application keeps working normally",
                path=str(target),
            )
        return LicenseStatus(
            status=STATUS_VALID if secret else STATUS_UNVERIFIED,
            licensee=licensee,
            edition=edition,
            issued_at=issued_at,
            expires_at=expires_at,
            days_remaining=days,
            features=features,
            signed=bool(signature),
            signature_valid=signature_valid,
            detail=(
                "verified"
                if secret
                else "no LICENSE_SECRET configured; metadata is informational only"
            ),
            path=str(target),
        )
    except (OSError, ValueError, TypeError, KeyError) as error:
        return LicenseStatus(
            status=STATUS_INVALID,
            detail=f"license could not be read: {type(error).__name__}",
            path=str(target),
        )


def sign_license(payload: dict[str, Any], secret: str) -> str:
    """Return the hex signature used by the release/onboarding tooling."""
    body = {key: value for key, value in payload.items() if key != "signature"}
    return hmac.new(str(secret).encode("utf-8"), _canonical(body), hashlib.sha256).hexdigest()

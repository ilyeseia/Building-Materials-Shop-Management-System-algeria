"""Domain errors.

Each error carries a *translation key* plus parameters rather than a message, so
the interface can show the message in the user's language while logs keep a stable
machine-readable code.
"""
from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """A rule of the business was violated.  Always user-presentable."""

    status_code = 400

    def __init__(self, message_key: str, **params: Any) -> None:
        self.message_key = message_key
        self.params = params
        super().__init__(f"{message_key} {params}" if params else message_key)

    def message(self, language: str | None = None) -> str:
        from app.i18n import translate

        return translate(self.message_key, language=language, **self.params)


class ValidationError(BusinessError):
    status_code = 400


class DecimalParseError(ValidationError):
    def __init__(self, field: str = "") -> None:
        super().__init__("val.invalid_number")
        self.field = field


class InsufficientStock(BusinessError):
    status_code = 409


class InvalidTransition(BusinessError):
    status_code = 409

    def __init__(self, message_key: str = "val.bad_status_transition", **params: Any) -> None:
        super().__init__(message_key, **params)


class NotFoundError(BusinessError):
    status_code = 404

    def __init__(self, message_key: str = "error.not_found", **params: Any) -> None:
        super().__init__(message_key, **params)


class PermissionDenied(BusinessError):
    status_code = 403

    def __init__(self, message_key: str = "auth.forbidden", **params: Any) -> None:
        super().__init__(message_key, **params)

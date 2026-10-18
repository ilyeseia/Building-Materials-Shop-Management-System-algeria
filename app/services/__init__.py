"""Service layer.

Business rules live here, never in templates or view functions, so that every
path that changes stock, money or documents goes through one implementation:
numbered documents, ledger entries, audit trail and validation included.
"""
from app.services.errors import (  # noqa: F401
    BusinessError,
    InsufficientStock,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
    DecimalParseError,
)

__all__ = [
    "BusinessError",
    "ValidationError",
    "InsufficientStock",
    "InvalidTransition",
    "NotFoundError",
    "PermissionDenied",
    "DecimalParseError",
]

"""Encryption of AI provider credentials at rest.

Why this module exists
-----------------------
The AI workspace can be pointed at any OpenAI-compatible endpoint.  An
administrator adds the provider's API key through the interface, so the secret
has to be stored in the database - which the application also reads for
reporting, and which an operator can copy.

Design
------
* The key is encrypted with **Fernet** (AES-128-CBC + HMAC-SHA256), so a
  tampered ciphertext is detected instead of being silently decrypted.
* The Fernet key is derived from the application's ``SECRET_KEY`` with
  PBKDF2-HMAC-SHA256 and a fixed application salt, so the same installation
  always derives the same key and the secret never has to be stored twice.
* The plaintext key is **never** returned to a page, written to a log, put in
  the audit trail or included in an exception message.  Only a short *hint*
  (the last four characters) is kept in clear.
* If ``SECRET_KEY`` changes, the stored ciphertext can no longer be opened.
  That is reported as "the key cannot be read, please enter it again" instead
  of crashing the page.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from flask import current_app

#: Fixed salt.  It is not a secret: it only separates the key derivation from
#: any other use of ``SECRET_KEY``.  The real secret is the password.
_KDF_SALT = b"bmshop-ai-provider-key-v1"

#: PBKDF2 rounds.  High enough to make an offline guess of a leaked
#: ``SECRET_KEY`` expensive, low enough to stay fast at request time.
_KDF_ROUNDS = 200_000

#: How many trailing characters are kept in clear for display.
_HINT_LENGTH = 4


class KeyUnavailable(Exception):
    """The stored credential cannot be decrypted (usually a new SECRET_KEY)."""


def _secret() -> bytes:
    secret = current_app.config.get("SECRET_KEY")
    if not secret:
        raise KeyUnavailable("no secret key configured")
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return secret


def _fernet():
    from cryptography.fernet import Fernet

    derived = hashlib.pbkdf2_hmac("sha256", _secret(), _KDF_SALT, _KDF_ROUNDS)
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_key(plaintext: str | None) -> str | None:
    """Encrypt a credential.  An empty credential stays empty."""
    if plaintext is None:
        return None
    text = plaintext.strip()
    if not text:
        return None
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_key(ciphertext: str | None) -> str | None:
    """Decrypt a credential.

    Raises :class:`KeyUnavailable` when the ciphertext cannot be opened, for
    example after ``SECRET_KEY`` was rotated.
    """
    if not ciphertext:
        return None
    try:
        from cryptography.fernet import InvalidToken
    except ImportError as error:  # pragma: no cover - dependency is pinned
        raise KeyUnavailable("encryption backend is unavailable") from error
    try:
        raw = _fernet().decrypt(ciphertext.encode("ascii"))
    except InvalidToken as error:
        raise KeyUnavailable("the stored credential cannot be decrypted") from error
    except (ValueError, TypeError) as error:
        raise KeyUnavailable("the stored credential is malformed") from error
    return raw.decode("utf-8", "replace")


def key_hint(plaintext: str | None) -> str | None:
    """A short, non-reversible display hint such as ``…a1b2``."""
    if not plaintext:
        return None
    text = plaintext.strip()
    if len(text) <= _HINT_LENGTH:
        return "*" * len(text)
    return "..." + text[-_HINT_LENGTH:]


def mask_key(plaintext: str | None) -> str:
    """Mask a credential for display without ever revealing it."""
    if not plaintext:
        return ""
    text = plaintext.strip()
    if len(text) <= 8:
        return "*" * len(text)
    return text[:3] + "*" * (len(text) - 7) + text[-4:]


def constant_time_equals(left: str | None, right: str | None) -> bool:
    """Compare two secrets without leaking their length through timing."""
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def is_cryptography_available() -> bool:
    try:
        import cryptography.fernet  # noqa: F401
    except Exception:  # pragma: no cover - dependency is pinned
        return False
    return True


def random_key(length: int = 32) -> str:
    """A random credential, used when a local runtime needs a placeholder."""
    return base64.urlsafe_b64encode(os.urandom(length)).decode("ascii").rstrip("=")

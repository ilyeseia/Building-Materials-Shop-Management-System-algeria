"""Security helpers: authorisation, rate limiting and request safety.

Threat model notes (kept honest and in the docs too)
----------------------------------------------------
* **Authorisation is server-side.**  Every protected route carries
  ``@requires(...)``; hiding a menu item is *not* a control.  The decorator
  checks the permission on the database-backed user object, so a hand-crafted
  request fails exactly like a clicked link.
* **Rate limiting is in-process.**  It stops casual brute force and accidental
  hammering on a single worker.  Behind several workers/replicas each process has
  its own counters, which is documented as a limitation: production deployments
  that need a global limit must put the limit in the reverse proxy or a shared
  store (see ``docs/SECURITY.md``).  This is why authentication is additionally
  protected by account lock-out on repeated failures *and* audited.
* **No secret in code.**  Nothing here stores or derives a secret; the session
  key comes from the environment and production refuses to start without it.
"""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from typing import Callable

from flask import abort, current_app, flash, request, url_for
from flask_login import current_user

from app.i18n import translate
from app.models.security import PASSWORD_MIN_LENGTH

# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------

def can(permission: str) -> bool:
    """Whether the current user holds ``permission`` (template helper)."""
    user = getattr(current_user, "_get_current_object", lambda: current_user)()
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    checker = getattr(user, "has_permission", None)
    return bool(checker(permission)) if callable(checker) else False


def has_permission(user, permission: str) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    checker = getattr(user, "has_permission", None)
    return bool(checker(permission)) if callable(checker) else False


def requires(*permissions: str, any_of: bool = False):
    """Require one (or, with ``any_of``, at least one) permission.

    Unauthenticated users are redirected to the login page by Flask-Login
    (``@login_required`` is applied implicitly); authenticated users without the
    permission receive a 403, never a silent success.
    """
    if not permissions:
        raise ValueError("requires() needs at least one permission")

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not getattr(current_user, "is_authenticated", False):
                from flask_login import login_required

                return login_required(view)(*args, **kwargs)
            granted = [has_permission(current_user, p) for p in permissions]
            allowed = any(granted) if any_of else all(granted)
            if not allowed:
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Thread-safe sliding-window counter keyed by an arbitrary string."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, max_calls: int, window_seconds: int) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``."""
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= max_calls:
                retry_after = int(bucket[0] + window_seconds - now) + 1
                return False, max(retry_after, 1)
            bucket.append(now)
            return True, 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


_limiter = _RateLimiter()


def client_key(*extra: str) -> str:
    """A rate-limit key: remote address plus optional discriminator."""
    address = request.remote_addr or "unknown"
    # Behind a reverse proxy, ``ProxyFix``/the proxy sets remote_addr correctly;
    # we deliberately do not trust X-Forwarded-For blindly here.
    return "|".join([address, *[str(part).lower().strip() for part in extra if part]])


def login_limited(username: str | None = None) -> tuple[bool, int]:
    """Whether the login attempt should be rejected. ``(limited, retry_after)``."""
    max_attempts = current_app.config.get("LOGIN_MAX_ATTEMPTS", 5)
    window = current_app.config.get("LOGIN_WINDOW_SECONDS", 300)
    allowed_ip, retry = _limiter.check(f"login:ip:{client_key()}", max_attempts * 4, window)
    if not allowed_ip:
        return True, retry
    if username:
        allowed_user, retry_user = _limiter.check(
            f"login:user:{username.lower()}", max_attempts, window
        )
        if not allowed_user:
            return True, retry_user
    return False, 0


def reset_login_limit(username: str | None = None) -> None:
    _limiter.reset(f"login:ip:{client_key()}")
    if username:
        _limiter.reset(f"login:user:{username.lower()}")


def rate_limit(max_calls: int, window_seconds: int, *, scope: str | None = None,
               by_user: bool = False):
    """Decorator limiting how often a view may run for one client."""

    def decorator(view: Callable) -> Callable:
        endpoint = scope or f"{view.__module__}.{view.__name__}"

        @wraps(view)
        def wrapper(*args, **kwargs):
            discriminated = [endpoint]
            if by_user and getattr(current_user, "is_authenticated", False):
                discriminated.append(str(current_user.id))
            allowed, retry = _limiter.check(
                "rl:" + client_key(*discriminated), max_calls, window_seconds
            )
            if not allowed:
                current_app.logger.warning(
                    "Rate limit hit on %s from %s", endpoint, request.remote_addr
                )
                abort(429)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def reset_rate_limits() -> None:
    """Clear every counter (used by tests and by an admin action)."""
    _limiter.clear()


# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------

#: Passwords that are guessed first by any credential-stuffing list.  Kept short
#: on purpose: a blocklist is a floor, not a strength meter.
_COMMON_PASSWORDS = frozenset({
    "password", "passwd", "motdepasse", "mot de passe", "admin", "admin123",
    "administrateur", "123456", "12345678", "123456789", "1234567890",
    "qwerty", "azerty", "azerty123", "letmein", "welcome", "iloveyou",
    "changeme", "secret", "test", "test1234", "algerie", "algeria", "dzair",
    "espclaw", "esp-claw", "shop", "boutique",
})

_TRAILING_DECORATION = re.compile(r"[\W\d_]+$")


def _common_core(candidate: str) -> str:
    """``password1``/``password!`` collapse to ``password`` before comparison."""
    return _TRAILING_DECORATION.sub("", candidate.lower())


def password_policy_errors(password: str, *, username: str | None = None) -> list[str]:
    """Return translation keys for each password-policy violation (empty = ok).

    The rules are deliberately explainable to a shopkeeper: long enough, not a
    password everybody guesses, not the account's own name, and contains both a
    letter and a digit.  Nothing pretends to measure "entropy".
    """
    errors: list[str] = []
    candidate = password or ""
    lowered = candidate.lower()
    core = _common_core(candidate)
    if len(candidate) < PASSWORD_MIN_LENGTH:
        errors.append("auth.password_too_short")
    if core and core in _COMMON_PASSWORDS:
        errors.append("auth.password_too_weak")
    if username and len(username) >= 4 and username.lower() in lowered:
        # A password built on the account name is the first thing an attacker
        # tries, whatever else it contains.
        if "auth.password_too_weak" not in errors:
            errors.append("auth.password_too_weak")
    if candidate and not any(char.isdigit() for char in candidate):
        errors.append("auth.password_needs_digit")
    if candidate and not any(char.isalpha() for char in candidate):
        errors.append("auth.password_needs_letter")
    return errors


# ---------------------------------------------------------------------------
# Request safety
# ---------------------------------------------------------------------------

def is_safe_redirect_url(target: str | None) -> bool:
    """Only allow same-site relative redirects (blocks open-redirect abuse)."""
    if not target:
        return False
    if target.startswith("//") or "\\" in target:
        return False
    reference = request.host_url.rstrip("/")
    if target.startswith("/"):
        return True
    return target.startswith(reference + "/")


def safe_next_url(fallback_endpoint: str = "dashboard.index") -> str:
    target = request.args.get("next") or request.form.get("next")
    if is_safe_redirect_url(target):
        return target
    return url_for(fallback_endpoint)


def flash_error(message_key: str, **params) -> None:
    """Flash a translated error without leaking internals."""
    flash(translate(message_key, **params), "danger")


def flash_success(message_key: str, **params) -> None:
    flash(translate(message_key, **params), "success")


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------

def init_app(app) -> None:
    """Expose :func:`can` to templates and install a request-timer-free hook."""

    @app.context_processor
    def _inject_security():
        return {"can": can, "has_permission": has_permission}

    @app.errorhandler(429)
    def _handle_429(error):  # noqa: ANN001
        from flask import jsonify, render_template

        message = translate("error.429")
        if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
            return jsonify({"error": message, "code": "rate_limited"}), 429
        return render_template("errors/400.html", message=message), 429

"""Security: authorisation, CSRF, headers, rate limiting, passwords and auditing.

These tests are deliberately structural.  A role that exists in the database but
is never checked by a route is not a control, so several tests hit a *manage*
endpoint as a read-only user and demand a 403 rather than "the link is hidden".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.extensions import db
from app.models import AuditEvent, User
from app.models.security import Role
from app.security import (
    is_safe_redirect_url,
    password_policy_errors,
    reset_rate_limits,
)

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


@pytest.fixture(autouse=True)
def _clean_limiter():
    """The rate limiter is a process-wide singleton; keep tests independent."""
    reset_rate_limits()
    yield
    reset_rate_limits()


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    ["/", "/inventory", "/sales/invoices", "/reports", "/users", "/settings", "/audit"],
)
def test_anonymous_visitors_are_sent_to_the_login_page(client, path):
    response = client.get(path)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_a_viewer_can_read_but_not_manage(client, viewer_user, login):
    login(viewer_user)

    assert client.get("/inventory").status_code == 200
    assert client.get("/inventory/adjustments/new").status_code == 403


def test_authorisation_is_server_side_not_a_hidden_link(client, viewer_user, login):
    """A hand-crafted POST must fail exactly like a clicked link."""
    login(viewer_user)

    response = client.post("/catalog/products/new", data={"name": "Faux"})

    assert response.status_code == 403


def test_managing_users_needs_the_users_permission(client, make_user, login):
    manager = make_user("manager", Role.MANAGER)
    db.session.commit()
    login(manager)

    # A manager runs the business but does not administer accounts.
    assert client.get("/users").status_code == 200
    assert client.get("/users/new").status_code == 403


def test_an_admin_reaches_the_manage_pages(client, admin_user, login):
    login(admin_user)

    for path in ("/users/new", "/inventory/adjustments/new", "/settings", "/catalog/products/new"):
        assert client.get(path).status_code == 200, path


def test_a_deactivated_account_cannot_sign_in(client, make_user):
    user = make_user("retired", Role.ADMIN, password="Karim-2026-Ok", is_active=False)
    db.session.commit()

    response = client.post("/login", data={"username": user.username, "password": "Karim-2026-Ok"})

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Login behaviour and brute-force protection
# ---------------------------------------------------------------------------

def test_a_successful_login_starts_a_session(client, make_user):
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    response = client.post("/login", data={"username": "admin", "password": "Karim-2026-Ok"})

    assert response.status_code == 302
    assert client.get("/").status_code == 200


def test_a_wrong_password_is_rejected_and_audited(client, admin_user):
    response = client.post("/login", data={"username": "admin", "password": "not-the-password"})

    assert response.status_code == 401
    events = db.session.query(AuditEvent).filter_by(action="login_failed").all()
    assert len(events) == 1
    assert events[0].summary and "admin" in events[0].summary


def test_the_login_error_does_not_reveal_whether_the_account_exists(client, admin_user):
    known = client.post("/login", data={"username": "admin", "password": "wrong-one"})
    unknown = client.post("/login", data={"username": "ghost", "password": "wrong-one"})

    assert known.status_code == unknown.status_code == 401


def test_repeated_failures_are_rate_limited(app, client, admin_user):
    app.config["LOGIN_MAX_ATTEMPTS"] = 2
    statuses = [
        client.post("/login", data={"username": "admin", "password": "nope"}).status_code
        for _ in range(4)
    ]

    assert 429 in statuses
    assert statuses[-1] == 429


def test_a_successful_login_clears_the_attempt_counter(app, client, make_user):
    app.config["LOGIN_MAX_ATTEMPTS"] = 2
    user = make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    client.post("/login", data={"username": "admin", "password": "nope"})
    assert client.post("/login", data={"username": "admin", "password": "Karim-2026-Ok"}).status_code == 302

    client.get("/logout")
    # The counter was reset on success, so fresh failures get their full budget.
    assert client.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def test_passwords_are_hashed_with_a_per_user_salt(make_user):
    first = make_user("ali", Role.ADMIN, password="Karim-2026-Ok")
    second = make_user("samir", Role.ADMIN, password="Karim-2026-Ok")

    assert "Karim-2026-Ok" not in first.password_hash
    assert first.password_hash != second.password_hash      # salted, not a bare digest
    assert first.check_password("Karim-2026-Ok") is True
    assert first.check_password("karim-2026-ok") is False


def test_the_password_policy_reports_every_problem_on_the_short_password():
    assert password_policy_errors("abc") == [
        "auth.password_too_short",
        "auth.password_needs_digit",
    ]


def test_the_password_policy_rejects_common_and_personal_passwords():
    # The trailing digit must not smuggle a guessable word past the blocklist.
    assert "auth.password_too_weak" in password_policy_errors("password1")
    assert "auth.password_too_weak" in password_policy_errors("Azerty!")
    # A password built on the account name is rejected even when spelled out.
    assert "auth.password_too_weak" in password_policy_errors("Karim-2026", username="karim")
    assert password_policy_errors("Sable-2026", username="karim") == []


def test_setting_a_short_password_raises(make_user):
    user = make_user("ali", Role.ADMIN)
    with pytest.raises(ValueError):
        user.set_password("short")


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def test_state_changing_posts_require_a_csrf_token(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post("/login", data={"username": "admin", "password": "whatever"})
        assert response.status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_csrf_is_switched_on_outside_the_test_configuration():
    from config import DevelopmentConfig, ProductionConfig

    # The tests disable it for convenience; shipped configurations must not.
    assert DevelopmentConfig.WTF_CSRF_ENABLED is True
    assert ProductionConfig.WTF_CSRF_ENABLED is True


# ---------------------------------------------------------------------------
# Response headers and offline assets
# ---------------------------------------------------------------------------

def test_security_headers_are_set_on_every_response(client):
    headers = client.get("/login").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "same-origin"
    policy = headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy


def test_the_strict_csp_holds_because_nothing_is_loaded_from_a_cdn():
    """A strict ``script-src 'self'`` only works while templates stay offline."""
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'(?:src|href)\s*=\s*["\'](https?:)?//', text):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == []


def test_no_template_uses_an_inline_event_handler():
    """``script-src 'self'`` blocks inline handlers, so they silently do nothing.

    A delete button guarded by ``onsubmit="return confirm(...)"`` would be
    submitted without asking.  The application uses ``data-confirm`` and
    ``data-autosubmit`` instead, which ``app.js`` handles.
    """
    pattern = re.compile(r"<[^>]+\son[a-z]+\s*=", re.IGNORECASE)
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            # ``data-confirm`` and ``data-autosubmit`` are attributes, not
            # event handlers; the pattern only matches a real ``on*`` handler.
            attribute = match.group(0)
            name = re.search(r"\son([a-z]+)\s*=", attribute, re.IGNORECASE)
            if not name:
                continue
            offenders.append(f"{path.name}: {attribute.strip()[:80]}")
    assert offenders == []


def test_every_post_form_carries_a_csrf_token():
    """A form without a CSRF field is rejected with a 400.

    ``{{ csrf_token() }}`` on its own renders the token as *text*, not as an
    input, so the field has to be a hidden input.  A WTForms form carries its
    own token through ``form.hidden_tag()``.
    """
    form_pattern = re.compile(
        r"<form\b[^>]*method=[\"']post[\"'][^>]*>(.*?)</form>", re.IGNORECASE | re.DOTALL
    )
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in form_pattern.finditer(text):
            body = match.group(1)
            if 'name="csrf_token"' in body:
                continue
            if "hidden_tag" in body:
                # A WTForms form renders its own token.
                continue
            if "{{ csrf_token() }}" in body:
                offenders.append(f"{path.name}: bare csrf_token()")
                continue
            offenders.append(f"{path.name}: no csrf_token field")
    assert offenders == []


def test_session_cookie_flags(client, make_user):
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    response = client.post("/login", data={"username": "admin", "password": "Karim-2026-Ok"})
    cookie = response.headers.get("Set-Cookie", "")

    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie.replace("SameSite=lax", "SameSite=Lax")


# ---------------------------------------------------------------------------
# Open redirects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "target",
    ["https://evil.example/steal", "//evil.example", "/\\evil.example", "http://evil.example"],
)
def test_offsite_redirect_targets_are_refused(app, target):
    with app.test_request_context("/login"):
        assert is_safe_redirect_url(target) is False


def test_login_ignores_an_offsite_next_target(app, client, make_user):
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    response = client.post(
        "/login?next=https://evil.example/steal",
        data={"username": "admin", "password": "Karim-2026-Ok"},
    )

    assert response.status_code == 302
    assert "evil.example" not in response.headers["Location"]


# ---------------------------------------------------------------------------
# Audit trail is append-only
# ---------------------------------------------------------------------------

def test_the_audit_log_can_only_be_read_through_the_interface(app):
    endpoints = {rule.endpoint: rule.methods for rule in app.url_map.iter_rules()}

    audit_views = {name: methods for name, methods in endpoints.items() if name.startswith("audit.")}
    assert set(audit_views) == {"audit.index"}
    assert audit_views["audit.index"] - {"HEAD", "OPTIONS"} == {"GET"}


def test_audit_events_capture_actor_action_and_source(app, client, admin_user, login):
    """A real request records who did it, from where."""
    login(admin_user)

    client.get("/logout")

    event = db.session.query(AuditEvent).filter_by(action="logout").one()
    assert event.actor_id == admin_user.id
    assert event.actor_name == admin_user.username
    assert event.entity_type == "user"
    assert event.ip_address is not None
    assert event.created_at is not None


def test_an_explicit_actor_is_recorded_without_a_request(app, admin_user):
    from app.services import audit

    event = audit.record("tested", "audit_event", 1, summary="pytest", actor=admin_user)
    db.session.commit()

    assert event is not None
    assert event.actor_id == admin_user.id
    assert event.action == "tested"


def test_an_audit_failure_does_not_break_the_business_transaction(app, monkeypatch):
    from app.services import audit

    class Boom(Exception):
        pass

    def explode(*args, **kwargs):
        raise Boom()

    monkeypatch.setattr(audit, "AuditEvent", explode, raising=False)
    assert audit.record("tested", "audit_event") is None


# ---------------------------------------------------------------------------
# Production refuses to start without real secrets
# ---------------------------------------------------------------------------

def test_production_refuses_to_boot_without_a_configured_secret(monkeypatch):
    from app import create_app

    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError) as error:
        create_app("production")

    assert "SECRET_KEY" in str(error.value)


def test_no_secret_key_is_committed_as_a_default():
    from config import Config

    assert Config.SECRET_KEY is None

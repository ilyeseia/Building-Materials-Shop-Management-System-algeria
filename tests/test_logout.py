"""Logout must actually end the session - including the persistent cookie.

These tests exist because "sign out" left the account signed in: the
"remember me" cookie survived the logout and Flask-Login silently
re-authenticated the user on the next request.
"""
from __future__ import annotations

from app.extensions import db
from app.models import User, UserSession
from app.models.security import Role


def _login(client, username, password, remember=False):
    data = {"username": username, "password": password}
    if remember:
        data["remember"] = "y"
    return client.post("/login", data=data)


def test_logout_deletes_the_remember_cookie(client, make_user):
    """A remembered session must not survive the logout."""
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    response = _login(client, "admin", "Karim-2026-Ok", remember=True)
    assert response.status_code == 302
    assert client.get_cookie("remember_token") is not None, "the remember cookie was never set"

    client.get("/logout")

    assert client.get_cookie("remember_token") is None, (
        "the remember cookie survived the logout"
    )


def test_logout_ends_the_authentication(client, make_user):
    """After the logout the user must be anonymous again."""
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    _login(client, "admin", "Karim-2026-Ok", remember=True)

    response = client.get("/logout")
    assert response.status_code == 302

    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_logout_without_remember_also_ends_the_authentication(client, make_user):
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    _login(client, "admin", "Karim-2026-Ok")

    client.get("/logout")

    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_logout_marks_the_tracked_session_inactive(client, make_user):
    """The IAM session row must be revoked when the user signs out."""
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    _login(client, "admin", "Karim-2026-Ok")
    tracked = db.session.query(UserSession).order_by(UserSession.id.desc()).first()
    assert tracked is not None
    assert tracked.is_active is True

    client.get("/logout")
    db.session.refresh(tracked)
    assert tracked.is_active is False


def test_logout_works_even_when_the_tracked_row_is_missing(client, make_user):
    """A missing session row must never block the sign-out."""
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    _login(client, "admin", "Karim-2026-Ok")
    db.session.query(UserSession).delete()
    db.session.commit()

    response = client.get("/logout")
    assert response.status_code == 302

    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_logging_in_twice_in_the_same_browser_session_works(client, make_user):
    """A second sign-in must not collide with the previous session key."""
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    assert _login(client, "admin", "Karim-2026-Ok").status_code == 302
    # Sign out, then sign in again in the same browser session.
    client.get("/logout")
    response = _login(client, "admin", "Karim-2026-Ok")
    assert response.status_code == 302
    assert client.get("/").status_code == 200


def test_the_login_after_logout_creates_a_fresh_session_row(client, make_user):
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    _login(client, "admin", "Karim-2026-Ok")
    first = db.session.query(UserSession).order_by(UserSession.id.desc()).first()

    client.get("/logout")
    _login(client, "admin", "Karim-2026-Ok")
    second = db.session.query(UserSession).order_by(UserSession.id.desc()).first()

    assert second is not None
    assert second.id != first.id
    assert second.session_key != first.session_key


def test_changing_the_password_keeps_the_current_session_alive(client, make_user):
    """A password change must not kill the session that made the change."""
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    _login(client, "admin", "Karim-2026-Ok")
    current = db.session.query(UserSession).order_by(UserSession.id.desc()).first()
    assert current is not None

    response = client.post(
        "/password",
        data={
            "current_password": "Karim-2026-Ok",
            "new_password": "Karim-2026-New",
            "confirm_password": "Karim-2026-New",
        },
    )
    assert response.status_code == 302

    db.session.refresh(current)
    assert current.is_active is True, "the session that changed the password was revoked"
    assert client.get("/").status_code == 200


def test_revoking_a_session_ends_the_access(client, make_user):
    """Revoking a session from the sessions page must sign that device out."""
    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    _login(client, "admin", "Karim-2026-Ok")
    tracked = db.session.query(UserSession).order_by(UserSession.id.desc()).first()
    assert tracked is not None
    assert client.get("/").status_code == 200

    # An administrator revokes the session from another browser.
    admin = make_user("boss", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()
    other = app_client_for(client)
    other.post("/login", data={"username": "boss", "password": "Karim-2026-Ok"})
    other.post(f"/users/sessions/{tracked.session_key}/revoke")

    db.session.refresh(tracked)
    assert tracked.is_active is False

    # The revoked browser must lose access on its next request.
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def app_client_for(client):
    """A second, independent browser session against the same application."""
    return client.application.test_client()


def test_the_per_request_session_check_stays_cheap(client, make_user):
    """Enforcing revoked sessions must not reintroduce a per-request flood.

    The check adds at most one SELECT and only writes the "last seen" stamp
    once a minute, so a dashboard request stays well under the guard.
    """
    from sqlalchemy import event

    make_user("admin", Role.ADMIN, password="Karim-2026-Ok")
    db.session.commit()

    _login(client, "admin", "Karim-2026-Ok")

    counter = {"queries": 0}

    def count(*_args):
        counter["queries"] += 1

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        response = client.get("/")
    finally:
        event.remove(db.engine, "before_cursor_execute", count)

    assert response.status_code == 200
    assert counter["queries"] < 30, f"the dashboard issued {counter['queries']} queries"


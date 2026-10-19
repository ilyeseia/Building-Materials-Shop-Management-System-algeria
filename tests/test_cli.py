"""Tests for the operator CLI (``flask <command>``).

CLI commands run outside a request context, so everything they print must be
translated with an explicit language, and a refused input must produce a clear
message and a non-zero exit code — never a traceback.  ``create-user`` used to
crash with ``RuntimeError: Working outside of request context`` when a password
was rejected, because the policy message was translated with the request-bound
locale.
"""
from __future__ import annotations

from app.extensions import db
from app.models import User


def _user(username: str) -> User | None:
    return db.session.execute(db.select(User).filter_by(username=username)).scalars().first()


def test_create_user_accepts_a_password_that_follows_the_policy(app, runner):
    result = runner.invoke(
        args=["create-user", "--username", "seller1", "--role", "sales"],
        input="BmShop-9f3a71\nBmShop-9f3a71\n",
    )

    assert result.exit_code == 0, result.output
    assert "created" in result.output
    assert _user("seller1") is not None


def test_create_user_reports_a_rejected_password_without_a_traceback(app, runner):
    result = runner.invoke(
        args=["create-user", "--username", "seller2", "--role", "sales"],
        input="Seller2-passw0rd\nSeller2-passw0rd\n",
    )

    assert result.exit_code != 0
    assert "Password rejected" in result.output
    assert "request context" not in result.output
    assert "Traceback" not in result.output
    assert _user("seller2") is None


def test_create_user_refuses_a_duplicate_username(app, runner, admin_user):
    result = runner.invoke(
        args=["create-user", "--username", admin_user.username, "--role", "sales"],
        input="BmShop-4c81de\nBmShop-4c81de\n",
    )

    assert result.exit_code != 0
    assert "already exists" in result.output

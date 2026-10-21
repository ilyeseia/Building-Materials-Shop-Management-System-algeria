"""Authentication: sign in, sign out, password change, language switching."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select

from app.extensions import db
from app.forms import ChangePasswordForm, LoginForm, TOTPForm
from app.i18n import set_locale, translate
from app.models import AccountStatus, User
from app.blueprints.helpers import current_user_id
from app.security import (
    login_limited,
    password_policy_errors,
    reset_login_limit,
    safe_next_url,
)
from app.services import audit
from app.services.session_mgr import SessionManager
from app.services.timeutil import utcnow

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    if form.validate_on_submit():
        username = (form.username.data or "").strip()
        limited, retry_after = login_limited(username)
        if limited:
            audit.record_login_event(username, False, "rate limited")
            db.session.commit()
            flash(translate("auth.too_many_attempts", seconds=retry_after), "danger")
            return render_template("auth/login.html", form=form), 429

        user = db.session.execute(
            select(User).filter_by(username=username)
        ).scalars().first()

        # Generic error message to avoid user enumeration
        if user is None or not user.check_password(form.password.data or ""):
            audit.record_login_event(username, False, "bad credentials")
            db.session.commit()
            flash(translate("auth.invalid_credentials"), "danger")
            return render_template("auth/login.html", form=form), 401

        # Check account lockout
        if user.is_locked:
            audit.record_login_event(username, False, "account locked")
            db.session.commit()
            flash(translate("auth.account_locked"), "danger")
            return render_template("auth/login.html", form=form), 403

        # Check account status
        if not user.is_active or user.status != AccountStatus.ACTIVE:
            audit.record_login_event(username, False, f"inactive status: {user.status}")
            db.session.commit()
            flash(translate("auth.account_disabled"), "danger")
            return render_template("auth/login.html", form=form), 403

        # Check TOTP MFA
        if user.totp_enabled:
            # Store user ID in session for TOTP verification step
            session["pre_2fa_user_id"] = user.id
            return redirect(url_for("auth.verify_totp"))

        # Record successful login
        user.record_login_success()
        reset_login_limit(username)

        # Create session record with a generated session key.  The key is
        # reused when the browser session already carries one, which is safe
        # because create_session() refreshes the existing row instead of
        # inserting a duplicate that would break the unique index.
        session_key = session.get("_session_key") or SessionManager.generate_session_key()
        session["_session_key"] = session_key
        SessionManager.create_session(
            user_id=user.id,
            session_key=session_key,
            ip_address=request.remote_addr,
            user_agent=request.user_agent.string if request.user_agent else None,
        )

        login_user(user, remember=bool(form.remember.data))
        audit.record_login_event(username, True)
        db.session.commit()

        if user.language:
            set_locale(user.language)

        # Check if password change is required
        if user.must_change_password:
            return redirect(url_for("auth.change_password"))

        return redirect(safe_next_url())

    return render_template("auth/login.html", form=form)


@bp.route("/verify-totp", methods=["GET", "POST"])
def verify_totp():
    """Verify TOTP code for MFA."""
    user_id = session.get("pre_2fa_user_id")
    if not user_id:
        return redirect(url_for("auth.login"))

    user = db.session.get(User, user_id)
    if not user or not user.totp_enabled:
        session.pop("pre_2fa_user_id", None)
        return redirect(url_for("auth.login"))

    form = TOTPForm()
    if form.validate_on_submit():
        import pyotp
        totp = pyotp.TOTP(user.totp_secret)
        if totp.verify(form.totp_code.data, valid_window=1):
            # Valid TOTP code
            session.pop("pre_2fa_user_id", None)

            # Record successful login
            user.record_login_success()
            reset_login_limit(user.username)

            # Create session record with a generated session key
            session_key = session.get("_session_key") or SessionManager.generate_session_key()
            session["_session_key"] = session_key
            SessionManager.create_session(
                user_id=user.id,
                session_key=session_key,
                ip_address=request.remote_addr,
                user_agent=request.user_agent.string if request.user_agent else None,
            )

            login_user(user, remember=False)
            audit.record_login_event(user.username, True)
            db.session.commit()

            if user.language:
                set_locale(user.language)

            # Check if password change is required
            if user.must_change_password:
                return redirect(url_for("auth.change_password"))

            return redirect(safe_next_url())
        else:
            audit.record_login_event(user.username, False, "invalid totp")
            db.session.commit()
            flash(translate("auth.invalid_totp"), "danger")

    return render_template("auth/verify_totp.html", form=form)


@bp.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    # Revoke the tracked session.  A missing row must never stop the sign-out:
    # the row can already be gone (a second logout, an administrator revoked
    # it) and the user still has to end up signed out.
    session_key = session.get("_session_key")
    SessionManager.revoke_session_if_present(session_key)
    audit.record("logout", "user", current_user_id(), summary=current_user.username)
    logout_user()
    session.clear()
    # Flask-Login marks the persistent "remember me" cookie for deletion with
    # session["_remember"] = "clear".  session.clear() above wiped that
    # marker, so the cookie survived the sign-out and Flask-Login silently
    # signed the user back in on the next request.  Re-apply the marker so
    # the after_request hook really deletes the cookie.
    session["_remember"] = "clear"
    db.session.commit()
    flash(translate("auth.signed_out"), "success")
    return redirect(url_for("auth.login"))


@bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data or ""):
            flash(translate("auth.wrong_current_password"), "danger")
            return render_template("auth/password.html", form=form), 400
        errors = password_policy_errors(form.new_password.data or "", username=current_user.username)
        if errors:
            for key in errors:
                flash(translate(key), "danger")
            return render_template("auth/password.html", form=form), 400
        current_user.set_password(form.new_password.data)
        # Revoke every other session on a password change.  The session that
        # made the change keeps working; it is identified by the tracked
        # session key, not by Flask's own session id.
        SessionManager.revoke_all_sessions(
            current_user.id, except_session=session.get("_session_key")
        )
        audit.record("password_changed", "user", current_user.id, summary=current_user.username)
        db.session.commit()
        flash(translate("auth.password_changed"), "success")
        return redirect(url_for("dashboard.index"))
    return render_template("auth/password.html", form=form)


@bp.route("/language/<code>")
def language(code: str):
    if not set_locale(code):
        flash(translate("error.not_found"), "warning")
    target = request.referrer
    if target and target.startswith(request.host_url):
        return redirect(target)
    return redirect(url_for("dashboard.index") if current_user.is_authenticated else url_for("auth.login"))
"""Authentication: sign in, sign out, password change, language switching."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select

from app.extensions import db
from app.forms import ChangePasswordForm, LoginForm
from app.i18n import set_locale, translate
from app.models import User
from app.blueprints.helpers import current_user_id
from app.security import (
    login_limited,
    password_policy_errors,
    reset_login_limit,
    safe_next_url,
)
from app.services import audit
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
        if user is None or not user.check_password(form.password.data or ""):
            audit.record_login_event(username, False, "bad credentials")
            db.session.commit()
            flash(translate("auth.invalid_credentials"), "danger")
            return render_template("auth/login.html", form=form), 401
        if not user.is_active:
            audit.record_login_event(username, False, "inactive")
            db.session.commit()
            flash(translate("auth.account_disabled"), "danger")
            return render_template("auth/login.html", form=form), 403

        login_user(user, remember=bool(form.remember.data))
        user.last_login_at = utcnow()
        reset_login_limit(username)
        audit.record_login_event(username, True)
        db.session.commit()
        if user.language:
            set_locale(user.language)
        return redirect(safe_next_url())

    return render_template("auth/login.html", form=form)


@bp.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    audit.record("logout", "user", current_user_id(), summary=current_user.username)
    username = current_user.username
    logout_user()
    session.clear()
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

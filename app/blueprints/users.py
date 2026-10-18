"""User administration."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, render_template_string, url_for
from sqlalchemy import select

from app.extensions import db
from app.forms import UserForm
from app.i18n import translate
from app.models import User
from app.blueprints.helpers import handle_errors, page_args
from app.security import password_policy_errors, requires
from app.services import audit, reporting

bp = Blueprint("users", __name__)


@bp.route("/users")
@requires("users.view", "users.manage", any_of=True)
def index():
    page, per_page = page_args()
    result = reporting.paginate(select(User).order_by(User.username), page, per_page)
    rows = [
        {
            "username": user.username,
            "name": user.full_name or "",
            "role": translate(f"role.{user.role}"),
            "language": (user.language or "").upper(),
            "active": translate("common.yes") if user.is_active else translate("common.no"),
            "last_login": user.last_login_at.strftime("%d/%m/%Y %H:%M") if user.last_login_at else "—",
            "_url": url_for("users.edit", user_id=user.id),
        }
        for user in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="nav.users", rows=rows,
        columns=[
            {"key": "username", "label": "auth.username"},
            {"key": "name", "label": "common.name"},
            {"key": "role", "label": "users.role"},
            {"key": "language", "label": "common.language"},
            {"key": "active", "label": "common.active"},
            {"key": "last_login", "label": "users.last_login"},
        ],
        result=result, search="", search_url=url_for("users.index"),
        create_url=url_for("users.create"),
    )


@bp.route("/users/new", methods=["GET", "POST"])
@requires("users.manage")
@handle_errors
def create():
    form = UserForm()
    if form.validate_on_submit():
        errors = password_policy_errors(form.password.data or "", username=form.username.data)
        if errors:
            for key in errors:
                flash(translate(key), "danger")
        elif db.session.execute(
            select(User).filter_by(username=form.username.data)
        ).scalars().first():
            flash(translate("users.exists"), "danger")
        else:
            user = User(
                username=form.username.data, full_name=form.full_name.data,
                role=form.role.data, phone=form.phone.data,
                language=form.language.data or None, is_active=bool(form.is_active.data),
            )
            user.set_password(form.password.data)
            db.session.add(user)
            audit.record("created", "user", None, summary=user.username)
            db.session.commit()
            flash(translate("common.saved"), "success")
            return redirect(url_for("users.index"))
    return render_template("crud/form.html", form=form, title_key="users.new",
                           cancel_url=url_for("users.index"))


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@requires("users.manage")
@handle_errors
def edit(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        return redirect(url_for("users.index"))
    form = UserForm(obj=user)
    if not form.is_submitted():
        form.language.data = user.language or ""
    if form.validate_on_submit():
        user.username = form.username.data
        user.full_name = form.full_name.data
        user.role = form.role.data
        user.phone = form.phone.data
        user.language = form.language.data or None
        user.is_active = bool(form.is_active.data)
        if form.password.data:
            errors = password_policy_errors(form.password.data, username=user.username)
            if errors:
                for key in errors:
                    flash(translate(key), "danger")
                return render_template("crud/form.html", form=form, title_key="users.edit",
                                       cancel_url=url_for("users.index"))
            user.set_password(form.password.data)
        audit.record("updated", "user", user.id, summary=user.username)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("users.index"))
    return render_template("crud/form.html", form=form, title_key="users.edit",
                           cancel_url=url_for("users.index"))

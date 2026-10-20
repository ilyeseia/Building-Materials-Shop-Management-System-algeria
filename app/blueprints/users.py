"""User administration: enterprise lifecycle, roles, and bulk operations."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import secrets
from typing import Optional

from flask import Blueprint, flash, redirect, render_template, request, url_for, current_app, session
from flask_login import current_user, login_required
from sqlalchemy import select, func, desc

from app.extensions import db
from app.forms import UserForm, RoleForm
from app.i18n import translate
from app.models import User, Warehouse, RoleDefinition, AuditEvent, UserSession
from app.models.security import ALL_ROLES
from app.blueprints.helpers import handle_errors, page_args
from app.security import password_policy_errors, requires
from app.services import audit, reporting
from app.services.user_management import UserManagement
from app.services.iam import IAMService
from app.services.session_mgr import SessionManager

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
            "status": user.status or "active",
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
            {"key": "status", "label": "users.status"},
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
    form.branch_id.choices = [(0, "—")] + [
        (w.id, w.name_ar) for w in db.session.execute(select(Warehouse)).scalars()
    ]
    form.status.choices = [
        ("active", "active"),
        ("inactive", "inactive"),
        ("suspended", "suspended"),
    ]
    # Add custom roles to role choices
    custom_roles = IAMService.list_roles()
    custom_role_codes = [r.code for r in custom_roles if not r.is_system]
    form.role.choices = [(role, f"role.{role}") for role in sorted(ALL_ROLES + tuple(custom_role_codes))]
    
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
                username=form.username.data,
                full_name=form.full_name.data,
                email=form.email.data or None,
                role=form.role.data,
                branch_id=form.branch_id.data or None,
                status=form.status.data or "active",
                phone=form.phone.data,
                language=form.language.data or None,
                is_active=bool(form.is_active.data),
            )
            user.set_password(form.password.data)
            if form.must_change_password.data:
                user.must_change_password = True
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
    form.branch_id.choices = [(0, "—")] + [
        (w.id, w.name_ar) for w in db.session.execute(select(Warehouse)).scalars()
    ]
    form.status.choices = [
        ("active", "active"),
        ("inactive", "inactive"),
        ("suspended", "suspended"),
    ]
    # Add custom roles to role choices
    custom_roles = IAMService.list_roles()
    custom_role_codes = [r.code for r in custom_roles if not r.is_system]
    form.role.choices = [(role, f"role.{role}") for role in sorted(ALL_ROLES + tuple(custom_role_codes))]
    
    if not form.is_submitted():
        form.language.data = user.language or ""
        form.branch_id.data = user.branch_id or 0
        form.status.data = user.status or "active"
        form.must_change_password.data = user.must_change_password
    if form.validate_on_submit():
        user.username = form.username.data
        user.full_name = form.full_name.data
        user.email = form.email.data or None
        user.role = form.role.data
        user.branch_id = form.branch_id.data or None
        user.status = form.status.data or "active"
        user.phone = form.phone.data
        user.language = form.language.data or None
        user.is_active = bool(form.is_active.data)
        user.must_change_password = bool(form.must_change_password.data)
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


# ======================================================================
# Role & Permission Administration
# ======================================================================

@bp.route("/users/roles")
@requires("users.manage_roles")
def roles_index():
    """List all role definitions (system + custom)."""
    roles = IAMService.list_roles()
    rows = [
        {
            "code": role.code,
            "name_ar": role.name_ar,
            "name_fr": role.name_fr,
            "name_en": role.name_en,
            "is_system": role.is_system,
            "perm_count": len(role.get_permissions()),
            "_url": url_for("users.roles_edit", code=role.code),
        }
        for role in roles
    ]
    return render_template(
        "crud/list.html", title_key="users.roles_title", rows=rows,
        columns=[
            {"key": "code", "label": "users.role_code"},
            {"key": "name_ar", "label": "users.name_ar"},
            {"key": "name_fr", "label": "users.name_fr"},
            {"key": "name_en", "label": "users.name_en"},
            {"key": "is_system", "label": "common.system"},
            {"key": "perm_count", "label": "users.permissions_count"},
        ],
        result={"items": roles, "total": len(roles), "page": 1, "per_page": 100, "pages": 1},
        search="", search_url=url_for("users.roles_index"),
        create_url=url_for("users.roles_new"),
    )


@bp.route("/users/roles/new", methods=["GET", "POST"])
@requires("users.manage_roles")
@handle_errors
def roles_new():
    form = RoleForm()
    # Populate permissions field with all available granular permissions
    from app.models.security import GRANULAR_PERMISSIONS
    all_perms = "\n".join(sorted(GRANULAR_PERMISSIONS.keys()))
    if not form.is_submitted():
        form.permissions.data = all_perms
    if form.validate_on_submit():
        perms = [p.strip() for p in (form.permissions.data or "").split("\n") if p.strip()]
        role = IAMService.create_or_update_role(
            code=form.code.data.strip(),
            name_ar=form.name_ar.data.strip(),
            name_fr=form.name_fr.data.strip(),
            name_en=form.name_en.data.strip(),
            permissions=perms,
            description=form.description.data or "",
        )
        audit.record("created", "role", role.id, summary=role.code)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("users.roles_index"))
    return render_template("crud/form.html", form=form, title_key="users.role_new",
                           cancel_url=url_for("users.roles_index"))


@bp.route("/users/roles/<code>/edit", methods=["GET", "POST"])
@requires("users.manage_roles")
@handle_errors
def roles_edit(code: str):
    role = db.session.execute(select(RoleDefinition).filter_by(code=code)).scalar_one_or_none()
    if role is None:
        flash(translate("error.not_found"), "danger")
        return redirect(url_for("users.roles_index"))
    form = RoleForm(obj=role)
    from app.models.security import GRANULAR_PERMISSIONS
    all_perms = "\n".join(sorted(GRANULAR_PERMISSIONS.keys()))
    if not form.is_submitted():
        form.permissions.data = "\n".join(sorted(role.get_permissions()))
    if form.validate_on_submit():
        perms = [p.strip() for p in (form.permissions.data or "").split("\n") if p.strip()]
        role.code = form.code.data.strip()
        role.name_ar = form.name_ar.data.strip()
        role.name_fr = form.name_fr.data.strip()
        role.name_en = form.name_en.data.strip()
        role.description = form.description.data or ""
        role.set_permissions(perms)
        audit.record("updated", "role", role.id, summary=role.code)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("users.roles_index"))
    return render_template("crud/form.html", form=form, title_key="users.role_edit",
                           cancel_url=url_for("users.roles_index"))


# ======================================================================
# Active Sessions Management
# ======================================================================

@bp.route("/users/<int:user_id>/sessions")
@requires("users.sessions")
def user_sessions(user_id: int):
    """View and revoke active sessions for a user."""
    user = db.session.get(User, user_id)
    if not user:
        flash(translate("error.not_found"), "danger")
        return redirect(url_for("users.index"))

    sessions = SessionManager.get_user_sessions(user_id)
    rows = [
        {
            "session_key": s.session_key[:12] + "...",
            "ip_address": s.ip_address or "—",
            "user_agent": (s.user_agent or "—")[:50],
            "created_at": s.created_at.strftime("%d/%m/%Y %H:%M"),
            "last_activity": s.last_activity_at.strftime("%d/%m/%Y %H:%M"),
            "is_active": translate("common.yes") if s.is_active else translate("common.no"),
            "_revoke_url": url_for("users.revoke_session", session_key=s.session_key) if s.is_active else None,
        }
        for s in sessions
    ]
    return render_template(
        "crud/list.html", title_key="users.sessions_title", rows=rows,
        columns=[
            {"key": "session_key", "label": "users.session_id"},
            {"key": "ip_address", "label": "users.ip"},
            {"key": "user_agent", "label": "users.user_agent"},
            {"key": "created_at", "label": "users.created"},
            {"key": "last_activity", "label": "users.last_activity"},
            {"key": "is_active", "label": "common.active"},
        ],
        result={"items": sessions, "total": len(sessions), "page": 1, "per_page": 100, "pages": 1},
        search="", search_url=url_for("users.user_sessions", user_id=user_id),
        create_url=None,
        extra_context={"target_user": user},
    )


@bp.route("/users/sessions/<session_key>/revoke", methods=["POST"])
@requires("users.sessions")
def revoke_session(session_key: str):
    """Revoke a specific session."""
    try:
        SessionManager.revoke_session(session_key)
        audit.record("session_revoked", "session", None, summary=session_key[:12])
        db.session.commit()
        flash(translate("common.revoked"), "success")
    except Exception as e:
        flash(translate("error.server"), "danger")
        current_app.logger.error(f"Failed to revoke session: {e}")
    return redirect(request.referrer or url_for("users.index"))


# ======================================================================
# Self-Service Profile & Security
# ======================================================================

@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """Self-service profile page."""
    form = UserForm(obj=current_user)
    form.branch_id.choices = [(0, "—")] + [
        (w.id, w.name_ar) for w in db.session.execute(select(Warehouse)).scalars()
    ]
    form.status.choices = [
        ("active", "active"),
        ("inactive", "inactive"),
        ("suspended", "suspended"),
    ]
    # Disable fields that users shouldn't edit themselves
    form.username.render_kw = {"readonly": True}
    form.role.render_kw = {"disabled": True}
    form.is_active.render_kw = {"disabled": True}
    form.status.render_kw = {"disabled": True}
    form.must_change_password.render_kw = {"disabled": True}

    if not form.is_submitted():
        form.language.data = current_user.language or ""
        form.branch_id.data = current_user.branch_id or 0
        form.status.data = current_user.status or "active"

    if form.validate_on_submit():
        current_user.full_name = form.full_name.data
        current_user.email = form.email.data or None
        current_user.phone = form.phone.data
        current_user.language = form.language.data or None
        if form.password.data:
            errors = password_policy_errors(form.password.data, username=current_user.username)
            if errors:
                for key in errors:
                    flash(translate(key), "danger")
                return render_template("users/profile.html", form=form, title_key="users.profile_title")
            current_user.set_password(form.password.data)
        audit.record("profile_updated", "user", current_user.id, summary=current_user.username)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("users.profile"))

    # Get active sessions for display
    sessions = SessionManager.get_active_sessions(current_user.id)
    return render_template("users/profile.html", form=form, title_key="users.profile_title",
                           sessions=sessions, totp_enabled=current_user.totp_enabled)


@bp.route("/profile/security", methods=["GET", "POST"])
@login_required
def profile_security():
    """Security settings: TOTP setup, backup codes, revoke sessions."""
    import pyotp
    import qrcode
    import io
    import base64

    qr_code = None
    backup_codes = None

    if request.method == "POST":
        action = request.form.get("action")

        if action == "enable_totp":
            if not current_user.totp_enabled:
                secret = pyotp.random_base32()
                current_user.totp_secret = secret
                # Generate backup codes
                backup_codes = [secrets.token_hex(4).upper() for _ in range(10)]
                current_user.totp_backup_codes = json.dumps(backup_codes)
                db.session.flush()

                # Generate QR code
                totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(
                    name=current_user.username, issuer_name="Building Materials Shop"
                )
                qr_img = qrcode.make(totp_uri)
                buf = io.BytesIO()
                qr_img.save(buf, format="PNG")
                qr_code = base64.b64encode(buf.getvalue()).decode()
                flash(translate("users.totp_setup_complete"), "success")

        elif action == "verify_totp":
            code = request.form.get("totp_code", "").strip()
            if current_user.totp_secret:
                totp = pyotp.TOTP(current_user.totp_secret)
                if totp.verify(code, valid_window=1):
                    current_user.totp_enabled = True
                    db.session.commit()
                    flash(translate("users.totp_enabled"), "success")
                else:
                    flash(translate("auth.invalid_totp"), "danger")

        elif action == "disable_totp":
            current_user.totp_enabled = False
            current_user.totp_secret = None
            current_user.totp_backup_codes = None
            db.session.commit()
            flash(translate("users.totp_disabled"), "success")

        elif action == "revoke_all":
            SessionManager.revoke_all_sessions(current_user.id, except_session=session.get("_session_key"))
            db.session.commit()
            flash(translate("users.all_sessions_revoked"), "success")

    # Generate QR code for display if TOTP not enabled but secret exists
    if current_user.totp_secret and not current_user.totp_enabled:
        totp_uri = pyotp.totp.TOTP(current_user.totp_secret).provisioning_uri(
            name=current_user.username, issuer_name="Building Materials Shop"
        )
        qr_img = qrcode.make(totp_uri)
        buf = io.BytesIO()
        qr_img.save(buf, format="PNG")
        qr_code = base64.b64encode(buf.getvalue()).decode()

    backup_codes_list = []
    if current_user.totp_backup_codes:
        try:
            backup_codes_list = json.loads(current_user.totp_backup_codes)
        except Exception:
            pass

    return render_template("users/security.html", title_key="users.security_title",
                           qr_code=qr_code, backup_codes=backup_codes_list,
                           totp_enabled=current_user.totp_enabled)


# ======================================================================
# Bulk Operations
# ======================================================================

@bp.route("/users", methods=["POST"])
@requires("users.manage")
def bulk_action():
    """Handle bulk actions on users: activate, deactivate, change role."""
    action = request.form.get("action")
    user_ids = request.form.getlist("user_ids", type=int)
    new_role = request.form.get("new_role")

    if not user_ids:
        flash(translate("users.no_users_selected"), "warning")
        return redirect(url_for("users.index"))

    users = db.session.execute(select(User).filter(User.id.in_(user_ids))).scalars().all()

    if action == "bulk_activate":
        for user in users:
            if user.status != "active":
                user.status = "active"
                audit.record("activated", "user", user.id, summary=user.username)
        db.session.commit()
        flash(translate("users.bulk_activated").format(count=len(users)), "success")

    elif action == "bulk_deactivate":
        for user in users:
            if user.status != "inactive":
                user.status = "inactive"
                audit.record("deactivated", "user", user.id, summary=user.username)
        db.session.commit()
        flash(translate("users.bulk_deactivated").format(count=len(users)), "success")

    elif action == "bulk_change_role" and new_role:
        for user in users:
            if user.role != new_role:
                user.role = new_role
                audit.record("role_changed", "user", user.id, summary=f"{user.username} -> {new_role}")
        db.session.commit()
        flash(translate("users.bulk_role_changed").format(count=len(users), role=new_role), "success")

    else:
        flash(translate("error.invalid_action"), "danger")

    return redirect(url_for("users.index"))
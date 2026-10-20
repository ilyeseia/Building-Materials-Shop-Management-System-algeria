"""Tests for Enterprise IAM frontend features: roles, sessions, profile, security."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Role, User, RoleDefinition, UserSession
from app.services.iam import IAMService
from app.services.session_mgr import SessionManager


# ---------------------------------------------------------------------------
# Role & Permission Administration
# ---------------------------------------------------------------------------

def test_admin_can_access_roles_page(client, admin_user, login):
    login(admin_user)
    response = client.get("/users/roles")
    assert response.status_code == 200
    # Page title should be translated
    assert "الأدوار والصلاحيات".encode("utf-8") in response.data or b"users.roles_title" in response.data


def test_viewer_cannot_access_roles_page(client, viewer_user, login):
    login(viewer_user)
    response = client.get("/users/roles")
    assert response.status_code == 403


def test_admin_can_create_custom_role(client, admin_user, login):
    login(admin_user)
    response = client.post("/users/roles/new", data={
        "code": "test_role",
        "name_ar": "دور تجريبي",
        "name_fr": "Rôle test",
        "name_en": "Test Role",
        "permissions": "dashboard.view\ncatalog.view",
        "description": "Test role"
    }, follow_redirects=True)
    assert response.status_code == 200
    role = db.session.query(RoleDefinition).filter_by(code="test_role").one_or_none()
    assert role is not None
    assert "dashboard.view" in role.get_permissions()
    assert "catalog.view" in role.get_permissions()


def test_admin_can_edit_custom_role(client, admin_user, login):
    login(admin_user)
    # Create role first
    role = IAMService.create_or_update_role(
        code="edit_test",
        name_ar="تعديل",
        name_fr="Édition",
        name_en="Edit",
        permissions=["dashboard.view"],
        description="Original"
    )
    db.session.commit()

    response = client.post(f"/users/roles/{role.code}/edit", data={
        "code": "edit_test",
        "name_ar": "معدل",
        "name_fr": "Modifié",
        "name_en": "Modified",
        "permissions": "dashboard.view\ncatalog.view\nsales.view",
        "description": "Updated"
    }, follow_redirects=True)
    assert response.status_code == 200
    role = db.session.query(RoleDefinition).filter_by(code="edit_test").one()
    assert "sales.view" in role.get_permissions()
    assert "catalog.view" in role.get_permissions()


def test_custom_role_appears_in_user_form(client, admin_user, login):
    login(admin_user)
    # Create custom role
    IAMService.create_or_update_role(
        code="custom_sales",
        name_ar="مبيعات مخصصة",
        name_fr="Ventes personnalisées",
        name_en="Custom Sales",
        permissions=["sales.view", "sales.manage", "customers.view", "dashboard.view"],
        description="Custom sales role"
    )
    db.session.commit()

    response = client.get("/users/new")
    assert response.status_code == 200
    # Custom role should appear in the role dropdown
    assert b"custom_sales" in response.data


def test_user_with_custom_role_gets_correct_permissions(client, admin_user, login):
    login(admin_user)
    # Create custom role
    IAMService.create_or_update_role(
        code="limited_ai",
        name_ar="ذكاء اصطناعي محدود",
        name_fr="IA limitée",
        name_en="Limited AI",
        permissions=["dashboard.view", "ai.view"],
        description="AI only"
    )
    db.session.commit()

    # Create user with custom role (use strong password, include all required fields)
    response = client.post("/users/new", data={
        "username": "aiuser",
        "full_name": "AI User",
        "email": "ai@example.com",
        "phone": "",
        "language": "ar",
        "branch_id": "0",
        "status": "active",
        "role": "limited_ai",
        "password": "StrongPass-2026!",
        "confirm_password": "StrongPass-2026!",
        "is_active": "y",
        "must_change_password": "",
    }, follow_redirects=True)
    assert response.status_code == 200

    user = db.session.query(User).filter_by(username="aiuser").one()
    assert user.has_permission("dashboard.view") is True
    assert user.has_permission("ai.view") is True
    assert user.has_permission("sales.view") is False
    assert user.has_permission("users.manage") is False


# ---------------------------------------------------------------------------
# Active Sessions Management
# ---------------------------------------------------------------------------

def test_admin_can_view_user_sessions(client, admin_user, login):
    login(admin_user)
    response = client.get(f"/users/{admin_user.id}/sessions")
    assert response.status_code == 200
    assert "الجلسات النشطة".encode("utf-8") in response.data or b"sessions_title" in response.data


def test_viewer_cannot_view_user_sessions(client, viewer_user, login):
    login(viewer_user)
    response = client.get(f"/users/{viewer_user.id}/sessions")
    assert response.status_code == 403


def test_admin_can_revoke_session(client, admin_user, login):
    login(admin_user)
    # Create a test session
    session_key = SessionManager.generate_session_key()
    session = SessionManager.create_session(
        user_id=admin_user.id,
        session_key=session_key,
        ip_address="192.168.1.100",
        user_agent="Test Agent"
    )
    db.session.commit()

    response = client.post(f"/users/sessions/{session.session_key}/revoke", follow_redirects=True)
    assert response.status_code == 200

    session = db.session.get(UserSession, session.id)
    assert session.is_active is False


def test_session_revocation_is_audited(client, admin_user, login):
    from app.models import AuditEvent
    login(admin_user)
    session_key = SessionManager.generate_session_key()
    session = SessionManager.create_session(
        user_id=admin_user.id,
        session_key=session_key,
        ip_address="192.168.1.100",
        user_agent="Test Agent"
    )
    db.session.commit()

    client.post(f"/users/sessions/{session.session_key}/revoke", follow_redirects=True)
    db.session.commit()

    event = db.session.query(AuditEvent).filter_by(action="session_revoked").first()
    assert event is not None
    assert event.entity_type == "session"


# ---------------------------------------------------------------------------
# Self-Service Profile & Security
# ---------------------------------------------------------------------------

def test_user_can_access_own_profile(client, admin_user, login):
    login(admin_user)
    response = client.get("/profile")
    assert response.status_code == 200
    assert "ملفي الشخصي".encode("utf-8") in response.data or b"profile_title" in response.data


def test_user_can_update_own_profile(client, admin_user, login):
    login(admin_user)
    response = client.post("/profile", data={
        "full_name": "Updated Name",
        "email": "updated@example.com",
        "phone": "+213 555 123456",
        "language": "fr",
    }, follow_redirects=True)
    assert response.status_code == 200

    user = db.session.get(User, admin_user.id)
    assert user.full_name == "Updated Name"
    assert user.email == "updated@example.com"
    assert user.language == "fr"


def test_user_cannot_change_own_role_or_status(client, admin_user, login):
    login(admin_user)
    original_role = admin_user.role
    original_status = admin_user.status

    response = client.post("/profile", data={
        "role": "viewer",
        "status": "inactive",
        "full_name": "Admin User",
    }, follow_redirects=True)
    assert response.status_code == 200

    user = db.session.get(User, admin_user.id)
    assert user.role == original_role
    assert user.status == original_status


def test_user_can_change_own_password(client, admin_user, login):
    login(admin_user)
    response = client.post("/profile", data={
        "full_name": "Admin User",
        "email": "admin@example.com",
        "phone": "",
        "language": "ar",
        "branch_id": "0",
        "password": "NewPass-2026!",
        "confirm_password": "NewPass-2026!",
    }, follow_redirects=True)
    assert response.status_code == 200

    user = db.session.get(User, admin_user.id)
    assert user.check_password("NewPass-2026!") is True


def test_user_can_access_security_page(client, admin_user, login):
    login(admin_user)
    response = client.get("/profile/security")
    assert response.status_code == 200
    assert "إعدادات الأمان".encode("utf-8") in response.data or b"security_title" in response.data


def test_totp_setup_generates_qr_and_backup_codes(client, admin_user, login):
    login(admin_user)
    # Enable TOTP
    with patch("pyotp.random_base32", return_value="JBSWY3DPEHPK3PXP"):
        response = client.post("/profile/security", data={"action": "enable_totp"}, follow_redirects=True)
    assert response.status_code == 200

    user = db.session.get(User, admin_user.id)
    assert user.totp_secret is not None
    assert user.totp_backup_codes is not None
    codes = json.loads(user.totp_backup_codes)
    assert len(codes) == 10


def test_totp_verification_enables_2fa(client, admin_user, login):
    login(admin_user)
    import pyotp
    # Set up TOTP secret
    admin_user.totp_secret = "JBSWY3DPEHPK3PXP"
    db.session.commit()

    totp = pyotp.TOTP("JBSWY3DPEHPK3PXP")
    valid_code = totp.now()

    response = client.post("/profile/security", data={
        "action": "verify_totp",
        "totp_code": valid_code
    }, follow_redirects=True)
    assert response.status_code == 200

    user = db.session.get(User, admin_user.id)
    assert user.totp_enabled is True


def test_totp_disable_removes_secret_and_backup_codes(client, admin_user, login):
    login(admin_user)
    admin_user.totp_secret = "JBSWY3DPEHPK3PXP"
    admin_user.totp_enabled = True
    admin_user.totp_backup_codes = json.dumps(["CODE1", "CODE2"])
    db.session.commit()

    response = client.post("/profile/security", data={"action": "disable_totp"}, follow_redirects=True)
    assert response.status_code == 200

    user = db.session.get(User, admin_user.id)
    assert user.totp_enabled is False
    assert user.totp_secret is None
    assert user.totp_backup_codes is None


def test_user_can_revoke_all_other_sessions(client, admin_user, login):
    login(admin_user)
    # Create another session
    other_session_key = SessionManager.generate_session_key()
    other_session = SessionManager.create_session(
        user_id=admin_user.id,
        session_key=other_session_key,
        ip_address="192.168.1.200",
        user_agent="Other Device"
    )
    db.session.commit()

    response = client.post("/profile/security", data={"action": "revoke_all"}, follow_redirects=True)
    assert response.status_code == 200

    other = db.session.get(UserSession, other_session.id)
    assert other.is_active is False


# ---------------------------------------------------------------------------
# Audit Log (via existing audit blueprint)
# ---------------------------------------------------------------------------

def test_audit_log_accessible_to_auditors(client, admin_user, login):
    login(admin_user)
    response = client.get("/audit")
    assert response.status_code == 200


def test_audit_log_filters_work(client, admin_user, login):
    login(admin_user)
    response = client.get("/audit?action=login")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# AI Workspace IAM Enforcement
# ---------------------------------------------------------------------------

def test_ai_tools_filtered_by_user_role(client, admin_user, login):
    """AI workspace should only show tools user has permission for."""
    login(admin_user)
    from app.services.ai import available_tools
    with client.application.test_request_context():
        from flask import session
        session["language"] = "en"
        tools = available_tools(admin_user)
    # Admin has all permissions
    assert len(tools) == 8


def test_ai_tool_execution_rechecks_permission(client, admin_user, login):
    """execute_tool should re-check permission server-side."""
    login(admin_user)
    from app.services.ai import execute_tool
    with client.application.test_request_context():
        from flask import session
        session["language"] = "en"
        ok, payload, err = execute_tool(admin_user, "business_overview", {})
        assert ok is True
        assert err is None


def test_custom_role_ai_user_only_sees_allowed_tools(client, admin_user, login):
    login(admin_user)
    # Create custom role with limited permissions
    IAMService.create_or_update_role(
        code="ai_basic",
        name_ar="ذكاء أساسي",
        name_fr="IA basique",
        name_en="Basic AI",
        permissions=["dashboard.view", "ai.view"],
        description="Basic AI access"
    )
    db.session.commit()

    # Create user with custom role
    ai_user = User(username="ai_basic_user", role="ai_basic", password_hash="x")
    db.session.add(ai_user)
    db.session.commit()

    from app.services.ai import available_tools
    with client.application.test_request_context():
        from flask import session
        session["language"] = "en"
        tools = available_tools(ai_user)
    # Should only see business_overview (requires dashboard.view)
    assert len(tools) == 1
    assert tools[0]["name"] == "business_overview"


def test_ai_workspace_page_loads_for_authenticated_users(client, admin_user, login):
    login(admin_user)
    response = client.get("/ai")
    assert response.status_code == 200
    # Should have CSRF form and tool list - check for Arabic "اسأل" or English "Ask"
    assert "اسأل".encode("utf-8") in response.data or b"Ask" in response.data or b"ai.ask" in response.data


# ---------------------------------------------------------------------------
# Bulk Operations
# ---------------------------------------------------------------------------

def test_bulk_activate_users(client, admin_user, login):
    login(admin_user)
    # Create inactive users
    for i in range(3):
        u = User(username=f"inactive{i}", role=Role.VIEWER, password_hash="x", status="inactive")
        db.session.add(u)
    db.session.commit()

    user_ids = [u.id for u in db.session.query(User).filter(User.username.like("inactive%")).all()]
    response = client.post("/users", data={
        "action": "bulk_activate",
        "user_ids": user_ids
    }, follow_redirects=True)
    assert response.status_code == 200

    for uid in user_ids:
        user = db.session.get(User, uid)
        assert user.status == "active"


def test_bulk_deactivate_users(client, admin_user, login):
    login(admin_user)
    # Create active users
    for i in range(3):
        u = User(username=f"active{i}", role=Role.VIEWER, password_hash="x", status="active")
        db.session.add(u)
    db.session.commit()

    user_ids = [u.id for u in db.session.query(User).filter(User.username.like("active%")).all()]
    response = client.post("/users", data={
        "action": "bulk_deactivate",
        "user_ids": user_ids
    }, follow_redirects=True)
    assert response.status_code == 200

    for uid in user_ids:
        user = db.session.get(User, uid)
        assert user.status == "inactive"


def test_bulk_change_role(client, admin_user, login):
    login(admin_user)
    # Create users
    for i in range(2):
        u = User(username=f"rolechange{i}", role=Role.VIEWER, password_hash="x", status="active")
        db.session.add(u)
    db.session.commit()

    user_ids = [u.id for u in db.session.query(User).filter(User.username.like("rolechange%")).all()]
    response = client.post("/users", data={
        "action": "bulk_change_role",
        "user_ids": user_ids,
        "new_role": "sales"
    }, follow_redirects=True)
    assert response.status_code == 200

    for uid in user_ids:
        user = db.session.get(User, uid)
        assert user.role == "sales"
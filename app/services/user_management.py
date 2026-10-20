"""User lifecycle management: create, edit, activate/deactivate, bulk operations, and security checks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets
import string
from typing import Optional, List, Dict, Any

from flask import current_app, flash, redirect, url_for
from sqlalchemy import select, and_, or_
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AccountStatus,
    Role,
    RoleDefinition,
    User,
    UserSession,
    PasswordResetToken,
    Warehouse,
)
from app.services import audit, reporting
from app.services.errors import BusinessError
from app.i18n import translate


def _get_current_actor():
    """Return current user object safely, or None if no request context."""
    try:
        from flask_login import current_user
        if current_user.is_authenticated:
            return current_user
    except Exception:
        pass
    return None


def _current_user_id():
    """Return current user ID safely, or None if no request context."""
    actor = _get_current_actor()
    return actor.id if actor else None


class UserManagement:
    @staticmethod
    def generate_password(length: int = 12) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(secrets.choice(chars) for _ in range(length))

    @staticmethod
    def generate_reset_token(user: User) -> PasswordResetToken:
        token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        token_hash = secrets.token_hex(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        token_record = PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        db.session.add(token_record)
        db.session.flush()
        return token_record

    @staticmethod
    def validate_user_create(data: Dict[str, Any]) -> None:
        if not data.get("username") or len(data["username"]) < 3:
            raise BusinessError("username_too_short")
        if not data.get("full_name") or len(data["full_name"]) < 3:
            raise BusinessError("full_name_too_short")
        if not data.get("role") or data["role"] not in Role.__dict__.values():
            raise BusinessError("invalid_role")
        if data.get("email") and not UserManagement.validate_email(data["email"]):
            raise BusinessError("invalid_email")

    @staticmethod
    def validate_email(email: str) -> bool:
        return "@" in email and "." in email.split("@")[-1]

    @staticmethod
    def create_user(
        username: str,
        full_name: str,
        email: Optional[str] = None,
        role: str = Role.VIEWER,
        branch_id: Optional[int] = None,
        password: Optional[str] = None,
        is_active: bool = True,
        status: str = AccountStatus.ACTIVE,
    ) -> User:
        if password is None:
            password = UserManagement.generate_password()

        # Check for existing username
        existing = db.session.execute(
            select(User).filter_by(username=username)
        ).scalar()
        if existing:
            raise BusinessError("username_taken")

        # Check for existing email
        if email:
            existing = db.session.execute(
                select(User).filter_by(email=email)
            ).scalar()
            if existing:
                raise BusinessError("email_taken")

        # Create user
        user = User(
            username=username,
            full_name=full_name,
            email=email,
            role=role,
            branch_id=branch_id,
            is_active=is_active,
            status=status,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        # Audit
        audit.record(
            action="user_created",
            entity_type="user",
            entity_id=user.id,
            actor=_get_current_actor(),
            summary=f"Created user {user.username}",
        )
        return user

    @staticmethod
    def get_user(user_id: int) -> User:
        user = db.session.execute(select(User).filter_by(id=user_id)).scalar_one_or_none()
        if not user:
            raise BusinessError("user_not_found")
        return user

    @staticmethod
    def get_user_by_username(username: str) -> User:
        user = db.session.execute(select(User).filter_by(username=username)).scalar_one_or_none()
        if not user:
            raise BusinessError("user_not_found")
        return user

    @staticmethod
    def get_user_by_email(email: str) -> User:
        user = db.session.execute(select(User).filter_by(email=email)).scalar_one_or_none()
        if not user:
            raise BusinessError("user_not_found")
        return user

    @staticmethod
    def list_users(
        role: Optional[str] = None,
        status: Optional[str] = None,
        branch_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[List[User], int]:
        query = select(User)
        if role:
            query = query.filter(User.role == role)
        if status:
            query = query.filter(User.status == status)
        if branch_id:
            query = query.filter(User.branch_id == branch_id)

        paginated = db.session.execute(query.order_by(User.id).offset((page - 1) * per_page).limit(per_page)).scalars()
        total = db.session.execute(query.count()).scalar()
        return list(paginated), total

    @staticmethod
    def update_user(user_id: int, data: Dict[str, Any]) -> User:
        user = UserManagement.get_user(user_id)
        if data.get("username") and data["username"] != user.username:
            if db.session.execute(select(User).filter(User.username == data["username"])).scalar():
                raise BusinessError("username_taken")
            user.username = data["username"]
        if data.get("full_name") and data["full_name"] != user.full_name:
            user.full_name = data["full_name"]
        if data.get("email") and data["email"] != user.email:
            if UserManagement.validate_email(data["email"]) and db.session.execute(select(User).filter(User.email == data["email"])).scalar():
                raise BusinessError("email_taken")
            user.email = data["email"]
        if data.get("role") and data["role"] != user.role:
            user.role = data["role"]
        if data.get("branch_id") and data["branch_id"] != user.branch_id:
            user.branch_id = data["branch_id"]
        if data.get("language") and data["language"] != user.language:
            user.language = data["language"]
        if data.get("is_active") is not None and data["is_active"] != user.is_active:
            user.is_active = data["is_active"]
        if data.get("status") and data["status"] != user.status:
            user.status = data["status"]

        # Audit
        audit.record(
            action="user_updated",
            entity_type="user",
            entity_id=user.id,
            actor=_get_current_actor(),
            summary=f"Updated user {user.username}",
        )
        return user

    @staticmethod
    def deactivate_user(user_id: int) -> User:
        user = UserManagement.get_user(user_id)
        if user.status == AccountStatus.INACTIVE:
            raise BusinessError("user_already_inactive")
        user.status = AccountStatus.INACTIVE
        user.deactivated_at = datetime.now(timezone.utc)
        db.session.flush()

        # Audit
        audit.record(
            action="user_deactivated",
            entity_type="user",
            entity_id=user.id,
            actor=_get_current_actor(),
            summary=f"Deactivated user {user.username}",
        )
        return user

    @staticmethod
    def activate_user(user_id: int) -> User:
        user = UserManagement.get_user(user_id)
        if user.status == AccountStatus.ACTIVE:
            raise BusinessError("user_already_active")
        user.status = AccountStatus.ACTIVE
        user.deactivated_at = None
        db.session.flush()

        # Audit
        audit.record(
            action="user_activated",
            entity_type="user",
            entity_id=user.id,
            actor=_get_current_actor(),
            summary=f"Activated user {user.username}",
        )
        return user

    @staticmethod
    def delete_user(user_id: int) -> None:
        user = UserManagement.get_user(user_id)
        if user.is_admin:
            raise BusinessError("cannot_delete_admin")
        if user.status == AccountStatus.ACTIVE:
            user.status = AccountStatus.INACTIVE
            user.deactivated_at = datetime.now(timezone.utc)
        else:
            db.session.delete(user)
        db.session.flush()

        # Audit
        audit.record(
            action="user_soft_deleted",
            entity_type="user",
            entity_id=user.id,
            actor=_get_current_actor(),
            summary=f"Soft-deleted user {user.username}",
        )

    @staticmethod
    def bulk_activate_users(user_ids: List[int]) -> Dict[str, int]:
        results = {"success": 0, "failed": 0}
        for user_id in user_ids:
            try:
                UserManagement.activate_user(user_id)
                results["success"] += 1
            except Exception as e:
                results["failed"] += 1
                current_app.logger.error(f"Failed to activate user {user_id}: {e}")
        return results

    @staticmethod
    def bulk_deactivate_users(user_ids: List[int]) -> Dict[str, int]:
        results = {"success": 0, "failed": 0}
        for user_id in user_ids:
            try:
                UserManagement.deactivate_user(user_id)
                results["success"] += 1
            except Exception as e:
                results["failed"] += 1
                current_app.logger.error(f"Failed to deactivate user {user_id}: {e}")
        return results

    @staticmethod
    def bulk_change_role(user_ids: List[int], new_role: str) -> Dict[str, int]:
        results = {"success": 0, "failed": 0}
        for user_id in user_ids:
            try:
                user = UserManagement.get_user(user_id)
                user.role = new_role
                db.session.flush()
                results["success"] += 1
                audit.record(
                    action="user_role_changed",
                    entity_type="user",
                    entity_id=user.id,
                    actor=_get_current_actor(),
                    summary=f"Changed role of user {user.username} to {new_role}",
                )
            except Exception as e:
                results["failed"] += 1
                current_app.logger.error(f"Failed to change role for user {user_id}: {e}")
        return results

    @staticmethod
    def get_user_sessions(user_id: int) -> List[UserSession]:
        return list(db.session.execute(select(UserSession).filter_by(user_id=user_id)).scalars())

    @staticmethod
    def revoke_session(session_key: str) -> UserSession:
        session = db.session.execute(select(UserSession).filter_by(session_key=session_key)).scalar_one_or_none()
        if not session:
            raise BusinessError("session_not_found")
        session.is_active = False
        db.session.flush()
        return session

    @staticmethod
    def revoke_all_sessions(user_id: int) -> int:
        count = db.session.execute(
            db.update(UserSession).where(UserSession.user_id == user_id, UserSession.is_active == True)
            .values(is_active=False)
        ).rowcount
        db.session.flush()
        return count

    @staticmethod
    def generate_password_reset_link(user: User) -> str:
        token = UserManagement.generate_reset_token(user)
        db.session.commit()
        return url_for("auth.reset_password", token=token.token_hash, _external=True)
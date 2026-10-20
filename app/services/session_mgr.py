"""Session management: tracking, revocation, and security checks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, List
import secrets

from flask import current_app, request
from sqlalchemy import select, update

from app.extensions import db
from app.models import User, UserSession
from app.services.errors import BusinessError


class SessionManager:
    @staticmethod
    def generate_session_key() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def create_session(user_id: int, session_key: str, ip_address: str, user_agent: str) -> UserSession:
        session = UserSession(
            user_id=user_id,
            session_key=session_key,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.session.add(session)
        db.session.flush()
        return session

    @staticmethod
    def get_session(session_key: str) -> Optional[UserSession]:
        return db.session.execute(select(UserSession).filter_by(session_key=session_key)).scalar_one_or_none()

    @staticmethod
    def get_active_sessions(user_id: int) -> List[UserSession]:
        return list(db.session.execute(
            select(UserSession).filter_by(user_id=user_id, is_active=True)
        ).scalars())

    @staticmethod
    def update_session_activity(session_key: str) -> UserSession:
        session = SessionManager.get_session(session_key)
        if not session:
            raise BusinessError("session_not_found")
        session.last_activity_at = datetime.now(timezone.utc)
        db.session.flush()
        return session

    @staticmethod
    def revoke_session(session_key: str) -> UserSession:
        session = SessionManager.get_session(session_key)
        if not session:
            raise BusinessError("session_not_found")
        session.is_active = False
        db.session.flush()
        return session

    @staticmethod
    def revoke_all_sessions(user_id: int, except_session: Optional[str] = None) -> int:
        query = update(UserSession).where(
            UserSession.user_id == user_id,
            UserSession.is_active == True
        )
        if except_session:
            query = query.where(UserSession.session_key != except_session)
        count = db.session.execute(query.values(is_active=False)).rowcount
        db.session.flush()
        return count

    @staticmethod
    def get_user_sessions(user_id: int) -> List[UserSession]:
        return list(db.session.execute(select(UserSession).filter_by(user_id=user_id)).scalars())

    @staticmethod
    def get_user_sessions_count(user_id: int) -> int:
        return db.session.execute(select(UserSession).filter_by(user_id=user_id)).scalar()

    @staticmethod
    def get_active_sessions_count(user_id: int) -> int:
        return db.session.execute(
            select(UserSession).filter_by(user_id=user_id, is_active=True)
        ).scalar()
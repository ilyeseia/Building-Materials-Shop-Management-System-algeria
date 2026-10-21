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
        """Register (or re-register) the session row for a browser session.

        The key is unique in the database, so a second sign-in that reuses a
        key must refresh the existing row instead of inserting a duplicate
        that would violate the unique index.
        """
        session = SessionManager.get_session(session_key)
        if session is None:
            session = UserSession(
                user_id=user_id,
                session_key=session_key,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            db.session.add(session)
        else:
            session.user_id = user_id
            session.ip_address = ip_address
            session.user_agent = user_agent
            session.is_active = True
            session.last_activity_at = datetime.now(timezone.utc)
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
    def revoke_session_if_present(session_key: Optional[str]) -> bool:
        """Revoke a session without ever raising.

        Signing out must always succeed.  A missing row means the session was
        already gone, which is exactly the desired end state.
        """
        if not session_key:
            return False
        session = SessionManager.get_session(session_key)
        if session is None:
            return False
        session.is_active = False
        db.session.flush()
        return True

    @staticmethod
    def is_session_active(session_key: Optional[str]) -> bool:
        """Is the tracked session still usable?

        A key that was never tracked is treated as active: the session may
        predate the tracking table, and refusing access would lock the
        operator out of their own shop.
        """
        if not session_key:
            return True
        session = SessionManager.get_session(session_key)
        if session is None:
            return True
        return bool(session.is_active)

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
"""IAM Service: Granular permission evaluation, role management, and scope enforcement.
"""
from __future__ import annotations

import json
from typing import Set, List

from flask import current_app
from sqlalchemy import select

from app.extensions import db
from app.models import Role, RoleDefinition, User, Warehouse


class IAMService:
    @staticmethod
    def get_role_permissions(role: str) -> Set[str]:
        """Fetch effective permissions for a role, considering DB overrides."""
        # Check for custom role definition in DB
        role_def = db.session.execute(
            select(RoleDefinition).filter_by(code=role)
        ).scalar()
        
        if role_def:
            try:
                return set(json.loads(role_def.permissions_json))
            except (json.JSONDecodeError, TypeError):
                pass
        
        # Fallback to hardcoded defaults (defined in models.security)
        from app.models.security import permissions_for
        return permissions_for(role)

    @staticmethod
    def user_has_permission(user: User, permission: str) -> bool:
        """Check if user has a specific granular permission."""
        if not user or not user.is_active:
            return False
            
        # Admin bypass
        if user.is_admin:
            return True
            
        grants = IAMService.get_role_permissions(user.role)
        if "*" in grants or permission in grants:
            return True
            
        # Implicit permission handling (manage implies view)
        module, _, action = permission.partition(".")
        if action == "view" and f"{module}.manage" in grants:
            return True
        if action == "manage" and f"{module}.manage" in grants:
            return True
            
        return False

    @staticmethod
    def enforce_branch_scope(user: User, resource_branch_id: int) -> bool:
        """Enforce branch/warehouse scope restriction."""
        if user.is_admin:
            return True
        # If user is assigned to a branch, verify matching
        if user.branch_id:
            return user.branch_id == resource_branch_id
        # If user has no branch restriction (e.g., HQ role), they might see all
        return True

    @staticmethod
    def list_roles() -> List[RoleDefinition]:
        """List all defined roles (system + custom)."""
        return list(db.session.execute(select(RoleDefinition).order_by(RoleDefinition.code)).scalars())

    @staticmethod
    def create_or_update_role(
        code: str, 
        name_ar: str, 
        name_fr: str, 
        name_en: str, 
        permissions: List[str], 
        description: str = ""
    ) -> RoleDefinition:
        """Create or update a custom role definition."""
        role = db.session.execute(select(RoleDefinition).filter_by(code=code)).scalar()
        if not role:
            role = RoleDefinition(code=code)
            db.session.add(role)
            
        role.name_ar = name_ar
        role.name_fr = name_fr
        role.name_en = name_en
        role.description = description
        role.set_permissions(permissions)
        
        db.session.flush()
        return role

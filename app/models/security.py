"""Users, roles and the enterprise permission model.

The security model supports granular permissions, dynamic role definitions,
session revocation, account locking, password security policies, MFA (TOTP),
and branch-scoped access controls.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models.base import TimestampMixin, utcnow

PASSWORD_MIN_LENGTH = 8


class AccountStatus:
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    PENDING = "pending"


ALL_ACCOUNT_STATUSES = (
    AccountStatus.ACTIVE,
    AccountStatus.INACTIVE,
    AccountStatus.SUSPENDED,
    AccountStatus.PENDING,
)


class Role:
    ADMIN = "admin"
    MANAGER = "manager"
    SALES = "sales"
    CASHIER = "cashier"
    STOCK = "stock"
    WEIGHBRIDGE = "weighbridge"
    DRIVER = "driver"
    ACCOUNTANT = "accountant"
    VIEWER = "viewer"


ALL_ROLES = (
    Role.ADMIN,
    Role.MANAGER,
    Role.SALES,
    Role.CASHIER,
    Role.STOCK,
    Role.WEIGHBRIDGE,
    Role.DRIVER,
    Role.ACCOUNTANT,
    Role.VIEWER,
)

#: Read-only permissions granted to every role that has the matching ``.manage``.
# Note: "users" is intentionally excluded - user management permissions are
# granted explicitly per-role to avoid giving MANAGER full user administration.
_PAIRS = (
    "sales", "purchasing", "inventory", "catalog", "customers", "suppliers",
    "weigh", "fleet", "payments", "settings", "reports",
)

# Granular permissions list for IAM inspection
GRANULAR_PERMISSIONS = {
    "dashboard.view": "View Executive Dashboard",
    "users.view": "View User Directory & Profiles",
    "users.create": "Create New User Accounts",
    "users.update": "Modify Existing User Accounts",
    "users.deactivate": "Activate, Deactivate or Suspend Accounts",
    "users.manage_roles": "Assign & Modify Roles and Permissions",
    "users.sessions": "Inspect & Revoke Active User Sessions",
    "catalog.view": "View Product Catalogue & Prices",
    "catalog.manage": "Manage Products, Categories & Units",
    "inventory.view": "View Stock Levels & Ledger",
    "inventory.manage": "Perform Stock Movements & Counts",
    "sales.view": "View Sales Orders & Invoices",
    "sales.manage": "Create Sales Invoices & Quotes",
    "purchasing.view": "View Purchase Orders & Invoices",
    "purchasing.manage": "Create Purchase Orders & Receipts",
    "customers.view": "View Customer Accounts",
    "customers.manage": "Manage Customers & Credit Limits",
    "suppliers.view": "View Supplier Accounts",
    "suppliers.manage": "Manage Suppliers",
    "weigh.view": "View Weighbridge Tickets",
    "weigh.manage": "Process Weighbridge Weighing Tickets",
    "fleet.view": "View Vehicles & Deliveries",
    "fleet.manage": "Manage Fleet & Assign Trips",
    "fleet.trips.status": "Update Vehicle Trip Statuses",
    "payments.view": "View Financial Payments",
    "payments.manage": "Record Payments & Allocations",
    "reports.view": "View Business & Margin Reports",
    "settings.view": "View System Configuration",
    "settings.manage": "Modify Business & System Settings",
    "audit.view": "Inspect Security Audit Logs",
    "ai.view": "Access Read-Only AI Workspace",
}

_ALL_VIEWS = {f"{name}.view" for name in _PAIRS} | {
    "dashboard.view", "reports.view", "audit.view", "ai.view", "settings.view",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    Role.ADMIN: {"*"},
    Role.MANAGER: (
        _ALL_VIEWS
        | {f"{name}.manage" for name in _PAIRS}
        | {"users.view", "users.create", "users.update", "users.deactivate"}
    ) - {"users.manage_roles", "settings.manage"},
    Role.SALES: {
        "dashboard.view", "sales.view", "sales.manage",
        "customers.view", "customers.manage",
        "catalog.view", "inventory.view", "reports.view", "weigh.view",
        "ai.view",
    },
    Role.CASHIER: {
        "dashboard.view", "sales.view", "payments.view", "payments.manage",
        "customers.view", "reports.view", "ai.view",
    },
    Role.STOCK: {
        "dashboard.view", "inventory.view", "inventory.manage",
        "catalog.view", "catalog.manage",
        "purchasing.view", "purchasing.manage",
        "suppliers.view", "suppliers.manage",
        "reports.view", "weigh.view", "ai.view",
    },
    Role.WEIGHBRIDGE: {
        "dashboard.view", "weigh.view", "weigh.manage",
        "sales.view", "inventory.view", "catalog.view", "fleet.view",
    },
    Role.DRIVER: {
        "dashboard.view", "fleet.view", "fleet.trips.status",
    },
    Role.ACCOUNTANT: {
        "dashboard.view", "sales.view", "purchasing.view", "payments.view",
        "payments.manage", "reports.view", "customers.view", "suppliers.view",
        "ai.view",
    },
    Role.VIEWER: set(_ALL_VIEWS),
}


def permissions_for(role: str) -> set[str]:
    # Check if a custom RoleDefinition exists in DB first
    try:
        role_def = db.session.execute(
            db.select(RoleDefinition).filter_by(code=role)
        ).scalars().first()
        if role_def:
            return set(json.loads(role_def.permissions_json))
    except Exception:  # pragma: no cover
        pass
    return set(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS[Role.VIEWER]))


class RoleDefinition(TimestampMixin, db.Model):
    __tablename__ = "role_definitions"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name_ar = db.Column(db.String(120), nullable=False)
    name_fr = db.Column(db.String(120), nullable=False)
    name_en = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    permissions_json = db.Column(db.Text, nullable=False, default="[]")
    is_system = db.Column(db.Boolean, nullable=False, default=False)

    def get_permissions(self) -> list[str]:
        try:
            return json.loads(self.permissions_json or "[]")
        except Exception:
            return []

    def set_permissions(self, perms: list[str]) -> None:
        self.permissions_json = json.dumps(sorted(list(set(perms))))


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120))
    email = db.Column(db.String(120), index=True)
    role = db.Column(db.String(20), nullable=False, default=Role.VIEWER, index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), index=True)
    language = db.Column(db.String(5))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(20), nullable=False, default=AccountStatus.ACTIVE, index=True)
    phone = db.Column(db.String(30))
    last_login_at = db.Column(db.DateTime)

    # IAM extensions
    failed_login_attempts = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    totp_secret = db.Column(db.String(64))
    totp_enabled = db.Column(db.Boolean, nullable=False, default=False)
    totp_backup_codes = db.Column(db.Text)
    last_password_change_at = db.Column(db.DateTime)
    deactivated_at = db.Column(db.DateTime)

    branch = db.relationship("Warehouse", lazy="joined")
    sessions = db.relationship("UserSession", backref="user", cascade="all, delete-orphan", lazy="select")
    reset_tokens = db.relationship("PasswordResetToken", backref="user", cascade="all, delete-orphan", lazy="select")

    # --- Account Lockout Logic ---------------------------------------
    @property
    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        return self.locked_until > utcnow()

    def record_login_failure(self, max_attempts: int = 5, lock_minutes: int = 15) -> None:
        self.failed_login_attempts = (self.failed_login_attempts or 0) + 1
        if self.failed_login_attempts >= max_attempts:
            from datetime import timedelta
            self.locked_until = utcnow() + timedelta(minutes=lock_minutes)

    def record_login_success(self) -> None:
        self.failed_login_attempts = 0
        self.locked_until = None
        self.last_login_at = utcnow()

    # --- Password handling -------------------------------------------
    def set_password(self, password: str) -> None:
        if len(password or "") < PASSWORD_MIN_LENGTH:
            raise ValueError("password_too_short")
        self.password_hash = generate_password_hash(password)
        self.last_password_change_at = utcnow()
        self.must_change_password = False

    def check_password(self, password: str) -> bool:
        if not self.password_hash or not password:
            return False
        return check_password_hash(self.password_hash, password)

    # --- Authorisation -----------------------------------------------
    def has_permission(self, permission: str) -> bool:
        if not self.is_active or self.status != AccountStatus.ACTIVE or self.is_locked:
            return False
        # Use IAMService for dynamic permission evaluation (includes custom roles)
        try:
            from app.services.iam import IAMService
            return IAMService.user_has_permission(self, permission)
        except Exception:
            # Fallback to static permissions if IAMService unavailable
            grants = permissions_for(self.role)
            if "*" in grants:
                return True
            if permission in grants:
                return True
            module, _, action = permission.partition(".")
            if action == "view" and (f"{module}.manage" in grants or "users.manage" in grants):
                return True
            if action == "manage" and f"{module}.manage" in grants:
                return True
            if module == "users" and action in ("create", "update", "deactivate") and "users.manage" in grants:
                return True
            if permission == "fleet.trips.status" and "fleet.manage" in grants:
                return True
            return False

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} ({self.role})>"


class UserSession(db.Model):
    __tablename__ = "user_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    session_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    ip_address = db.Column(db.String(64))
    user_agent = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_activity_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    @property
    def is_valid(self) -> bool:
        if self.used_at is not None:
            return False
        # Handle both naive and aware datetimes
        expires = self.expires_at
        now = utcnow()
        if expires.tzinfo is not None:
            # If expires_at is aware, make now aware too
            from datetime import timezone
            now = now.replace(tzinfo=timezone.utc)
        return expires > now

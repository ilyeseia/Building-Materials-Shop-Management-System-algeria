"""Users, roles and the permission model.

The legacy schema had a single ``users`` table with no notion of roles, so every
authenticated person could perform every action.  Roles are introduced here and
enforced in ``app/security.py``; the table is extended additively so existing
rows keep working (they are granted the ``admin`` role by the migration).

**Important:** adding a role to the database is *not* a security boundary on its
own.  Every protected route must still call the ``@requires(...)`` decorator;
``tests/test_security.py`` exercises that.
"""
from __future__ import annotations

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models.base import TimestampMixin

PASSWORD_MIN_LENGTH = 8


class Role:
    ADMIN = "admin"
    MANAGER = "manager"
    SALES = "sales"
    CASHIER = "cashier"
    STOCK = "stock"
    WEIGHBRIDGE = "weighbridge"
    DRIVER = "driver"
    VIEWER = "viewer"


ALL_ROLES = (
    Role.ADMIN,
    Role.MANAGER,
    Role.SALES,
    Role.CASHIER,
    Role.STOCK,
    Role.WEIGHBRIDGE,
    Role.DRIVER,
    Role.VIEWER,
)

#: Read-only permissions granted to every role that has the matching ``.manage``.
_PAIRS = (
    "sales", "purchasing", "inventory", "catalog", "customers", "suppliers",
    "weigh", "fleet", "payments", "settings", "users",
)

# The AI workspace is read-only, so there is deliberately no ``ai.manage``
# permission.  Granting one would suggest the assistant can change data.
_ALL_VIEWS = {f"{name}.view" for name in _PAIRS} | {
    "dashboard.view", "reports.view", "audit.view", "ai.view",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    Role.ADMIN: {"*"},
    Role.MANAGER: (
        _ALL_VIEWS
        | {f"{name}.manage" for name in _PAIRS}
    ) - {"users.manage"},
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
    Role.VIEWER: set(_ALL_VIEWS),
}


def permissions_for(role: str) -> set[str]:
    return set(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS[Role.VIEWER]))


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120))
    role = db.Column(db.String(20), nullable=False, default=Role.VIEWER, index=True)
    language = db.Column(db.String(5))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    phone = db.Column(db.String(30))
    last_login_at = db.Column(db.DateTime)
    #: ``created_at``/``updated_at`` come from :class:`TimestampMixin`.  The legacy
    #: table already had ``created_at``; the migration only adds the new columns.

    # --- Password handling -------------------------------------------
    def set_password(self, password: str) -> None:
        if len(password or "") < PASSWORD_MIN_LENGTH:
            raise ValueError("password_too_short")
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash or not password:
            return False
        return check_password_hash(self.password_hash, password)

    # --- Authorisation -----------------------------------------------
    def has_permission(self, permission: str) -> bool:
        grants = permissions_for(self.role)
        if "*" in grants:
            return True
        if permission in grants:
            return True
        # ``x.manage`` implies ``x.view``.
        module, _, action = permission.partition(".")
        if action == "view" and f"{module}.manage" in grants:
            return True
        if action == "manage" and f"{module}.manage" in grants:
            return True
        if permission == "fleet.trips.status" and "fleet.manage" in grants:
            return True
        return False

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<User {self.username} ({self.role})>"

"""Enterprise IAM: user lifecycle, sessions, password reset tokens, custom roles

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None

NEW_USER_COLUMNS = {
    "email": sa.Column("email", sa.String(length=120), nullable=True),
    "branch_id": sa.Column("branch_id", sa.Integer(), nullable=True),
    "failed_login_attempts": sa.Column(
        "failed_login_attempts", sa.Integer(), nullable=False, server_default="0"
    ),
    "locked_until": sa.Column("locked_until", sa.DateTime(), nullable=True),
    "must_change_password": sa.Column(
        "must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("0")
    ),
    "totp_secret": sa.Column("totp_secret", sa.String(length=64), nullable=True),
    "totp_enabled": sa.Column(
        "totp_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")
    ),
    "totp_backup_codes": sa.Column("totp_backup_codes", sa.Text(), nullable=True),
    "last_password_change_at": sa.Column(
        "last_password_change_at", sa.DateTime(), nullable=True
    ),
    "deactivated_at": sa.Column("deactivated_at", sa.DateTime(), nullable=True),
    "status": sa.Column(
        "status", sa.String(length=20), nullable=False, server_default="active"
    ),
}

NEW_INDEXES = [
    ("ix_users_email", "users", ["email"]),
    ("ix_users_branch_id", "users", ["branch_id"]),
    ("ix_users_status", "users", ["status"]),
]

NEW_TABLES = [
    "user_sessions",
    "password_reset_tokens",
    "role_definitions",
]


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    if "users" in tables:
        users_columns = {col["name"] for col in inspector.get_columns("users")}
        for name, column in NEW_USER_COLUMNS.items():
            if name not in users_columns:
                op.add_column("users", column)

        existing_indexes = {ix["name"] for ix in inspector.get_indexes("users")}
        for index_name, table, columns in NEW_INDEXES:
            if index_name not in existing_indexes:
                op.create_index(index_name, table, columns)

        existing_fks = {
            fk["name"] for fk in inspector.get_foreign_keys("users") if fk.get("name")
        }
        fk_name = "fk_users_branch_id_warehouses"
        if fk_name not in existing_fks:
            if op.get_bind().dialect.name == "sqlite":
                # SQLite cannot ALTER a foreign key in-place; batch mode
                # recreates the table with the constraint added.
                with op.batch_alter_table("users") as batch_op:
                    batch_op.create_foreign_key(
                        fk_name, "warehouses", ["branch_id"], ["id"]
                    )
            else:
                op.create_foreign_key(
                    fk_name, "users", "warehouses", ["branch_id"], ["id"]
                )

    if "user_sessions" not in tables:
        op.create_table(
            "user_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("session_key", sa.String(length=64), nullable=False),
            sa.Column("ip_address", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_activity_at", sa.DateTime(), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name="fk_user_sessions_user_id_users",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_user_sessions_user_id", "user_sessions", ["user_id"]
        )
        op.create_index(
            "ix_user_sessions_session_key",
            "user_sessions",
            ["session_key"],
            unique=True,
        )
        op.create_index("ix_user_sessions_is_active", "user_sessions", ["is_active"])

    if "password_reset_tokens" not in tables:
        op.create_table(
            "password_reset_tokens",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name="fk_password_reset_tokens_user_id_users",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
        )
        op.create_index(
            "ix_password_reset_tokens_token_hash",
            "password_reset_tokens",
            ["token_hash"],
            unique=True,
        )

    if "role_definitions" not in tables:
        op.create_table(
            "role_definitions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=40), nullable=False),
            sa.Column("name_ar", sa.String(length=120), nullable=False),
            sa.Column("name_fr", sa.String(length=120), nullable=False),
            sa.Column("name_en", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("permissions_json", sa.Text(), nullable=False),
            sa.Column(
                "is_system", sa.Boolean(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_role_definitions_code", "role_definitions", ["code"], unique=True
        )


def downgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    # Dropping these tables removes their indexes and foreign keys with them.
    op.drop_table("role_definitions") if "role_definitions" in tables else None
    op.drop_table("password_reset_tokens") if "password_reset_tokens" in tables else None
    op.drop_table("user_sessions") if "user_sessions" in tables else None

    if "users" not in tables:
        return

    users_columns = {col["name"] for col in inspector.get_columns("users")}
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("users")}
    fk_name = "fk_users_branch_id_warehouses"
    fk_present = inspector.get_foreign_keys("users")

    if any(fk.get("name") == fk_name for fk in fk_present):
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("users") as batch_op:
                batch_op.drop_constraint(fk_name, type_="foreignkey")
        else:
            op.drop_constraint(fk_name, "users", type_="foreignkey")

    for index_name, table, _columns in NEW_INDEXES:
        if index_name in {ix["name"] for ix in inspector.get_indexes(table)}:
            op.drop_index(index_name, table_name=table)

    for name in reversed(list(NEW_USER_COLUMNS.keys())):
        if name in users_columns:
            op.drop_column("users", name)

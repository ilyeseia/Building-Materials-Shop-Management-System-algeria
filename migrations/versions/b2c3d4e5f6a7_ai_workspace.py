"""AI workspace

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-19

Adds the append-only ``ai_interactions`` audit table and a per-business switch
for the read-only AI workspace.  The switch is additive: existing rows default
to enabled, and disabling the workspace never touches sales, stock or
accounting data.
"""
from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "business_settings",
        sa.Column("enable_ai", sa.Boolean(), nullable=False, server_default=sa.text("1")),
    )

    op.create_table(
        "ai_interactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False, server_default="local"),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ok"),
        sa.Column("tool_calls", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_key", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_ai_interactions_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_interactions_created_at", "ai_interactions", ["created_at"])
    op.create_index("ix_ai_interactions_user_id", "ai_interactions", ["user_id"])
    op.create_index("ix_ai_interactions_status", "ai_interactions", ["status"])


def downgrade():
    op.drop_index("ix_ai_interactions_status", table_name="ai_interactions")
    op.drop_index("ix_ai_interactions_user_id", table_name="ai_interactions")
    op.drop_index("ix_ai_interactions_created_at", table_name="ai_interactions")
    op.drop_table("ai_interactions")
    op.drop_column("business_settings", "enable_ai")

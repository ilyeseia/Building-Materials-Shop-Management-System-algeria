"""AI provider and model registry

Adds the tables that let an administrator register any OpenAI-compatible
endpoint (including the free-tier gateways) and the models it offers.  The
API key is stored encrypted; the column holds ciphertext, never a readable
secret.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    if "ai_providers" not in tables:
        op.create_table(
            "ai_providers",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=40), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "provider_type", sa.String(length=30), nullable=False,
                server_default="openai_compatible",
            ),
            sa.Column("base_url", sa.String(length=255), nullable=False),
            sa.Column("api_key_encrypted", sa.Text(), nullable=True),
            sa.Column("key_hint", sa.String(length=16), nullable=True),
            sa.Column("free_tier", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_ai_providers_code", "ai_providers", ["code"], unique=True)
        op.create_index("ix_ai_providers_is_active", "ai_providers", ["is_active"])
        op.create_index("ix_ai_providers_is_default", "ai_providers", ["is_default"])

    if "ai_models" not in tables:
        op.create_table(
            "ai_models",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("provider_id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=120), nullable=False),
            sa.Column("label", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("context_window", sa.Integer(), nullable=True),
            sa.Column("max_output_tokens", sa.Integer(), nullable=True),
            sa.Column("supports_tools", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("free_tier", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["provider_id"],
                ["ai_providers.id"],
                name="fk_ai_models_provider_id_ai_providers",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_ai_models_provider_id", "ai_models", ["provider_id"])
        op.create_index("ix_ai_models_code", "ai_models", ["code"])
        op.create_index("ix_ai_models_is_active", "ai_models", ["is_active"])


def downgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    # The child table goes first: dropping it removes its index and the
    # foreign key that points at ai_providers.
    if "ai_models" in tables:
        op.drop_table("ai_models")
    if "ai_providers" in tables:
        op.drop_table("ai_providers")

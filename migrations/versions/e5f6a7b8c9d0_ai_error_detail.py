"""Store the provider message behind a local fallback

The workspace used to report only "the external provider is unavailable", which
made a rejected model id impossible to diagnose from the interface.  The new
nullable column keeps the short, secret-free provider message (for example
``HTTP 400: ... is a decisions model``) so an administrator can see why the
answer came from the local assistant.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade():
    if "error_detail" not in _columns("ai_interactions"):
        op.add_column("ai_interactions", sa.Column("error_detail", sa.Text(), nullable=True))


def downgrade():
    if "error_detail" in _columns("ai_interactions"):
        op.drop_column("ai_interactions", "error_detail")

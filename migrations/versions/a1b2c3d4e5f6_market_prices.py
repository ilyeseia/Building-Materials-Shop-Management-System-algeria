"""market reference prices

Revision ID: a1b2c3d4e5f6
Revises: 5790880d9443
Create Date: 2026-10-19

Adds the append-only market-price observation table.  It is deliberately
separate from ``products.sale_price``: an external observation never changes a
selling price automatically.
"""
from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "5790880d9443"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "market_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("unit_id", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("source_name", sa.String(length=160), nullable=False),
        sa.Column("source_reference", sa.String(length=255), nullable=True),
        sa.Column("wilaya_code", sa.Integer(), nullable=True),
        sa.Column("location", sa.String(length=160), nullable=True),
        sa.Column("observed_on", sa.Date(), nullable=False),
        sa.Column("verification_status", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["unit_id"], ["units_of_measure.id"]),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id",
            "unit_id",
            "source_name",
            "observed_on",
            name="uq_market_price_observation",
        ),
    )
    op.create_index(
        "ix_market_prices_product_observed",
        "market_prices",
        ["product_id", "observed_on"],
    )
    op.create_index("ix_market_prices_product_id", "market_prices", ["product_id"])
    op.create_index("ix_market_prices_unit_id", "market_prices", ["unit_id"])
    op.create_index("ix_market_prices_source_name", "market_prices", ["source_name"])
    op.create_index("ix_market_prices_wilaya_code", "market_prices", ["wilaya_code"])
    op.create_index("ix_market_prices_observed_on", "market_prices", ["observed_on"])
    op.create_index(
        "ix_market_prices_verification_status",
        "market_prices",
        ["verification_status"],
    )


def downgrade():
    op.drop_index("ix_market_prices_verification_status", table_name="market_prices")
    op.drop_index("ix_market_prices_observed_on", table_name="market_prices")
    op.drop_index("ix_market_prices_wilaya_code", table_name="market_prices")
    op.drop_index("ix_market_prices_source_name", table_name="market_prices")
    op.drop_index("ix_market_prices_unit_id", table_name="market_prices")
    op.drop_index("ix_market_prices_product_id", table_name="market_prices")
    op.drop_index("ix_market_prices_product_observed", table_name="market_prices")
    op.drop_table("market_prices")

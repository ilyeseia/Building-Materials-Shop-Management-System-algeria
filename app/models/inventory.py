"""Warehouses, stock levels, the stock-movement ledger, reservations,
transfers and physical counts.

Two invariants drive this module:

1. **On-hand quantities are never edited directly.**  Every change is appended to
   :class:`StockMovement`, which records the product, warehouse, sign, quantity in
   the product's stock unit, the original quantity and unit as entered, the source
   document, the user and the resulting balance.
2. **Reservations are not stock.**  ``StockLevel.quantity`` is physical stock,
   ``StockLevel.reserved`` is stock promised to a confirmed order.  Available =
   quantity − reserved.
"""
from __future__ import annotations

from app.extensions import db
from app.models.base import (
    MONEY,
    QUANTITY,
    ActorMixin,
    MovementDirection,
    MovementReason,
    SoftDeleteMixin,
    TimestampMixin,
)


class Warehouse(TimestampMixin, SoftDeleteMixin, db.Model):
    __tablename__ = "warehouses"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name_ar = db.Column(db.String(120), nullable=False)
    name_fr = db.Column(db.String(120), nullable=False)
    name_en = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(200))
    wilaya_code = db.Column(db.Integer)
    is_default = db.Column(db.Boolean, nullable=False, default=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Warehouse {self.code}>"


class StockLevel(TimestampMixin, db.Model):
    """Current physical and reserved quantity per (product, warehouse).

    ``avg_cost`` holds the running weighted-average cost used for stock
    valuation, recomputed on every receipt (see ``app.services.stock``).
    """

    __tablename__ = "stock_levels"
    __table_args__ = (
        db.UniqueConstraint("product_id", "warehouse_id", name="uq_stock_level"),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id"), nullable=False, index=True
    )
    warehouse_id = db.Column(
        db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True
    )
    quantity = db.Column(QUANTITY, nullable=False, default=0)
    reserved = db.Column(QUANTITY, nullable=False, default=0)
    avg_cost = db.Column(MONEY, nullable=False, default=0)

    product = db.relationship("Product", lazy="joined")
    warehouse = db.relationship("Warehouse", lazy="joined")

    @property
    def available(self):
        return (self.quantity or 0) - (self.reserved or 0)


class StockMovement(db.Model):
    """Append-only stock ledger.

    ``quantity`` is always positive and expressed in the product's stock unit;
    ``direction`` gives the sign.  Reservation changes are recorded on the same
    ledger with ``reserved_delta`` and ``quantity = 0`` so that a single
    append-only stream explains both columns of :class:`StockLevel`.
    """

    __tablename__ = "stock_movements"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, index=True)
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id"), nullable=False, index=True
    )
    warehouse_id = db.Column(
        db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True
    )
    direction = db.Column(db.String(4), nullable=False)   # in | out
    reason = db.Column(db.String(20), nullable=False, index=True)
    quantity = db.Column(QUANTITY, nullable=False, default=0)
    reserved_delta = db.Column(QUANTITY, nullable=False, default=0)
    on_hand_after = db.Column(QUANTITY, nullable=False, default=0)
    reserved_after = db.Column(QUANTITY, nullable=False, default=0)

    #: Unit actually used when the movement was entered (may differ from the
    #: stock unit); the factor applied is preserved for audit.
    unit_id = db.Column(db.Integer, db.ForeignKey("units_of_measure.id"))
    original_quantity = db.Column(QUANTITY)
    original_unit_id = db.Column(db.Integer, db.ForeignKey("units_of_measure.id"))
    conversion_factor = db.Column(db.Numeric(18, 6))

    #: Source document, e.g. ``("sales_invoice", 42)``.
    source_type = db.Column(db.String(40), index=True)
    source_id = db.Column(db.Integer, index=True)
    reference = db.Column(db.String(60))
    note = db.Column(db.Text)

    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    ip_address = db.Column(db.String(64))

    product = db.relationship("Product", lazy="joined")
    warehouse = db.relationship("Warehouse", lazy="joined")
    unit = db.relationship("UnitOfMeasure", foreign_keys=[unit_id], lazy="joined")
    actor = db.relationship("User", lazy="joined")

    @property
    def signed_quantity(self):
        from decimal import Decimal

        value = self.quantity or Decimal("0")
        return value if self.direction == MovementDirection.IN.value else -value

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<StockMovement {self.reason} {self.direction} "
            f"{self.quantity} p{self.product_id}@w{self.warehouse_id}>"
        )


class StockAdjustment(TimestampMixin, ActorMixin, db.Model):
    """Header for a manual stock adjustment (optionally with several lines).

    The adjustment itself changes nothing: posting it creates the corresponding
    :class:`StockMovement` rows, so nothing can bypass the ledger.
    """

    __tablename__ = "stock_adjustments"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, nullable=False)
    adjustment_date = db.Column(db.Date, nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    reason = db.Column(db.String(20), nullable=False, default=MovementReason.ADJUSTMENT.value)
    justification = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(12), nullable=False, default="draft")
    posted_at = db.Column(db.DateTime)

    warehouse = db.relationship("Warehouse", lazy="joined")
    lines = db.relationship(
        "StockAdjustmentLine", backref="adjustment",
        cascade="all, delete-orphan", lazy="select",
    )


class StockAdjustmentLine(db.Model):
    __tablename__ = "stock_adjustment_lines"

    id = db.Column(db.Integer, primary_key=True)
    adjustment_id = db.Column(
        db.Integer, db.ForeignKey("stock_adjustments.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(QUANTITY, nullable=False)   # signed: + increases stock
    note = db.Column(db.String(200))

    product = db.relationship("Product", lazy="joined")


class StockTransfer(TimestampMixin, ActorMixin, db.Model):
    __tablename__ = "stock_transfers"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, nullable=False)
    transfer_date = db.Column(db.Date, nullable=False)
    source_warehouse_id = db.Column(
        db.Integer, db.ForeignKey("warehouses.id"), nullable=False
    )
    destination_warehouse_id = db.Column(
        db.Integer, db.ForeignKey("warehouses.id"), nullable=False
    )
    status = db.Column(db.String(12), nullable=False, default="draft")
    note = db.Column(db.Text)
    posted_at = db.Column(db.DateTime)

    source_warehouse = db.relationship("Warehouse", foreign_keys=[source_warehouse_id])
    destination_warehouse = db.relationship(
        "Warehouse", foreign_keys=[destination_warehouse_id]
    )
    lines = db.relationship(
        "StockTransferLine", backref="transfer",
        cascade="all, delete-orphan", lazy="select",
    )


class StockTransferLine(db.Model):
    __tablename__ = "stock_transfer_lines"

    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(
        db.Integer, db.ForeignKey("stock_transfers.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(QUANTITY, nullable=False)

    product = db.relationship("Product", lazy="joined")


class StockCount(TimestampMixin, ActorMixin, db.Model):
    __tablename__ = "stock_counts"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, nullable=False)
    count_date = db.Column(db.Date, nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="draft")
    note = db.Column(db.Text)
    posted_at = db.Column(db.DateTime)

    warehouse = db.relationship("Warehouse", lazy="joined")
    lines = db.relationship(
        "StockCountLine", backref="count",
        cascade="all, delete-orphan", lazy="select",
    )


class StockCountLine(db.Model):
    __tablename__ = "stock_count_lines"

    id = db.Column(db.Integer, primary_key=True)
    count_id = db.Column(
        db.Integer, db.ForeignKey("stock_counts.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    system_quantity = db.Column(QUANTITY, nullable=False, default=0)
    counted_quantity = db.Column(QUANTITY, nullable=False, default=0)
    note = db.Column(db.String(200))

    product = db.relationship("Product", lazy="joined")

    @property
    def difference(self):
        return (self.counted_quantity or 0) - (self.system_quantity or 0)

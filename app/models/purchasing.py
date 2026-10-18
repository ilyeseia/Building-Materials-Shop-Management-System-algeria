"""Purchasing: purchase order â†’ goods receipt â†’ supplier invoice â†’ payment.

Receipts are the point at which stock enters the ledger.  Landed costs (transport,
handling, other) are allocated to receipt lines in proportion to line value, so
the weighted-average cost used for margin and valuation reflects what the goods
actually cost.

Supplier invoices record what the supplier bills; they are created after the
receipt and are what supplier payments settle.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import declared_attr

from app.extensions import db
from app.models.base import (
    FACTOR,
    MONEY,
    PERCENT,
    QUANTITY,
    ActorMixin,
    DocumentStatus,
    TimestampMixin,
)


class PurchaseDocumentMixin(TimestampMixin, ActorMixin):
    number = db.Column(db.String(40), unique=True, index=True)
    status = db.Column(db.String(24), nullable=False, default=DocumentStatus.DRAFT.value)
    subtotal = db.Column(MONEY, nullable=False, default=0)
    discount_amount = db.Column(MONEY, nullable=False, default=0)
    tax_amount = db.Column(MONEY, nullable=False, default=0)
    total_amount = db.Column(MONEY, nullable=False, default=0)
    vat_rate = db.Column(PERCENT)
    notes = db.Column(db.Text)
    cancelled_at = db.Column(db.DateTime)
    cancel_reason = db.Column(db.String(255))


class PurchaseOrderLineMixin:
    quantity = db.Column(QUANTITY, nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey("units_of_measure.id"), nullable=False)
    stock_quantity = db.Column(QUANTITY, nullable=False, default=0)
    conversion_factor = db.Column(FACTOR, nullable=False, default=1)
    unit_price = db.Column(MONEY, nullable=False, default=0)
    discount_percent = db.Column(PERCENT, nullable=False, default=0)
    unit_cost = db.Column(MONEY, nullable=False, default=0)
    line_total = db.Column(MONEY, nullable=False, default=0)
    description = db.Column(db.String(255))

    @declared_attr
    def unit(cls):  # noqa: N805 - declarative mixin signature
        return db.relationship("UnitOfMeasure", lazy="joined")


class PurchaseOrder(PurchaseDocumentMixin, db.Model):
    __tablename__ = "purchase_orders"

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(
        db.Integer, db.ForeignKey("suppliers.id"), nullable=False, index=True
    )
    order_date = db.Column(db.Date, nullable=False, index=True)
    expected_date = db.Column(db.Date)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    supplier_reference = db.Column(db.String(60))
    received_at = db.Column(db.Date)

    supplier = db.relationship("Supplier", lazy="joined")
    warehouse = db.relationship("Warehouse", lazy="joined")
    items = db.relationship(
        "PurchaseOrderItem", backref="order", cascade="all, delete-orphan", lazy="select"
    )
    receipts = db.relationship(
        "GoodsReceipt", backref="order", lazy="select",
        foreign_keys="GoodsReceipt.purchase_order_id",
    )


class PurchaseOrderItem(PurchaseOrderLineMixin, db.Model):
    __tablename__ = "purchase_order_items"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(
        db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    received_quantity = db.Column(QUANTITY, nullable=False, default=0)
    invoiced_quantity = db.Column(QUANTITY, nullable=False, default=0)

    product = db.relationship("Product", lazy="joined")

    @property
    def remaining_to_receive(self):
        return (self.quantity or 0) - (self.received_quantity or 0)


class GoodsReceipt(PurchaseDocumentMixin, db.Model):
    __tablename__ = "goods_receipts"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(
        db.Integer, db.ForeignKey("purchase_orders.id"), index=True
    )
    supplier_id = db.Column(
        db.Integer, db.ForeignKey("suppliers.id"), nullable=False, index=True
    )
    receipt_date = db.Column(db.Date, nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    supplier_reference = db.Column(db.String(60))
    #: Costs incurred to bring the goods in; allocated across lines by value.
    transport_cost = db.Column(MONEY, nullable=False, default=0)
    handling_cost = db.Column(MONEY, nullable=False, default=0)
    other_cost = db.Column(MONEY, nullable=False, default=0)
    landed_cost_allocated = db.Column(MONEY, nullable=False, default=0)

    supplier = db.relationship("Supplier", lazy="joined")
    warehouse = db.relationship("Warehouse", lazy="joined")
    items = db.relationship(
        "GoodsReceiptItem", backref="receipt", cascade="all, delete-orphan", lazy="select"
    )

    @property
    def total_landed_cost(self) -> Decimal:
        return (
            (self.transport_cost or Decimal("0"))
            + (self.handling_cost or Decimal("0"))
            + (self.other_cost or Decimal("0"))
        )


class GoodsReceiptItem(PurchaseOrderLineMixin, db.Model):
    __tablename__ = "goods_receipt_items"

    id = db.Column(db.Integer, primary_key=True)
    receipt_id = db.Column(
        db.Integer, db.ForeignKey("goods_receipts.id"), nullable=False, index=True
    )
    purchase_order_item_id = db.Column(
        db.Integer, db.ForeignKey("purchase_order_items.id")
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    #: unit_cost after landed-cost allocation (see app.services.purchasing).
    landed_unit_cost = db.Column(MONEY, nullable=False, default=0)

    product = db.relationship("Product", lazy="joined")


class SupplierInvoice(PurchaseDocumentMixin, db.Model):
    __tablename__ = "supplier_invoices"

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(
        db.Integer, db.ForeignKey("suppliers.id"), nullable=False, index=True
    )
    goods_receipt_id = db.Column(db.Integer, db.ForeignKey("goods_receipts.id"))
    invoice_date = db.Column(db.Date, nullable=False, index=True)
    due_date = db.Column(db.Date, index=True)
    supplier_reference = db.Column(db.String(60), index=True)
    received_at = db.Column(db.Date)

    supplier = db.relationship("Supplier", lazy="joined")
    items = db.relationship(
        "SupplierInvoiceItem", backref="invoice", cascade="all, delete-orphan", lazy="select"
    )

    @property
    def paid_amount(self) -> Decimal:
        from app.models.payments import Payment, PaymentAllocation

        total = (
            db.session.query(db.func.coalesce(db.func.sum(PaymentAllocation.amount), 0))
            .join(Payment, Payment.id == PaymentAllocation.payment_id)
            .filter(
                PaymentAllocation.document_type == "supplier_invoice",
                PaymentAllocation.document_id == self.id,
                Payment.status == DocumentStatus.POSTED.value,
            )
            .scalar()
        )
        return Decimal(str(total or 0))

    @property
    def balance_due(self) -> Decimal:
        return ((self.total_amount or Decimal("0")) - self.paid_amount).quantize(
            Decimal("0.01")
        )

    @property
    def settlement_status(self) -> str:
        if self.status in {DocumentStatus.DRAFT.value, DocumentStatus.CANCELLED.value}:
            return self.status
        if self.balance_due <= 0:
            return "paid"
        if self.paid_amount > 0:
            return "partial"
        return "unpaid"


class SupplierInvoiceItem(PurchaseOrderLineMixin, db.Model):
    __tablename__ = "supplier_invoice_items"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(
        db.Integer, db.ForeignKey("supplier_invoices.id"), nullable=False, index=True
    )
    goods_receipt_item_id = db.Column(
        db.Integer, db.ForeignKey("goods_receipt_items.id")
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)

    product = db.relationship("Product", lazy="joined")


class PurchaseReturn(PurchaseDocumentMixin, db.Model):
    """Goods returned to a supplier; removes the corresponding stock."""

    __tablename__ = "purchase_returns"

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(
        db.Integer, db.ForeignKey("suppliers.id"), nullable=False, index=True
    )
    goods_receipt_id = db.Column(db.Integer, db.ForeignKey("goods_receipts.id"))
    return_date = db.Column(db.Date, nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    reason = db.Column(db.String(255), nullable=False)

    supplier = db.relationship("Supplier", lazy="joined")
    warehouse = db.relationship("Warehouse", lazy="joined")
    items = db.relationship(
        "PurchaseReturnItem", backref="purchase_return",
        cascade="all, delete-orphan", lazy="select",
    )


class PurchaseReturnItem(PurchaseOrderLineMixin, db.Model):
    __tablename__ = "purchase_return_items"

    id = db.Column(db.Integer, primary_key=True)
    purchase_return_id = db.Column(
        db.Integer, db.ForeignKey("purchase_returns.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    receipt_item_id = db.Column(db.Integer, db.ForeignKey("goods_receipt_items.id"))

    product = db.relationship("Product", lazy="joined")


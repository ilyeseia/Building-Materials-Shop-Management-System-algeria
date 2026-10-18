"""Sales documents: quotation â†’ order â†’ delivery note â†’ invoice â†’ payment,
plus returns settled by credit notes.

Every money value is :class:`~decimal.Decimal`; every quantity is expressed in
the product's stock unit *and* keeps the unit/keyboard unit the operator used so
that the original document can be reprinted faithfully.

State changes are performed by :mod:`app.services.sales`, never by the blueprints
directly, so that stock effects, numbering and audit entries cannot be skipped.
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
    PriceList,
    TimestampMixin,
)


class SalesDocumentMixin(TimestampMixin, ActorMixin):
    """Amounts and status shared by quotations, orders, deliveries and invoices."""

    number = db.Column(db.String(40), unique=True, index=True)
    status = db.Column(db.String(24), nullable=False, default=DocumentStatus.DRAFT.value)
    price_list = db.Column(
        db.String(20), nullable=False, default=PriceList.RETAIL.value
    )
    subtotal = db.Column(MONEY, nullable=False, default=0)
    discount_amount = db.Column(MONEY, nullable=False, default=0)
    tax_amount = db.Column(MONEY, nullable=False, default=0)
    total_amount = db.Column(MONEY, nullable=False, default=0)
    vat_rate = db.Column(PERCENT)
    notes = db.Column(db.Text)
    cancelled_at = db.Column(db.DateTime)
    cancel_reason = db.Column(db.String(255))

    @property
    def gross_total(self) -> Decimal:
        """Total *before* the line discount (``subtotal`` is after it).

        Derived, never stored: a second stored copy could drift out of step with
        ``subtotal`` and ``discount_amount`` after an edit.
        """
        return ((self.subtotal or Decimal("0")) + (self.discount_amount or Decimal("0"))).quantize(
            Decimal("0.01")
        )


class LineMixin:
    """A document line.

    ``quantity`` is in the line's unit (``unit_id``); the quantity converted to
    the product's stock unit is ``stock_quantity`` and the factor used is kept so
    the conversion remains explainable after the fact.
    """

    quantity = db.Column(QUANTITY, nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey("units_of_measure.id"), nullable=False)
    stock_quantity = db.Column(QUANTITY, nullable=False, default=0)
    conversion_factor = db.Column(FACTOR, nullable=False, default=1)
    unit_price = db.Column(MONEY, nullable=False, default=0)
    discount_percent = db.Column(PERCENT, nullable=False, default=0)
    #: Cost of the goods at the time of the transaction â€” the basis of the
    #: gross-margin report.  Snapshotted so later price changes cannot rewrite
    #: historical margin.
    unit_cost = db.Column(MONEY, nullable=False, default=0)
    line_total = db.Column(MONEY, nullable=False, default=0)
    description = db.Column(db.String(255))

    @declared_attr
    def unit(cls):  # noqa: N805 - declarative mixin signature
        return db.relationship("UnitOfMeasure", lazy="joined")

    @property
    def gross_amount(self) -> Decimal:
        return (self.quantity or Decimal("0")) * (self.unit_price or Decimal("0"))

    @property
    def discount_amount(self) -> Decimal:
        return (self.gross_amount * (self.discount_percent or Decimal("0")) / Decimal("100")).quantize(
            Decimal("0.01")
        )

    @property
    def net_amount(self) -> Decimal:
        return (self.gross_amount - self.discount_amount).quantize(Decimal("0.01"))

    @property
    def margin_amount(self) -> Decimal:
        cost = (self.stock_quantity or Decimal("0")) * (self.unit_cost or Decimal("0"))
        return (self.net_amount - cost).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Quotation
# ---------------------------------------------------------------------------

class Quotation(SalesDocumentMixin, db.Model):
    __tablename__ = "quotations"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True
    )
    quotation_date = db.Column(db.Date, nullable=False)
    valid_until = db.Column(db.Date)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))

    customer = db.relationship("Customer", lazy="joined")
    warehouse = db.relationship("Warehouse", lazy="joined")
    items = db.relationship(
        "QuotationItem", backref="quotation", cascade="all, delete-orphan", lazy="select"
    )


class QuotationItem(LineMixin, db.Model):
    __tablename__ = "quotation_items"

    id = db.Column(db.Integer, primary_key=True)
    quotation_id = db.Column(
        db.Integer, db.ForeignKey("quotations.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    product = db.relationship("Product", lazy="joined")


# ---------------------------------------------------------------------------
# Sales order
# ---------------------------------------------------------------------------

class SalesOrder(SalesDocumentMixin, db.Model):
    __tablename__ = "sales_orders"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True
    )
    quotation_id = db.Column(db.Integer, db.ForeignKey("quotations.id"))
    order_date = db.Column(db.Date, nullable=False, index=True)
    delivery_date = db.Column(db.Date, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    #: Legacy mirror of the free-text delivery address.
    delivery_address = db.Column(db.Text)
    delivery_wilaya_code = db.Column(db.Integer)
    delivery_commune = db.Column(db.String(80))
    delivery_locality = db.Column(db.String(80))
    delivery_street = db.Column(db.String(160))
    stock_reserved = db.Column(db.Boolean, nullable=False, default=False)
    delivered_at = db.Column(db.Date)

    customer = db.relationship("Customer", lazy="joined")
    warehouse = db.relationship("Warehouse", lazy="joined")
    items = db.relationship(
        "SalesOrderItem", backref="order", cascade="all, delete-orphan", lazy="select"
    )
    delivery_notes = db.relationship(
        "DeliveryNote", backref="order", lazy="select",
        foreign_keys="DeliveryNote.order_id",
    )
    invoices = db.relationship(
        "SalesInvoice", backref="order", lazy="select",
        foreign_keys="SalesInvoice.order_id",
    )


class SalesOrderItem(LineMixin, db.Model):
    __tablename__ = "sales_order_items"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(
        db.Integer, db.ForeignKey("sales_orders.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    delivered_quantity = db.Column(QUANTITY, nullable=False, default=0)
    invoiced_quantity = db.Column(QUANTITY, nullable=False, default=0)

    product = db.relationship("Product", lazy="joined")

    @property
    def remaining_to_deliver(self):
        return (self.quantity or 0) - (self.delivered_quantity or 0)

    @property
    def remaining_to_invoice(self):
        return (self.delivered_quantity or 0) - (self.invoiced_quantity or 0)


# ---------------------------------------------------------------------------
# Delivery note
# ---------------------------------------------------------------------------

class DeliveryNote(SalesDocumentMixin, db.Model):
    __tablename__ = "delivery_notes"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_orders.id"), index=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True
    )
    delivery_date = db.Column(db.Date, nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    delivery_address = db.Column(db.Text)
    # Transport (kept on the delivery so trip costing can be reconciled).
    transport_fee = db.Column(MONEY, nullable=False, default=0)
    trip_id = db.Column(db.Integer, db.ForeignKey("trips.id"))
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"))
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"))
    carrier_id = db.Column(db.Integer, db.ForeignKey("carriers.id"))
    received_by_name = db.Column(db.String(120))
    received_at = db.Column(db.DateTime)
    stock_consumed = db.Column(db.Boolean, nullable=False, default=False)

    customer = db.relationship("Customer", lazy="joined")
    warehouse = db.relationship("Warehouse", lazy="joined")
    items = db.relationship(
        "DeliveryNoteItem", backref="delivery", cascade="all, delete-orphan", lazy="select"
    )
    invoices = db.relationship(
        "SalesInvoice", backref="delivery_note", lazy="select",
        foreign_keys="SalesInvoice.delivery_note_id",
    )


class DeliveryNoteItem(LineMixin, db.Model):
    __tablename__ = "delivery_note_items"

    id = db.Column(db.Integer, primary_key=True)
    delivery_note_id = db.Column(
        db.Integer, db.ForeignKey("delivery_notes.id"), nullable=False, index=True
    )
    order_item_id = db.Column(db.Integer, db.ForeignKey("sales_order_items.id"))
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    invoiced_quantity = db.Column(QUANTITY, nullable=False, default=0)
    weighing_ticket_id = db.Column(db.Integer, db.ForeignKey("weighing_tickets.id"))

    product = db.relationship("Product", lazy="joined")
    order_item = db.relationship("SalesOrderItem", lazy="select")

    @property
    def remaining_to_invoice(self):
        return (self.quantity or 0) - (self.invoiced_quantity or 0)


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------

class SalesInvoice(SalesDocumentMixin, db.Model):
    __tablename__ = "sales_invoices"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_orders.id"), index=True)
    delivery_note_id = db.Column(
        db.Integer, db.ForeignKey("delivery_notes.id"), index=True
    )
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True
    )
    invoice_date = db.Column(db.Date, nullable=False, index=True)
    due_date = db.Column(db.Date, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    printed_at = db.Column(db.DateTime)

    customer = db.relationship("Customer", lazy="joined")
    items = db.relationship(
        "SalesInvoiceItem", backref="invoice", cascade="all, delete-orphan", lazy="select"
    )
    credit_notes = db.relationship(
        "CreditNote", backref="invoice", lazy="select",
        foreign_keys="CreditNote.invoice_id",
    )

    # --- Derived amounts (always computed from allocations, never stored) ---
    @property
    def paid_amount(self) -> Decimal:
        from app.models.payments import Payment, PaymentAllocation

        total = (
            db.session.query(db.func.coalesce(db.func.sum(PaymentAllocation.amount), 0))
            .join(Payment, Payment.id == PaymentAllocation.payment_id)
            .filter(
                PaymentAllocation.document_type == "sales_invoice",
                PaymentAllocation.document_id == self.id,
                Payment.status == DocumentStatus.POSTED.value,
            )
            .scalar()
        )
        return Decimal(str(total or 0))

    @property
    def credited_amount(self) -> Decimal:
        total = (
            db.session.query(db.func.coalesce(db.func.sum(CreditNote.total_amount), 0))
            .filter(
                CreditNote.invoice_id == self.id,
                CreditNote.status != DocumentStatus.CANCELLED.value,
            )
            .scalar()
        )
        return Decimal(str(total or 0))

    @property
    def balance_due(self) -> Decimal:
        return (
            (self.total_amount or Decimal("0")) - self.paid_amount - self.credited_amount
        ).quantize(Decimal("0.01"))

    @property
    def settlement_status(self) -> str:
        if self.status in {DocumentStatus.DRAFT.value, DocumentStatus.CANCELLED.value}:
            return self.status
        if self.balance_due <= 0:
            return "paid"
        if self.paid_amount > 0 or self.credited_amount > 0:
            return "partial"
        return "unpaid"


class SalesInvoiceItem(LineMixin, db.Model):
    __tablename__ = "sales_invoice_items"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(
        db.Integer, db.ForeignKey("sales_invoices.id"), nullable=False, index=True
    )
    order_item_id = db.Column(db.Integer, db.ForeignKey("sales_order_items.id"))
    delivery_item_id = db.Column(db.Integer, db.ForeignKey("delivery_note_items.id"))
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)

    product = db.relationship("Product", lazy="joined")


# ---------------------------------------------------------------------------
# Credit note (customer return)
# ---------------------------------------------------------------------------

class CreditNote(SalesDocumentMixin, db.Model):
    """Customer return.  Cancels value and (when goods come back) restores stock."""

    __tablename__ = "credit_notes"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("sales_invoices.id"), index=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True
    )
    credit_note_date = db.Column(db.Date, nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    reason = db.Column(db.String(255), nullable=False)
    restock = db.Column(db.Boolean, nullable=False, default=True)

    customer = db.relationship("Customer", lazy="joined")
    items = db.relationship(
        "CreditNoteItem", backref="credit_note", cascade="all, delete-orphan", lazy="select"
    )


class CreditNoteItem(LineMixin, db.Model):
    __tablename__ = "credit_note_items"

    id = db.Column(db.Integer, primary_key=True)
    credit_note_id = db.Column(
        db.Integer, db.ForeignKey("credit_notes.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    invoice_item_id = db.Column(db.Integer, db.ForeignKey("sales_invoice_items.id"))

    product = db.relationship("Product", lazy="joined")


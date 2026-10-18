"""Accounts receivable / payable: payments and their allocation to documents.

Design rules:

* A payment is never deleted.  Correcting one creates a *reversal*, so the
  financial history stays complete (``status`` becomes ``reversed``).
* A payment may be allocated across several invoices, and any remainder stays
  unallocated as a customer advance or supplier advance.
* Allocation cannot exceed either the payment amount or the document balance;
  this is enforced in :mod:`app.services.payments` inside a transaction.
"""
from __future__ import annotations

from decimal import Decimal

from app.extensions import db
from app.models.base import (
    MONEY,
    ActorMixin,
    DocumentStatus,
    PaymentDirection,
    PaymentMethod,
    TimestampMixin,
)


class Payment(TimestampMixin, ActorMixin, db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, index=True)
    direction = db.Column(
        db.String(10), nullable=False, default=PaymentDirection.INBOUND.value, index=True
    )
    payment_date = db.Column(db.Date, nullable=False, index=True)
    amount = db.Column(MONEY, nullable=False)
    method = db.Column(
        db.String(20), nullable=False, default=PaymentMethod.CASH.value, index=True
    )
    reference = db.Column(db.String(80))
    notes = db.Column(db.Text)

    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), index=True)

    status = db.Column(
        db.String(12), nullable=False, default=DocumentStatus.POSTED.value, index=True
    )
    reversed_at = db.Column(db.DateTime)
    reversed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reversal_reason = db.Column(db.String(255))

    customer = db.relationship("Customer", lazy="joined")
    supplier = db.relationship("Supplier", lazy="joined")
    reversed_by = db.relationship("User", foreign_keys=[reversed_by_id], lazy="joined")
    allocations = db.relationship(
        "PaymentAllocation", backref="payment",
        cascade="all, delete-orphan", lazy="select",
    )

    @property
    def allocated_amount(self) -> Decimal:
        total = sum((row.amount or Decimal("0") for row in self.allocations), Decimal("0"))
        return total.quantize(Decimal("0.01"))

    @property
    def unallocated_amount(self) -> Decimal:
        return ((self.amount or Decimal("0")) - self.allocated_amount).quantize(
            Decimal("0.01")
        )

    @property
    def is_reversed(self) -> bool:
        return self.status == DocumentStatus.REVERSED.value

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Payment {self.number} {self.direction} {self.amount}>"


class PaymentAllocation(TimestampMixin, ActorMixin, db.Model):
    """Applies part of a payment to one invoice (customer or supplier)."""

    __tablename__ = "payment_allocations"
    __table_args__ = (
        db.UniqueConstraint(
            "payment_id", "document_type", "document_id", name="uq_payment_allocation"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(
        db.Integer, db.ForeignKey("payments.id"), nullable=False, index=True
    )
    #: "sales_invoice" (receivable) or "supplier_invoice" (payable).
    document_type = db.Column(db.String(30), nullable=False, index=True)
    document_id = db.Column(db.Integer, nullable=False, index=True)
    amount = db.Column(MONEY, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PaymentAllocation {self.document_type}#{self.document_id} {self.amount}>"

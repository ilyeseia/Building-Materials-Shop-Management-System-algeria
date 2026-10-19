"""Receivables and payables: recording payments and allocating them.

Design rules enforced here
--------------------------
* A payment is **never deleted**.  Correcting one calls
  :func:`reverse_payment`, which marks it ``reversed`` and leaves the allocation
  history intact; reversed payments stop counting towards a document's paid
  amount because the balance queries join the payment and require ``posted``.
* An allocation can never exceed the payment's unallocated amount **or** the
  document's remaining balance.  Both checks happen in one transaction.
* The direction, the party and the document type must agree: money from a
  customer settles a sales invoice, money to a supplier settles a supplier
  invoice.  Mixing them is refused rather than silently accepted.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    CreditNote,
    Customer,
    DocumentStatus,
    Payment,
    PaymentAllocation,
    PaymentDirection,
    PaymentMethod,
    SalesInvoice,
    Supplier,
    SupplierInvoice,
)
from app.services import audit, numbering, uom
from app.services.errors import InvalidTransition, NotFoundError, ValidationError
from app.services.timeutil import today_local, utcnow

MONEY_PLACES = Decimal("0.01")

SALES_INVOICE = "sales_invoice"
SUPPLIER_INVOICE = "supplier_invoice"

#: Document type → (model, party field) for the two settlement directions.
_DOCUMENT_TYPES = {
    SALES_INVOICE: (SalesInvoice, "customer_id"),
    SUPPLIER_INVOICE: (SupplierInvoice, "supplier_id"),
}

_DIRECTION_FOR = {
    SALES_INVOICE: PaymentDirection.INBOUND.value,
    SUPPLIER_INVOICE: PaymentDirection.OUTBOUND.value,
}


# ---------------------------------------------------------------------------
# Document helpers
# ---------------------------------------------------------------------------

def load_document(document_type: str, document_id: int):
    """Return the invoice a ``document_type``/``document_id`` pair refers to."""
    entry = _DOCUMENT_TYPES.get(document_type)
    if entry is None:
        raise ValidationError("payment.bad_document_type")
    model, _party_field = entry
    document = db.session.get(model, int(document_id))
    if document is None:
        raise NotFoundError("error.not_found")
    return document


def document_balance(document) -> Decimal:
    """Outstanding balance of an invoice (total − paid − credited)."""
    return Decimal(document.balance_due or 0)


# ---------------------------------------------------------------------------
# Concurrency-safe loading
# ---------------------------------------------------------------------------
# Two cashiers can submit a receipt for the same invoice at the same time.  A
# plain ``SELECT`` followed by an application-level check is not sufficient:
# both requests see the old balance and both insert an allocation, over-settling
# the invoice.  Every write path therefore locks the payment and the invoice
# with ``SELECT ... FOR UPDATE`` *and* reads the allocation rows with a locking
# read.  The locking read is important under MySQL's REPEATABLE READ: a normal
# SELECT can keep using the transaction snapshot taken before the other
# cashier committed.
#
# SQLite ignores ``FOR UPDATE`` (it serialises writers with a database lock),
# so the same code remains portable for development and the test suite.

def _lock_payment(payment: Payment) -> Payment:
    """Re-load ``payment`` with its allocations and hold a row lock."""
    if payment.id is None:  # pragma: no cover - callers flush first
        return payment
    locked = db.session.execute(
        select(Payment)
        .options(joinedload(Payment.allocations))
        .where(Payment.id == payment.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).unique().scalar_one_or_none()
    if locked is None:
        raise NotFoundError("error.not_found")
    return locked


def _lock_document(document_type: str, document_id: int):
    """Re-load an invoice and hold a row lock for the rest of the transaction."""
    entry = _DOCUMENT_TYPES.get(document_type)
    if entry is None:
        raise ValidationError("payment.bad_document_type")
    model, _party_field = entry
    document = db.session.execute(
        select(model)
        .where(model.id == int(document_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if document is None:
        raise NotFoundError("error.not_found")
    return document


def _locked_paid_amount(document_type: str, document_id: int) -> Decimal:
    """Sum posted allocations using a locking read (latest committed rows)."""
    rows = db.session.execute(
        select(PaymentAllocation.amount, Payment.status)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(
            PaymentAllocation.document_type == document_type,
            PaymentAllocation.document_id == int(document_id),
        )
        .with_for_update(of=PaymentAllocation)
    ).all()
    total = sum(
        (Decimal(str(amount)) for amount, status in rows if status == DocumentStatus.POSTED.value),
        Decimal("0"),
    )
    return total.quantize(MONEY_PLACES)


def _locked_credited_amount(invoice_id: int) -> Decimal:
    """Sum non-cancelled credit notes with a locking read."""
    rows = db.session.execute(
        select(CreditNote.total_amount)
        .where(
            CreditNote.invoice_id == invoice_id,
            CreditNote.status != DocumentStatus.CANCELLED.value,
        )
        .with_for_update()
    ).scalars().all()
    total = sum((Decimal(str(value or 0)) for value in rows), Decimal("0"))
    return total.quantize(MONEY_PLACES)


def _document_balance_locked(document_type: str, document) -> Decimal:
    paid = _locked_paid_amount(document_type, document.id)
    credited = (
        _locked_credited_amount(document.id)
        if document_type == SALES_INVOICE
        else Decimal("0")
    )
    return (Decimal(document.total_amount or 0) - paid - credited).quantize(MONEY_PLACES)


def open_documents(document_type: str, party_id: int) -> list:
    """Outstanding invoices of a party, oldest first (for auto-allocation)."""
    entry = _DOCUMENT_TYPES.get(document_type)
    if entry is None:
        raise ValidationError("payment.bad_document_type")
    model, party_field = entry
    date_field = SalesInvoice.invoice_date if model is SalesInvoice else SupplierInvoice.invoice_date
    documents = db.session.execute(
        select(model)
        .filter(
            getattr(model, party_field) == party_id,
            model.status.notin_(
                [DocumentStatus.DRAFT.value, DocumentStatus.CANCELLED.value]
            ),
        )
        .order_by(date_field, model.id)
    ).scalars().all()
    return [doc for doc in documents if document_balance(doc) > 0]


# ---------------------------------------------------------------------------
# Recording payments
# ---------------------------------------------------------------------------

def record_payment(
    *,
    direction: str,
    amount,
    customer: Customer | None = None,
    supplier: Supplier | None = None,
    payment_date: date | None = None,
    method: str | None = None,
    reference: str | None = None,
    notes: str | None = None,
    allocations: list[dict] | None = None,
    auto_allocate: bool = False,
    actor=None,
) -> Payment:
    """Record a customer receipt or a supplier payment.

    ``allocations`` is a list of ``{"document_type", "document_id", "amount"}``.
    Anything left unallocated stays as an advance; with ``auto_allocate`` the
    remaining amount is applied to the party's oldest open invoices.
    """
    value = uom.parse_money(amount)
    if value <= 0:
        raise ValidationError("val.must_be_positive")
    if direction not in {PaymentDirection.INBOUND.value, PaymentDirection.OUTBOUND.value}:
        raise ValidationError("payment.bad_direction")

    if direction == PaymentDirection.INBOUND.value:
        if customer is None or supplier is not None:
            raise ValidationError("sale.customer_required")
    else:
        if supplier is None:
            raise ValidationError("val.required")

    method = method or PaymentMethod.CASH.value
    if method not in {m.value for m in PaymentMethod}:
        raise ValidationError("payment.bad_method")

    sequence = "customer_payment" if direction == PaymentDirection.INBOUND.value else "supplier_payment"
    payment = Payment(
        number=numbering.allocate(sequence),
        direction=direction,
        payment_date=payment_date or today_local(),
        amount=value,
        method=method,
        reference=reference,
        notes=notes,
        customer_id=customer.id if customer else None,
        supplier_id=supplier.id if supplier else None,
        status=DocumentStatus.POSTED.value,
        created_by_id=getattr(actor, "id", None),
    )
    db.session.add(payment)
    db.session.flush()

    for entry in allocations or []:
        allocate(
            payment,
            entry.get("document_type"),
            int(entry.get("document_id")),
            entry.get("amount"),
            actor=actor,
        )

    if auto_allocate and payment.unallocated_amount > 0:
        auto_allocate_payment(payment, actor=actor)

    audit.record(
        "recorded", "payment", payment.id,
        summary=f"{payment.number} {direction} {value}",
    )
    return payment


def allocate(
    payment: Payment,
    document_type: str,
    document_id: int,
    amount,
    *,
    actor=None,
) -> PaymentAllocation:
    """Apply part of ``payment`` to one invoice, enforcing both ceilings.

    The payment and invoice rows are locked for the duration of the
    transaction.  The second of two concurrent requests therefore sees the
    allocation committed by the first one instead of over-settling the
    invoice or the payment.
    """
    # Read the payment direction first, then lock the invoice, then lock the
    # payment.  Every allocation path uses the same invoice -> payment order,
    # so two cashiers settling the same invoice cannot deadlock over each
    # other's payment row.
    transaction = _DIRECTION_FOR.get(document_type)
    if transaction is None:
        raise ValidationError("payment.bad_document_type")
    if payment.status != DocumentStatus.POSTED.value:
        raise InvalidTransition("payment.reversed_locked")
    if transaction != payment.direction:
        raise ValidationError("payment.direction_mismatch")

    value = uom.parse_money(amount)
    if value <= 0:
        raise ValidationError("val.must_be_positive")

    expected_party = (
        payment.customer_id
        if payment.direction == PaymentDirection.INBOUND.value
        else payment.supplier_id
    )
    if expected_party is None:
        raise ValidationError("val.required")

    document = _lock_document(document_type, document_id)
    payment = _lock_payment(payment)
    if payment.status != DocumentStatus.POSTED.value:
        raise InvalidTransition("payment.reversed_locked")
    if transaction != payment.direction:
        raise ValidationError("payment.direction_mismatch")
    expected_party = (
        payment.customer_id
        if payment.direction == PaymentDirection.INBOUND.value
        else payment.supplier_id
    )
    _model, party_field = _DOCUMENT_TYPES[document_type]
    if getattr(document, party_field) != expected_party:
        raise ValidationError("payment.party_mismatch")
    if document.status in {DocumentStatus.DRAFT.value, DocumentStatus.CANCELLED.value}:
        raise InvalidTransition("payment.not_allocatable")

    unallocated = payment.unallocated_amount
    if value > unallocated:
        raise ValidationError("payment.exceeds_payment", amount=str(unallocated))
    balance = _document_balance_locked(document_type, document)
    if value > balance:
        raise ValidationError("payment.exceeds_balance", amount=str(balance))

    allocation = PaymentAllocation(
        # ``payment=payment`` (not ``payment_id=``) so the in-memory collection is
        # updated immediately; otherwise ``payment.unallocated_amount`` stays
        # stale inside this session and ``auto_allocate_payment`` would re-apply
        # the whole amount to the next invoice.
        payment=payment,
        document_type=document_type,
        document_id=document.id,
        amount=value,
        created_by_id=getattr(actor, "id", None),
    )
    db.session.add(allocation)
    db.session.flush()
    return allocation


def auto_allocate_payment(payment: Payment, *, actor=None) -> list[PaymentAllocation]:
    """Apply the unallocated remainder to the party's oldest open invoices."""
    if payment.status != DocumentStatus.POSTED.value:
        raise InvalidTransition("payment.reversed_locked")

    if payment.direction == PaymentDirection.INBOUND.value:
        document_type, party_id = SALES_INVOICE, payment.customer_id
    else:
        document_type, party_id = SUPPLIER_INVOICE, payment.supplier_id

    created: list[PaymentAllocation] = []
    if party_id is None:
        return created

    remaining = payment.unallocated_amount
    for document in open_documents(document_type, party_id):
        if remaining <= 0:
            break
        balance = document_balance(document)
        applied = min(remaining, balance)
        if applied <= 0:
            continue
        created.append(allocate(payment, document_type, document.id, applied, actor=actor))
        remaining = payment.unallocated_amount

    return created


def reverse_payment(payment: Payment, reason: str, *, actor=None) -> Payment:
    """Reverse a payment without deleting it; the allocations stay as history."""
    if payment.status == DocumentStatus.REVERSED.value:
        raise InvalidTransition()
    if not reason:
        raise ValidationError("val.required")
    payment.status = DocumentStatus.REVERSED.value
    payment.reversed_at = utcnow()
    payment.reversed_by_id = getattr(actor, "id", None)
    payment.reversal_reason = reason
    audit.record("reversed", "payment", payment.id, summary=f"{payment.number}: {reason}")
    return payment


# ---------------------------------------------------------------------------
# Party balances
# ---------------------------------------------------------------------------

def customer_balance(customer_id: int) -> Decimal:
    """Total outstanding on a customer's sales invoices (receivable)."""
    invoices = db.session.execute(
        select(SalesInvoice).filter(
            SalesInvoice.customer_id == customer_id,
            SalesInvoice.status.notin_(
                [DocumentStatus.DRAFT.value, DocumentStatus.CANCELLED.value]
            ),
        )
    ).scalars().all()
    total = sum((document_balance(inv) for inv in invoices), Decimal("0"))
    return total.quantize(MONEY_PLACES)


def supplier_balance(supplier_id: int) -> Decimal:
    """Total outstanding on a supplier's invoices (payable)."""
    invoices = db.session.execute(
        select(SupplierInvoice).filter(
            SupplierInvoice.supplier_id == supplier_id,
            SupplierInvoice.status.notin_(
                [DocumentStatus.DRAFT.value, DocumentStatus.CANCELLED.value]
            ),
        )
    ).scalars().all()
    total = sum((document_balance(inv) for inv in invoices), Decimal("0"))
    return total.quantize(MONEY_PLACES)


def customer_credit(customer_id: int) -> Decimal:
    """Unallocated customer payments (advances / overpayments)."""
    payments = db.session.execute(
        select(Payment).filter(
            Payment.customer_id == customer_id,
            Payment.direction == PaymentDirection.INBOUND.value,
            Payment.status == DocumentStatus.POSTED.value,
        )
    ).scalars().all()
    total = sum((p.unallocated_amount for p in payments), Decimal("0"))
    return total.quantize(MONEY_PLACES)

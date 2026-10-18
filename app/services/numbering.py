"""Atomic document numbering.

Numbers are allocated from :class:`~app.models.platform.NumberSequence` inside the
current transaction, taking a row lock so two concurrent requests cannot receive
the same number.  A number is never reused, even when the document is cancelled —
a gap in a sequence is far cheaper than two documents sharing an identity.
"""
from __future__ import annotations

from sqlalchemy import select

from app.extensions import db
from app.models import NumberSequence
from app.services.errors import NotFoundError

DEFAULT_SEQUENCES: tuple[tuple[str, str, str], ...] = (
    # (code, label translation key, prefix)
    ("quotation", "sale.quotation", "DEV-"),
    ("sales_order", "sale.order", "BC-"),
    ("delivery_note", "sale.delivery", "BL-"),
    ("sales_invoice", "sale.invoice", "FAC-"),
    ("credit_note", "sale.credit_note", "AV-"),
    ("purchase_order", "purchase.order", "BCF-"),
    ("goods_receipt", "purchase.receipt", "BR-"),
    ("supplier_invoice", "purchase.supplier_invoice", "FF-"),
    ("purchase_return", "nav.returns", "RF-"),
    ("customer_payment", "payment.receipt", "ENC-"),
    ("supplier_payment", "payment.voucher", "REG-"),
    ("weighing_ticket", "weigh.ticket", "PB-"),
    ("trip", "fleet.trip", "VY-"),
    ("stock_transfer", "stock.transfer", "TR-"),
    ("stock_count", "stock.count", "INV-"),
    ("stock_adjustment", "stock.adjust", "AJ-"),
)


def ensure_sequences() -> None:
    """Create any missing sequence rows (idempotent; used by seeding and tests)."""
    existing = {
        row.code for row in db.session.execute(select(NumberSequence)).scalars().all()
    }
    created = False
    for code, label_key, prefix in DEFAULT_SEQUENCES:
        if code in existing:
            continue
        db.session.add(
            NumberSequence(code=code, label_key=label_key, prefix=prefix, next_number=1)
        )
        created = True
    if created:
        db.session.flush()


def peek(code: str) -> str:
    """Preview the next number without consuming it (for forms)."""
    sequence = db.session.execute(
        select(NumberSequence).filter_by(code=code)
    ).scalars().first()
    if sequence is None:
        return ""
    return f"{sequence.prefix}{sequence.next_number:0{sequence.padding}d}"


def allocate(code: str) -> str:
    """Consume and return the next document number for ``code``."""
    sequence = db.session.execute(
        select(NumberSequence).filter_by(code=code).with_for_update()
    ).scalars().first()
    if sequence is None:
        raise NotFoundError("error.server")
    number = f"{sequence.prefix}{sequence.next_number:0{sequence.padding}d}"
    sequence.next_number = sequence.next_number + 1
    db.session.flush()
    return number

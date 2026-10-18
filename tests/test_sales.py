"""Sales workflow: quotation → order → delivery → invoice → payment, plus returns.

The tests assert *money* and *stock* outcomes, never that "a 200 came back":
the audited revision fabricated a 50 % payment on a "Partial" status, which is
exactly the kind of behaviour these checks exist to prevent.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Customer,
    DeliveryNote,
    DocumentStatus,
    PaymentMethod,
    PaymentDirection,
    Quotation,
    SalesInvoice,
    SalesOrder,
    StockLevel,
)
from app.services import payments, sales, stock
from app.services.errors import InsufficientStock, InvalidTransition, ValidationError
from app.services.sales import LineInput

from tests.conftest import give_stock, today


VAT = Decimal("0.19")


def _lines(product, quantity="10", **extra) -> list[LineInput]:
    return [LineInput(product_id=product.id, quantity=Decimal(quantity), **extra)]


# ---------------------------------------------------------------------------
# Quotation and order
# ---------------------------------------------------------------------------

def test_quotation_does_not_touch_stock(customer, product, warehouse):
    give_stock(product, warehouse, "50")

    quotation = sales.create_quotation(customer=customer, lines=_lines(product, "10"))

    assert isinstance(quotation, Quotation)
    assert quotation.status == DocumentStatus.DRAFT.value
    assert quotation.number
    assert stock.on_hand(product, warehouse) == Decimal("50")
    assert stock.available(product, warehouse) == Decimal("50")


def test_accepting_a_quotation_creates_a_draft_order(customer, product, warehouse):
    give_stock(product, warehouse, "50")
    quotation = sales.create_quotation(customer=customer, lines=_lines(product, "10"))

    order = sales.accept_quotation(quotation)

    assert isinstance(order, SalesOrder)
    assert order.status == DocumentStatus.DRAFT.value
    assert order.quotation_id == quotation.id
    assert stock.available(product, warehouse) == Decimal("50")   # still not reserved


def test_confirming_an_order_reserves_without_removing_stock(customer, product, warehouse):
    give_stock(product, warehouse, "50")
    order = sales.create_order(customer=customer, lines=_lines(product, "10"))

    sales.confirm_order(order)

    level = db.session.get(StockLevel, (product.id, warehouse.id)) if False else stock.get_level(product, warehouse)
    assert order.status == DocumentStatus.CONFIRMED.value
    assert level.quantity == Decimal("50")            # physically untouched
    assert level.reserved == Decimal("10")
    assert level.available == Decimal("40")


def test_cannot_confirm_an_order_beyond_available_stock(customer, product, warehouse):
    give_stock(product, warehouse, "5")
    order = sales.create_order(customer=customer, lines=_lines(product, "10"))

    with pytest.raises(InsufficientStock):
        sales.confirm_order(order)

    assert stock.get_level(product, warehouse).reserved == Decimal("0")


def test_cancelling_a_confirmed_order_releases_the_reservation(customer, product, warehouse):
    give_stock(product, warehouse, "50")
    order = sales.create_order(customer=customer, lines=_lines(product, "10"), confirm=True)

    sales.cancel_order(order, reason="client a annulé")

    assert order.status == DocumentStatus.CANCELLED.value
    level = stock.get_level(product, warehouse)
    assert level.reserved == Decimal("0")
    assert level.available == Decimal("50")


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def test_delivering_consumes_the_reservation_once(customer, product, warehouse):
    give_stock(product, warehouse, "50")
    order = sales.create_order(customer=customer, lines=_lines(product, "10"), confirm=True)

    delivery = sales.deliver_order(order)

    assert isinstance(delivery, DeliveryNote)
    assert delivery.status == DocumentStatus.DELIVERED.value
    level = stock.get_level(product, warehouse)
    assert level.quantity == Decimal("40")     # deducted once, not twice
    assert level.reserved == Decimal("0")
    assert level.available == Decimal("40")
    assert order.status == DocumentStatus.DELIVERED.value


def test_cannot_deliver_more_than_was_ordered(customer, product, warehouse):
    give_stock(product, warehouse, "50")
    order = sales.create_order(customer=customer, lines=_lines(product, "10"), confirm=True)
    item = order.items[0]

    with pytest.raises(ValidationError):
        sales.deliver_order(
            order,
            deliveries=[{"order_item_id": item.id, "quantity": Decimal("11")}],
        )

    assert stock.on_hand(product, warehouse) == Decimal("50")


def test_partial_delivery_keeps_the_order_open(customer, product, warehouse):
    give_stock(product, warehouse, "50")
    order = sales.create_order(customer=customer, lines=_lines(product, "10"), confirm=True)
    item = order.items[0]

    sales.deliver_order(order, deliveries=[{"order_item_id": item.id, "quantity": Decimal("4")}])

    assert order.status == DocumentStatus.PARTIALLY_DELIVERED.value
    assert stock.on_hand(product, warehouse) == Decimal("46")
    assert stock.get_level(product, warehouse).reserved == Decimal("6")


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------

def test_invoice_totals_are_decimal_and_consistent(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")
    order = sales.create_order(customer=customer, lines=_lines(product, "10"), confirm=True)
    delivery = sales.deliver_order(order)

    invoice = sales.create_invoice_from_delivery(delivery)

    assert isinstance(invoice, SalesInvoice)
    assert isinstance(invoice.subtotal, Decimal)
    assert isinstance(invoice.tax_amount, Decimal)
    assert isinstance(invoice.total_amount, Decimal)
    # 10 × 1800 = 18 000 HT ; TVA 19 % = 3 420 ; TTC = 21 420
    assert invoice.subtotal == Decimal("18000.00")
    assert invoice.tax_amount == Decimal("3420.00")
    assert invoice.total_amount == Decimal("21420.00")
    assert invoice.gross_total == Decimal("18000.00")     # no discount on this order
    assert invoice.subtotal + invoice.tax_amount - invoice.discount_amount == invoice.total_amount
    assert invoice.vat_rate == Decimal("19.00")


def test_invoice_snapshots_the_cost_for_a_historical_margin(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")
    order = sales.create_order(customer=customer, lines=_lines(product, "10"), confirm=True)
    delivery = sales.deliver_order(order)
    invoice = sales.create_invoice_from_delivery(delivery)

    item = invoice.items[0]
    assert Decimal(item.unit_cost) == Decimal("1000")
    # Margin uses the *snapshot*, so a later price change cannot rewrite history.
    assert item.margin_amount == Decimal("8000.00")


def test_discounted_line_reduces_the_taxable_base(customer, product, warehouse):
    give_stock(product, warehouse, "50")
    order = sales.create_order(
        customer=customer,
        lines=[LineInput(product_id=product.id, quantity=Decimal("10"), discount_percent=Decimal("10"))],
        confirm=True,
    )
    delivery = sales.deliver_order(order)
    invoice = sales.create_invoice_from_delivery(delivery)

    # ``subtotal`` is the taxable base *after* the line discount; ``gross_total``
    # is the pre-discount figure and is derived, never stored.
    assert invoice.gross_total == Decimal("18000.00")
    assert invoice.discount_amount == Decimal("1800.00")     # 10 % of 18 000
    assert invoice.subtotal == Decimal("16200.00")
    assert invoice.tax_amount == Decimal("3078.00")
    assert invoice.total_amount == Decimal("19278.00")       # (18 000 − 1 800) × 1.19


def test_an_invoice_cannot_be_created_from_an_undelivered_document(customer, product, warehouse):
    give_stock(product, warehouse, "50")
    quotation = sales.create_quotation(customer=customer, lines=_lines(product, "10"))
    with pytest.raises(InvalidTransition):
        sales.create_invoice_from_delivery(quotation)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

def test_paying_an_invoice_clears_its_balance(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")
    invoice = _issued_invoice(customer, product, warehouse)

    payment = payments.record_payment(
        direction=PaymentDirection.INBOUND.value,
        amount=invoice.total_amount,
        customer=customer,
        method=PaymentMethod.CASH.value,
        allocations=[{
            "document_type": "sales_invoice",
            "document_id": invoice.id,
            "amount": invoice.total_amount,
        }],
    )

    assert payment.amount == Decimal("21420.00")
    assert invoice.paid_amount == Decimal("21420.00")
    assert invoice.balance_due == Decimal("0.00")
    assert invoice.settlement_status == "paid"
    assert payments.customer_balance(customer.id) == Decimal("0.00")


def test_a_partial_payment_is_not_reported_as_paid(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")
    invoice = _issued_invoice(customer, product, warehouse)

    payments.record_payment(
        direction=PaymentDirection.INBOUND.value,
        amount=Decimal("10000"),
        customer=customer,
        allocations=[{"document_type": "sales_invoice", "document_id": invoice.id, "amount": Decimal("10000")}],
    )

    assert invoice.paid_amount == Decimal("10000.00")
    assert invoice.settlement_status == "partial"
    assert invoice.balance_due == Decimal("11420.00")
    # The audited revision invented a 50 % payment here; nothing is invented now.
    assert invoice.paid_amount != invoice.total_amount / 2


def test_an_overpayment_is_refused(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")
    invoice = _issued_invoice(customer, product, warehouse)

    with pytest.raises(ValidationError) as error:
        payments.record_payment(
            direction=PaymentDirection.INBOUND.value,
            amount=Decimal("99999"),
            customer=customer,
            allocations=[{"document_type": "sales_invoice", "document_id": invoice.id, "amount": Decimal("99999")}],
        )
    assert error.value.message_key in {"payment.exceeds_balance", "payment.exceeds_payment"}
    assert invoice.paid_amount == Decimal("0.00")


def test_a_payment_cannot_settle_another_customers_invoice(customer, make_customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")
    invoice = _issued_invoice(customer, product, warehouse)
    other = make_customer("SARL Autre")

    with pytest.raises(ValidationError):
        payments.record_payment(
            direction=PaymentDirection.INBOUND.value,
            amount=Decimal("1000"),
            customer=other,
            allocations=[{"document_type": "sales_invoice", "document_id": invoice.id, "amount": Decimal("1000")}],
        )


def test_unallocated_remainder_stays_as_a_customer_advance(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")
    invoice = _issued_invoice(customer, product, warehouse)

    payment = payments.record_payment(
        direction=PaymentDirection.INBOUND.value,
        amount=Decimal("30000"),
        customer=customer,
        allocations=[{"document_type": "sales_invoice", "document_id": invoice.id, "amount": invoice.total_amount}],
    )

    assert payment.unallocated_amount == Decimal("8580.00")
    assert payments.customer_credit(customer.id) == Decimal("8580.00")
    assert invoice.settlement_status == "paid"


def test_reversing_a_payment_restores_the_balance(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")
    invoice = _issued_invoice(customer, product, warehouse)
    payment = payments.record_payment(
        direction=PaymentDirection.INBOUND.value,
        amount=invoice.total_amount,
        customer=customer,
        allocations=[{"document_type": "sales_invoice", "document_id": invoice.id, "amount": invoice.total_amount}],
    )

    payments.reverse_payment(payment, "erreur de caisse")

    assert payment.status == DocumentStatus.REVERSED.value
    assert invoice.paid_amount == Decimal("0.00")
    assert invoice.settlement_status == "unpaid"


# ---------------------------------------------------------------------------
# Direct sale and credit note
# ---------------------------------------------------------------------------

def test_direct_sale_creates_a_delivery_and_an_invoice(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")

    delivery, invoice = sales.direct_sale(customer=customer, lines=_lines(product, "5"))

    assert delivery.status == DocumentStatus.DELIVERED.value
    assert invoice.total_amount == Decimal("10710.00")     # 5 × 1800 × 1.19
    assert stock.on_hand(product, warehouse) == Decimal("45")
    assert invoice.balance_due == invoice.total_amount      # unpaid unless a payment is given


def test_direct_sale_with_an_upfront_payment(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")

    _delivery, invoice = sales.direct_sale(
        customer=customer,
        lines=_lines(product, "5"),
        payment_method=PaymentMethod.CASH.value,
        paid_amount=Decimal("10710"),
    )

    assert invoice.balance_due == Decimal("0.00")
    assert invoice.settlement_status == "paid"


def test_credit_note_restocks_and_reduces_the_invoice_balance(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")
    invoice = _issued_invoice(customer, product, warehouse)

    note = sales.create_credit_note(
        invoice=invoice,
        lines=[LineInput(product_id=product.id, quantity=Decimal("2"))],
        reason="2 m³ cassés au transport",
        restock=True,
    )

    assert note.total_amount == Decimal("4284.00")          # 2 × 1800 × 1.19
    assert stock.on_hand(product, warehouse) == Decimal("42")   # 10 delivered, 2 returned
    assert invoice.balance_due == Decimal("17136.00")
    assert invoice.settlement_status == "partial"


def test_credit_note_requires_a_reason(customer, product, warehouse):
    give_stock(product, warehouse, "50", unit_cost="1000")
    invoice = _issued_invoice(customer, product, warehouse)

    with pytest.raises(ValidationError):
        sales.create_credit_note(
            invoice=invoice,
            lines=[LineInput(product_id=product.id, quantity=Decimal("1"))],
            reason="",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _issued_invoice(customer, product, warehouse, quantity: str = "10") -> SalesInvoice:
    order = sales.create_order(customer=customer, lines=_lines(product, quantity), confirm=True)
    delivery = sales.deliver_order(order)
    return sales.create_invoice_from_delivery(delivery)

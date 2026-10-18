"""Purchasing: order → receipt → supplier invoice → payment, plus returns.

The interesting behaviour here is *cost*: a receipt must update the weighted
average cost including the landed costs the shop actually paid, and a supplier
invoice must not drift away from the receipt it came from.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    DocumentStatus,
    GoodsReceipt,
    PaymentDirection,
    PurchaseOrder,
    PurchaseReturn,
    SupplierInvoice,
)
from app.services import payments, purchasing, stock
from app.services.errors import InsufficientStock, InvalidTransition, ValidationError
from app.services.purchasing import PurchaseLineInput

from tests.conftest import give_stock


def _lines(product, quantity="10", unit_price=None, **extra) -> list[PurchaseLineInput]:
    return [
        PurchaseLineInput(
            product_id=product.id,
            quantity=Decimal(quantity),
            unit_price=Decimal(unit_price) if unit_price is not None else None,
            **extra,
        )
    ]


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------

def test_purchase_order_is_a_draft_and_does_not_move_stock(supplier, product, warehouse):
    order = purchasing.create_order(supplier=supplier, lines=_lines(product, "100", "900"))

    assert isinstance(order, PurchaseOrder)
    assert order.status == DocumentStatus.DRAFT.value
    assert order.number
    assert stock.on_hand(product, warehouse) == Decimal("0")


def test_purchase_order_requires_at_least_one_line(supplier):
    with pytest.raises(ValidationError) as error:
        purchasing.create_order(supplier=supplier, lines=[])
    assert error.value.message_key == "purchase.no_lines"


def test_purchase_order_confirm_keeps_it_receivable(supplier, product):
    order = purchasing.create_order(supplier=supplier, lines=_lines(product, "100", "900"))
    purchasing.confirm_order(order)
    assert order.status == DocumentStatus.CONFIRMED.value


# ---------------------------------------------------------------------------
# Receipt and valuation
# ---------------------------------------------------------------------------

def test_receipt_from_an_order_receives_everything_outstanding(supplier, product, warehouse):
    order = purchasing.create_order(supplier=supplier, lines=_lines(product, "100", "900"), confirm=True)

    receipt = purchasing.create_receipt(order=order)

    assert isinstance(receipt, GoodsReceipt)
    assert receipt.items[0].quantity == Decimal("100")
    assert receipt.status == DocumentStatus.DRAFT.value
    assert stock.on_hand(product, warehouse) == Decimal("0")     # not posted yet


def test_posting_a_receipt_moves_stock_and_sets_the_cost(supplier, product, warehouse):
    order = purchasing.create_order(supplier=supplier, lines=_lines(product, "100", "900"), confirm=True)
    receipt = purchasing.create_receipt(order=order)

    purchasing.post_receipt(receipt)

    assert receipt.status == DocumentStatus.POSTED.value
    assert stock.on_hand(product, warehouse) == Decimal("100")
    level = stock.get_level(product, warehouse)
    assert level.avg_cost == Decimal("900.00")
    # Receiving is recorded on the append-only ledger, linked to the receipt.
    from app.models import MovementReason, StockMovement

    movement = db.session.query(StockMovement).filter_by(source_type="goods_receipt").one()
    assert movement.reason == MovementReason.PURCHASE.value
    assert movement.quantity == Decimal("100")


def test_a_receipt_cannot_be_posted_twice(supplier, product, warehouse):
    receipt = purchasing.create_receipt(
        supplier=supplier, lines=_lines(product, "10", "900")
    )
    purchasing.post_receipt(receipt)

    with pytest.raises(InvalidTransition):
        purchasing.post_receipt(receipt)

    assert stock.on_hand(product, warehouse) == Decimal("10")


def test_weighted_average_cost_across_two_receipts(supplier, product, warehouse):
    first = purchasing.create_receipt(supplier=supplier, lines=_lines(product, "100", "800"))
    purchasing.post_receipt(first)
    second = purchasing.create_receipt(supplier=supplier, lines=_lines(product, "100", "1000"))
    purchasing.post_receipt(second)

    level = stock.get_level(product, warehouse)
    assert level.quantity == Decimal("200")
    # (100 × 800 + 100 × 1000) / 200 = 900  — exact Decimal arithmetic
    assert level.avg_cost == Decimal("900.00")


def test_landed_costs_are_allocated_and_raise_the_unit_cost(supplier, product, warehouse):
    receipt = purchasing.create_receipt(
        supplier=supplier,
        lines=_lines(product, "100", "800"),
        transport_cost=Decimal("15000"),
        handling_cost=Decimal("5000"),
        other_cost=Decimal("0"),
    )

    allocated = purchasing.allocate_landed_costs(receipt)
    purchasing.post_receipt(receipt)

    assert allocated == Decimal("20000.00")
    assert receipt.landed_cost_allocated == Decimal("20000.00")
    level = stock.get_level(product, warehouse)
    # 20 000 DA of freight over 100 m³ = 200 DA/m³ on top of 800 DA/m³
    assert level.avg_cost == Decimal("1000.00")


def test_landed_costs_are_spread_by_line_value(supplier, product, cement, warehouse):
    receipt = purchasing.create_receipt(
        supplier=supplier,
        lines=[
            PurchaseLineInput(product_id=product.id, quantity=Decimal("10"), unit_price=Decimal("1000")),
            PurchaseLineInput(product_id=cement.id, quantity=Decimal("10"), unit_price=Decimal("1000")),
        ],
        transport_cost=Decimal("1000"),
    )

    purchasing.allocate_landed_costs(receipt)

    # 1 000 DA of freight split by line value (10 000 vs 10 000) = 500 each,
    # i.e. 50 DA per stock unit on top of the 1 000 DA purchase price.
    assert receipt.landed_cost_allocated == Decimal("1000.00")
    assert [item.landed_unit_cost for item in receipt.items] == [
        Decimal("1050.00"),
        Decimal("1050.00"),
    ]


# ---------------------------------------------------------------------------
# Supplier invoice
# ---------------------------------------------------------------------------

def test_supplier_invoice_from_a_receipt_is_posted_and_matches_its_base(supplier, product, warehouse):
    receipt = purchasing.create_receipt(supplier=supplier, lines=_lines(product, "100", "900"))
    purchasing.post_receipt(receipt)

    invoice = purchasing.create_invoice_from_receipt(receipt)

    assert isinstance(invoice, SupplierInvoice)
    assert invoice.status == DocumentStatus.POSTED.value
    assert invoice.subtotal == Decimal("90000.00")
    assert invoice.tax_amount == Decimal("17100.00")        # 19 %
    assert invoice.total_amount == Decimal("107100.00")


def test_supplier_invoice_can_be_paid_and_clears_its_balance(supplier, product, warehouse):
    receipt = purchasing.create_receipt(supplier=supplier, lines=_lines(product, "100", "900"))
    purchasing.post_receipt(receipt)
    invoice = purchasing.create_invoice_from_receipt(receipt)

    payment = payments.record_payment(
        direction=PaymentDirection.OUTBOUND.value,
        amount=invoice.total_amount,
        supplier=supplier,
        allocations=[{
            "document_type": "supplier_invoice",
            "document_id": invoice.id,
            "amount": invoice.total_amount,
        }],
    )

    assert payment.amount == Decimal("107100.00")
    assert invoice.balance_due == Decimal("0.00")
    assert payments.supplier_balance(supplier.id) == Decimal("0.00")


def test_a_paid_supplier_invoice_cannot_be_cancelled(supplier, product, warehouse):
    receipt = purchasing.create_receipt(supplier=supplier, lines=_lines(product, "10", "900"))
    purchasing.post_receipt(receipt)
    invoice = purchasing.create_invoice_from_receipt(receipt)
    payments.record_payment(
        direction=PaymentDirection.OUTBOUND.value,
        amount=invoice.total_amount,
        supplier=supplier,
        allocations=[{"document_type": "supplier_invoice", "document_id": invoice.id, "amount": invoice.total_amount}],
    )

    with pytest.raises(InvalidTransition) as error:
        purchasing.cancel_invoice(invoice, "erreur")
    assert error.value.message_key == "purchase.invoice_paid"


def test_a_customer_receipt_cannot_pay_a_supplier_invoice(customer, supplier, product, warehouse):
    receipt = purchasing.create_receipt(supplier=supplier, lines=_lines(product, "10", "900"))
    purchasing.post_receipt(receipt)
    invoice = purchasing.create_invoice_from_receipt(receipt)

    with pytest.raises(ValidationError):
        payments.record_payment(
            direction=PaymentDirection.INBOUND.value,
            amount=Decimal("1000"),
            customer=customer,
            allocations=[{"document_type": "supplier_invoice", "document_id": invoice.id, "amount": Decimal("1000")}],
        )


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------

def test_purchase_return_removes_stock_and_requires_a_reason(supplier, product, warehouse):
    receipt = purchasing.create_receipt(supplier=supplier, lines=_lines(product, "10", "900"))
    purchasing.post_receipt(receipt)

    with pytest.raises(ValidationError):
        purchasing.create_return(supplier=supplier, lines=_lines(product, "2", "900"), reason="")

    returned = purchasing.create_return(
        supplier=supplier, lines=_lines(product, "2", "900"), reason="produit non conforme"
    )

    assert isinstance(returned, PurchaseReturn)
    assert stock.on_hand(product, warehouse) == Decimal("8")
    assert returned.total_amount == Decimal("2142.00")     # 2 × 900 × 1.19


def test_return_above_stock_is_refused(supplier, product, warehouse):
    receipt = purchasing.create_receipt(supplier=supplier, lines=_lines(product, "2", "900"))
    purchasing.post_receipt(receipt)

    with pytest.raises(InsufficientStock):
        purchasing.create_return(supplier=supplier, lines=_lines(product, "5", "900"), reason="test")

    assert stock.on_hand(product, warehouse) == Decimal("2")

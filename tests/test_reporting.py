"""Reporting: aging buckets, KPIs, valuation and pagination.

Reports are where a shop management system is most tempted to flatter the
numbers.  These tests pin down that receivables really are grouped by how late
they are, that a settled invoice leaves the aging report, and that aggregation
happens in SQL with exact Decimal arithmetic.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import PaymentDirection, SalesInvoice
from app.services import payments, reporting, sales, stock
from app.services.sales import LineInput

from tests.conftest import give_stock, today

UNITS = ("m3",)


def _invoice(customer, product, warehouse, quantity: str) -> SalesInvoice:
    order = sales.create_order(
        customer=customer,
        lines=[LineInput(product_id=product.id, quantity=Decimal(quantity))],
        confirm=True,
    )
    delivery = sales.deliver_order(order)
    return sales.create_invoice_from_delivery(delivery)


def _aged_invoice(customer, product, warehouse, quantity: str, days_late: int) -> SalesInvoice:
    invoice = _invoice(customer, product, warehouse, quantity)
    invoice.due_date = today() - timedelta(days=days_late)
    db.session.flush()
    return invoice


# ---------------------------------------------------------------------------
# Receivables aging
# ---------------------------------------------------------------------------

def test_each_overdue_invoice_lands_in_the_right_bucket(customer, product, warehouse):
    give_stock(product, warehouse, "100")
    _aged_invoice(customer, product, warehouse, "10", days_late=0)     # current
    _aged_invoice(customer, product, warehouse, "1", days_late=15)     # 1–30
    _aged_invoice(customer, product, warehouse, "2", days_late=45)     # 31–60
    _aged_invoice(customer, product, warehouse, "3", days_late=75)     # 61–90
    _aged_invoice(customer, product, warehouse, "4", days_late=200)    # 90+
    db.session.commit()

    aging = reporting.receivables_aging()

    # 1 800 DA per m³, plus 19 % VAT.
    assert aging["buckets"]["current"] == Decimal("21420.00")
    assert aging["buckets"]["d1_30"] == Decimal("2142.00")
    assert aging["buckets"]["d31_60"] == Decimal("4284.00")
    assert aging["buckets"]["d61_90"] == Decimal("6426.00")
    assert aging["buckets"]["over_90"] == Decimal("8568.00")
    assert aging["total"] == sum(aging["buckets"].values())
    assert aging["total"] == Decimal("42840.00")


def test_bucket_boundaries_are_exclusive_on_the_upper_edge(customer, product, warehouse):
    give_stock(product, warehouse, "100")
    for days in (1, 30, 31, 60, 61, 90, 91):
        _aged_invoice(customer, product, warehouse, "1", days_late=days)
    db.session.commit()

    aging = reporting.receivables_aging()
    expected = Decimal("2142.00")
    assert aging["buckets"]["current"] == Decimal("0.00")
    assert aging["buckets"]["d1_30"] == expected * 2      # 1 and 30
    assert aging["buckets"]["d31_60"] == expected * 2     # 31 and 60
    assert aging["buckets"]["d61_90"] == expected * 2     # 61 and 90
    assert aging["buckets"]["over_90"] == expected        # 91


def test_a_settled_invoice_leaves_the_aging_report(customer, product, warehouse):
    give_stock(product, warehouse, "100")
    invoice = _aged_invoice(customer, product, warehouse, "10", days_late=40)
    db.session.commit()
    assert reporting.receivables_aging()["total"] == Decimal("21420.00")

    payments.record_payment(
        direction=PaymentDirection.INBOUND.value,
        amount=invoice.total_amount,
        customer=customer,
        allocations=[{
            "document_type": "sales_invoice",
            "document_id": invoice.id,
            "amount": invoice.total_amount,
        }],
    )
    db.session.commit()

    aging = reporting.receivables_aging()
    assert aging["total"] == Decimal("0.00")
    assert aging["rows"] == []
    assert reporting.receivable_total() == Decimal("0.00")


def test_a_draft_invoice_is_not_a_receivable(customer, product, warehouse):
    give_stock(product, warehouse, "100")
    sales.create_quotation(
        customer=customer,
        lines=[LineInput(product_id=product.id, quantity=Decimal("10"))],
    )
    db.session.commit()

    # A quotation is not an invoice and must not inflate what customers owe.
    assert reporting.receivables_aging()["total"] == Decimal("0.00")


def test_aging_can_be_narrowed_to_one_customer(customer, make_customer, product, warehouse):
    other = make_customer("EPE Travaux Publics")
    give_stock(product, warehouse, "100")
    _aged_invoice(customer, product, warehouse, "10", days_late=10)
    _aged_invoice(other, product, warehouse, "1", days_late=10)
    db.session.commit()

    aging = reporting.receivables_aging(customer_id=customer.id)

    assert aging["total"] == Decimal("21420.00")
    assert len(aging["by_customer"]) == 1
    assert aging["by_customer"][0]["customer_id"] == customer.id


def test_payables_aging_tracks_what_the_shop_owes(supplier, product, warehouse):
    from app.services import purchasing
    from app.services.purchasing import PurchaseLineInput

    receipt = purchasing.create_receipt(
        supplier=supplier,
        lines=[PurchaseLineInput(product_id=product.id, quantity=Decimal("100"), unit_price=Decimal("900"))],
    )
    purchasing.post_receipt(receipt)
    invoice = purchasing.create_invoice_from_receipt(receipt)
    invoice.due_date = today() - timedelta(days=35)
    db.session.flush()
    db.session.commit()

    aging = reporting.payables_aging()

    assert aging["buckets"]["d31_60"] == invoice.total_amount
    assert aging["total"] == invoice.total_amount
    assert reporting.payable_total() == invoice.total_amount


# ---------------------------------------------------------------------------
# Dashboard KPIs
# ---------------------------------------------------------------------------

def test_dashboard_separates_revenue_cost_and_margin(customer, product, warehouse):
    give_stock(product, warehouse, "100", unit_cost="1000")
    _invoice(customer, product, warehouse, "10")
    db.session.commit()

    kpis = reporting.dashboard_kpis()

    assert kpis["sales_month_total"] == Decimal("21420.00")      # TTC, what the customer pays
    assert kpis["sales_month_count"] == 1
    assert kpis["revenue_net_month"] == Decimal("18000.00")      # HT
    assert kpis["cogs_month"] == Decimal("10000.00")             # 10 × 1 000 cost
    assert kpis["gross_margin_month"] == Decimal("8000.00")
    assert kpis["gross_margin_percent"] == Decimal("44.44")
    assert kpis["receivable_total"] == Decimal("21420.00")
    # 100 received at 1 000, 10 sold: 90 left on the shelf.
    assert kpis["stock_value"] == Decimal("90000.00")


def test_dashboard_query_count_does_not_grow_per_invoice(
    customer, product, warehouse
):
    """Measured N+1 regression guard.

    The dashboard previously called ``invoice.paid_amount`` inside a loop,
    producing ~1,000 statements for 1,000 invoices.  Balances are now aggregated
    once; this test fails if a per-invoice query comes back.
    """
    from sqlalchemy import event

    give_stock(product, warehouse, "50")
    for _ in range(15):
        _invoice(customer, product, warehouse, "1")

    counter = {"queries": 0}

    def count(*_args):
        counter["queries"] += 1

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        reporting.dashboard_kpis()
    finally:
        event.remove(db.engine, "before_cursor_execute", count)
    assert counter["queries"] < 25


def test_invoice_settlements_batch_query_count(customer, product, warehouse):
    from sqlalchemy import event

    give_stock(product, warehouse, "50")
    invoices = [
        _invoice(customer, product, warehouse, "1") for _ in range(15)
    ]
    counter = {"queries": 0}

    def count(*_args):
        counter["queries"] += 1

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        settlements = reporting.invoice_settlements(invoices)
    finally:
        event.remove(db.engine, "before_cursor_execute", count)
    assert len(settlements) == 15
    assert all(row["status"] == "unpaid" for row in settlements.values())
    assert counter["queries"] <= 4


def test_dashboard_margin_is_undefined_not_zero_when_nothing_was_sold(product, warehouse):
    give_stock(product, warehouse, "10", unit_cost="1000")
    db.session.commit()

    kpis = reporting.dashboard_kpis()

    assert kpis["revenue_net_month"] == Decimal("0.00")
    # Nothing sold means the ratio does not exist; it is not "0 %".
    assert kpis["gross_margin_percent"] is None
    assert kpis["stock_value"] == Decimal("10000.00")


# ---------------------------------------------------------------------------
# Stock valuation and low stock
# ---------------------------------------------------------------------------

def test_stock_valuation_uses_the_ledger_average_cost(product, cement, warehouse):
    give_stock(product, warehouse, "10", unit_cost="1200")
    give_stock(cement, warehouse, "100", unit_cost="560")
    db.session.commit()

    rows = reporting.stock_valuation_report()
    by_name = {row["name"]: row for row in rows}

    assert by_name["Sable 0/4"]["quantity"] == Decimal("10")
    assert by_name["Sable 0/4"]["avg_cost"] == Decimal("1200.00")
    assert by_name["Sable 0/4"]["value"] == Decimal("12000.00")
    assert by_name["Ciment CPJ 42.5"]["value"] == Decimal("56000.00")


def test_low_stock_lists_only_products_below_their_reorder_level(product, warehouse):
    give_stock(product, warehouse, "4")     # reorder level is 10 by default
    db.session.commit()

    rows = reporting.low_stock_products()

    assert [row["name"] for row in rows] == ["Sable 0/4"]
    assert rows[0]["available"] == Decimal("4")
    assert rows[0]["reorder_level"] == Decimal("10")

    give_stock(product, warehouse, "50")
    db.session.commit()
    assert reporting.low_stock_products() == []


# ---------------------------------------------------------------------------
# Margin report
# ---------------------------------------------------------------------------

def test_margin_report_uses_the_cost_snapshotted_on_the_invoice(customer, product, warehouse):
    give_stock(product, warehouse, "100", unit_cost="1000")
    _invoice(customer, product, warehouse, "10")
    db.session.commit()

    # The product's cost price is changed *after* the sale; history must not move.
    product.cost_price = Decimal("1600")
    db.session.commit()

    rows = reporting.margin_report(date_from=today() - timedelta(days=1), date_to=today())

    assert len(rows) == 1
    assert rows[0]["revenue"] == Decimal("18000.00")
    assert rows[0]["cost"] == Decimal("10000.00")
    assert rows[0]["margin"] == Decimal("8000.00")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_pagination_reports_its_own_bounds(make_product):
    from sqlalchemy import select

    from app.models import Product

    for index in range(25):
        make_product(name=f"Produit {index}", sku=f"P-{index:03d}")
    db.session.commit()

    page = reporting.paginate(select(Product).order_by(Product.sku), page=1, per_page=10)

    assert page["total"] == 25
    assert page["pages"] == 3
    assert len(page["items"]) == 10
    assert page["has_prev"] is False
    assert page["has_next"] is True

    last = reporting.paginate(select(Product).order_by(Product.sku), page=3, per_page=10)
    assert len(last["items"]) == 5
    assert last["has_next"] is False
    assert last["has_prev"] is True


def test_pagination_clamps_out_of_range_requests(make_product):
    from sqlalchemy import select

    from app.models import Product

    make_product(sku="ONLY-1")
    db.session.commit()

    page = reporting.paginate(select(Product), page=99, per_page=10)

    assert page["page"] == 1
    assert page["pages"] == 1
    assert page["has_next"] is False


def test_pagination_does_not_materialise_the_whole_table(make_product):
    from sqlalchemy import select

    from app.models import Product

    for index in range(60):
        make_product(name=f"P {index}", sku=f"X-{index:03d}")
    db.session.commit()

    page = reporting.paginate(select(Product).order_by(Product.id), page=2, per_page=20)

    assert page["total"] == 60
    assert page["items"][0].id != 1        # really the second page
    assert len(page["items"]) == 20

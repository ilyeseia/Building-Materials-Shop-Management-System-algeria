"""Reporting: SQL-aggregated KPIs, aging, valuation and margin.

Principles
----------
* Aggregation happens in the database (``SUM``/``COUNT``/``GROUP BY``), not by
  loading whole tables into Python; the results are paginated.
* A cancelled document never contributes to a total, and a reversed payment never
  counts as money received.
* Stock valuation uses the recorded weighted-average cost of each
  (product, warehouse) level, which is what the ledger actually posted — the same
  figure as :func:`app.services.stock.stock_valuation`.
* Every function returns plain ``dict``/number data, so a template, an API
  response or a test can consume it without a second query layer.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    CreditNote,
    Customer,
    DeliveryNote,
    DocumentStatus,
    Payment,
    PaymentAllocation,
    PaymentDirection,
    Product,
    SalesInvoice,
    SalesInvoiceItem,
    StockLevel,
    StockMovement,
    Supplier,
    SupplierInvoice,
    Trip,
    TripExpense,
    WeighingTicket,
    WeighingDirection,
)
from app.services.timeutil import add_days, month_start, today_local

MONEY_PLACES = Decimal("0.01")
ZERO = Decimal("0")

#: Aging buckets: (key, label key, lower bound in days, upper bound exclusive).
AGING_BUCKETS = (
    ("current", "aging.current", None, 1),
    ("d1_30", "aging.d1_30", 1, 31),
    ("d31_60", "aging.d31_60", 31, 61),
    ("d61_90", "aging.d61_90", 61, 91),
    ("over_90", "aging.over_90", 91, None),
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY_PLACES)


def _scalar(statement) -> Decimal:
    value = db.session.execute(statement).scalar()
    return Decimal(str(value or 0))


def paginate(query, page: int = 1, per_page: int = 20) -> dict:
    """Run ``query`` for one page and return items plus pagination metadata."""
    page = max(int(page or 1), 1)
    per_page = max(int(per_page or 20), 1)
    total = db.session.execute(
        select(func.count()).select_from(query.order_by(None).subquery())
    ).scalar() or 0
    pages = max(math.ceil(total / per_page), 1)
    page = min(page, pages)
    items = db.session.execute(
        query.limit(per_page).offset((page - 1) * per_page)
    ).scalars().all()
    return {
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": int(total),
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
    }


def _active_invoice_filter(model, date_field, date_from, date_to):
    conditions = [model.status.notin_([DocumentStatus.DRAFT.value, DocumentStatus.CANCELLED.value])]
    if date_from is not None:
        conditions.append(date_field >= date_from)
    if date_to is not None:
        conditions.append(date_field <= date_to)
    return conditions


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def dashboard_kpis(*, on_date: date | None = None, warehouse_id: int | None = None) -> dict:
    today = on_date or today_local()
    first_of_month = month_start(today)
    first_of_year = today.replace(month=1, day=1)

    def _sales_total(date_from, date_to) -> tuple[Decimal, int]:
        conditions = _active_invoice_filter(
            SalesInvoice, SalesInvoice.invoice_date, date_from, date_to
        )
        row = db.session.execute(
            select(
                func.coalesce(func.sum(SalesInvoice.total_amount), 0),
                func.count(SalesInvoice.id),
            ).where(*conditions)
        ).one()
        return _money(row[0]), int(row[1])

    month_total, month_count = _sales_total(first_of_month, today)
    year_total, year_count = _sales_total(first_of_year, today)

    cogs_month = _scalar(
        select(func.coalesce(func.sum(SalesInvoiceItem.stock_quantity * SalesInvoiceItem.unit_cost), 0))
        .join(SalesInvoice, SalesInvoice.id == SalesInvoiceItem.invoice_id)
        .where(
            *_active_invoice_filter(SalesInvoice, SalesInvoice.invoice_date, first_of_month, today)
        )
    )
    revenue_net_month = _scalar(
        select(func.coalesce(func.sum(SalesInvoiceItem.line_total), 0))
        .join(SalesInvoice, SalesInvoice.id == SalesInvoiceItem.invoice_id)
        .where(
            *_active_invoice_filter(SalesInvoice, SalesInvoice.invoice_date, first_of_month, today)
        )
    )

    receivable = receivable_total(as_of=today)
    payable = payable_total(as_of=today)

    stock_levels = select(func.count(StockLevel.id))
    low_stock = low_stock_products(warehouse_id=warehouse_id, limit=5)
    valuation = _scalar(
        select(func.coalesce(func.sum(StockLevel.quantity * StockLevel.avg_cost), 0))
    )

    return {
        "today": today,
        "month_start": first_of_month,
        "sales_month_total": month_total,
        "sales_month_count": month_count,
        "sales_year_total": year_total,
        "sales_year_count": year_count,
        "revenue_net_month": _money(revenue_net_month),
        "cogs_month": _money(cogs_month),
        "gross_margin_month": _money(revenue_net_month - cogs_month),
        "gross_margin_percent": (
            ((revenue_net_month - cogs_month) / revenue_net_month * Decimal("100")).quantize(
                Decimal("0.01")
            )
            if revenue_net_month > 0
            else None
        ),
        "receivable_total": receivable,
        "payable_total": payable,
        "stock_value": _money(valuation),
        "stock_level_count": int(db.session.execute(stock_levels).scalar() or 0),
        "low_stock": low_stock,
        "weighing_today": int(
            db.session.execute(
                select(func.count(WeighingTicket.id)).where(WeighingTicket.ticket_date == today)
            ).scalar()
            or 0
        ),
        "trips_open": int(
            db.session.execute(
                select(func.count(Trip.id)).where(
                    Trip.status.in_(["open", "in_transit", "draft"])
                )
            ).scalar()
            or 0
        ),
    }


# ---------------------------------------------------------------------------
# Receivables / payables
# ---------------------------------------------------------------------------

def _paid_by_document(document_type: str) -> dict[int, Decimal]:
    rows = db.session.execute(
        select(
            PaymentAllocation.document_id,
            func.coalesce(func.sum(PaymentAllocation.amount), 0),
        )
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(
            PaymentAllocation.document_type == document_type,
            Payment.status == DocumentStatus.POSTED.value,
        )
        .group_by(PaymentAllocation.document_id)
    ).all()
    return {int(document_id): Decimal(str(total or 0)) for document_id, total in rows}


def receivable_total(*, as_of: date | None = None) -> Decimal:
    as_of = as_of or today_local()
    invoices = db.session.execute(
        select(SalesInvoice).where(
            *_active_invoice_filter(SalesInvoice, SalesInvoice.invoice_date, None, as_of)
        )
    ).scalars().all()
    credited = _credited_by_invoice()
    return sum(
        (_invoice_balance(inv, credited.get(inv.id, ZERO)) for inv in invoices), ZERO
    ).quantize(MONEY_PLACES)


def payable_total(*, as_of: date | None = None) -> Decimal:
    as_of = as_of or today_local()
    invoices = db.session.execute(
        select(SupplierInvoice).where(
            *_active_invoice_filter(SupplierInvoice, SupplierInvoice.invoice_date, None, as_of)
        )
    ).scalars().all()
    return sum((Decimal(inv.balance_due or 0) for inv in invoices), ZERO).quantize(MONEY_PLACES)


def _credited_by_invoice() -> dict[int, Decimal]:
    rows = db.session.execute(
        select(CreditNote.invoice_id, func.coalesce(func.sum(CreditNote.total_amount), 0))
        .where(
            CreditNote.invoice_id.is_not(None),
            CreditNote.status != DocumentStatus.CANCELLED.value,
        )
        .group_by(CreditNote.invoice_id)
    ).all()
    return {int(invoice_id): Decimal(str(total or 0)) for invoice_id, total in rows}


def _invoice_balance(invoice: SalesInvoice, credited: Decimal) -> Decimal:
    return _money(Decimal(invoice.total_amount or 0) - invoice.paid_amount - credited)


def _bucket_key(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "d1_30"
    if days_overdue <= 60:
        return "d31_60"
    if days_overdue <= 90:
        return "d61_90"
    return "over_90"


def receivables_aging(*, as_of: date | None = None, customer_id: int | None = None) -> dict:
    """Outstanding sales invoices grouped into aging buckets, plus per-customer rows."""
    as_of = as_of or today_local()
    query = select(SalesInvoice).where(
        *_active_invoice_filter(SalesInvoice, SalesInvoice.invoice_date, None, as_of)
    )
    if customer_id:
        query = query.where(SalesInvoice.customer_id == customer_id)
    invoices = db.session.execute(query.order_by(SalesInvoice.invoice_date)).scalars().all()
    credited = _credited_by_invoice()

    buckets = {key: ZERO for key, _label, _lo, _hi in AGING_BUCKETS}
    rows: list[dict] = []
    by_customer: dict[int, Decimal] = {}
    for invoice in invoices:
        balance = _invoice_balance(invoice, credited.get(invoice.id, ZERO))
        if balance <= 0:
            continue
        due = invoice.due_date or invoice.invoice_date
        days_overdue = (as_of - due).days
        key = _bucket_key(days_overdue)
        buckets[key] += balance
        by_customer[invoice.customer_id] = by_customer.get(invoice.customer_id, ZERO) + balance
        rows.append(
            {
                "invoice": invoice,
                "customer": invoice.customer,
                "due_date": due,
                "days_overdue": days_overdue,
                "bucket": key,
                "balance": balance,
            }
        )

    customer_names = {
        row.id: row.name
        for row in db.session.execute(
            select(Customer).where(Customer.id.in_(list(by_customer) or [0]))
        ).scalars().all()
    }
    return {
        "as_of": as_of,
        "buckets": {key: value.quantize(MONEY_PLACES) for key, value in buckets.items()},
        "total": sum(buckets.values(), ZERO).quantize(MONEY_PLACES),
        "rows": rows,
        "by_customer": [
            {"customer_id": cid, "name": customer_names.get(cid, ""), "balance": value.quantize(MONEY_PLACES)}
            for cid, value in sorted(by_customer.items(), key=lambda item: item[1], reverse=True)
        ],
    }


def payables_aging(*, as_of: date | None = None, supplier_id: int | None = None) -> dict:
    as_of = as_of or today_local()
    query = select(SupplierInvoice).where(
        *_active_invoice_filter(SupplierInvoice, SupplierInvoice.invoice_date, None, as_of)
    )
    if supplier_id:
        query = query.where(SupplierInvoice.supplier_id == supplier_id)
    invoices = db.session.execute(query.order_by(SupplierInvoice.invoice_date)).scalars().all()

    buckets = {key: ZERO for key, _label, _lo, _hi in AGING_BUCKETS}
    rows: list[dict] = []
    by_supplier: dict[int, Decimal] = {}
    for invoice in invoices:
        balance = _money(invoice.balance_due)
        if balance <= 0:
            continue
        due = invoice.due_date or invoice.invoice_date
        days_overdue = (as_of - due).days
        key = _bucket_key(days_overdue)
        buckets[key] += balance
        by_supplier[invoice.supplier_id] = by_supplier.get(invoice.supplier_id, ZERO) + balance
        rows.append(
            {
                "invoice": invoice,
                "supplier": invoice.supplier,
                "due_date": due,
                "days_overdue": days_overdue,
                "bucket": key,
                "balance": balance,
            }
        )
    supplier_names = {
        row.id: row.name
        for row in db.session.execute(
            select(Supplier).where(Supplier.id.in_(list(by_supplier) or [0]))
        ).scalars().all()
    }
    return {
        "as_of": as_of,
        "buckets": {key: value.quantize(MONEY_PLACES) for key, value in buckets.items()},
        "total": sum(buckets.values(), ZERO).quantize(MONEY_PLACES),
        "rows": rows,
        "by_supplier": [
            {"supplier_id": sid, "name": supplier_names.get(sid, ""), "balance": value.quantize(MONEY_PLACES)}
            for sid, value in sorted(by_supplier.items(), key=lambda item: item[1], reverse=True)
        ],
    }


# ---------------------------------------------------------------------------
# Sales / margin
# ---------------------------------------------------------------------------

def sales_by_day(*, date_from: date, date_to: date, warehouse_id: int | None = None) -> list[dict]:
    conditions = _active_invoice_filter(SalesInvoice, SalesInvoice.invoice_date, date_from, date_to)
    if warehouse_id:
        conditions.append(SalesInvoice.warehouse_id == warehouse_id)
    rows = db.session.execute(
        select(
            SalesInvoice.invoice_date,
            func.coalesce(func.sum(SalesInvoice.total_amount), 0),
            func.count(SalesInvoice.id),
        )
        .where(*conditions)
        .group_by(SalesInvoice.invoice_date)
        .order_by(SalesInvoice.invoice_date)
    ).all()
    return [
        {"day": day, "total": _money(total), "count": int(count)}
        for day, total, count in rows
    ]


def top_products(*, date_from: date, date_to: date, limit: int = 10) -> list[dict]:
    rows = db.session.execute(
        select(
            Product.id,
            Product.name,
            func.coalesce(func.sum(SalesInvoiceItem.stock_quantity), 0),
            func.coalesce(func.sum(SalesInvoiceItem.line_total), 0),
            func.coalesce(func.sum(SalesInvoiceItem.stock_quantity * SalesInvoiceItem.unit_cost), 0),
        )
        .join(SalesInvoiceItem, SalesInvoiceItem.product_id == Product.id)
        .join(SalesInvoice, SalesInvoice.id == SalesInvoiceItem.invoice_id)
        .where(*_active_invoice_filter(SalesInvoice, SalesInvoice.invoice_date, date_from, date_to))
        .group_by(Product.id, Product.name)
        .order_by(func.sum(SalesInvoiceItem.line_total).desc())
        .limit(limit)
    ).all()
    return [
        {
            "product_id": pid,
            "name": name,
            "quantity": Decimal(str(quantity or 0)),
            "revenue": _money(revenue),
            "cost": _money(cost),
            "margin": _money(Decimal(str(revenue or 0)) - Decimal(str(cost or 0))),
        }
        for pid, name, quantity, revenue, cost in rows
    ]


def sales_by_customer(*, date_from: date, date_to: date, limit: int = 20) -> list[dict]:
    rows = db.session.execute(
        select(
            Customer.id,
            Customer.name,
            func.coalesce(func.sum(SalesInvoice.total_amount), 0),
            func.count(SalesInvoice.id),
        )
        .join(SalesInvoice, SalesInvoice.customer_id == Customer.id)
        .where(*_active_invoice_filter(SalesInvoice, SalesInvoice.invoice_date, date_from, date_to))
        .group_by(Customer.id, Customer.name)
        .order_by(func.sum(SalesInvoice.total_amount).desc())
        .limit(limit)
    ).all()
    return [{"customer_id": cid, "name": name, "total": _money(total), "count": int(count)}
            for cid, name, total, count in rows]


def margin_report(*, date_from: date, date_to: date, group_by: str = "product") -> list[dict]:
    """Gross margin per product or per invoice, from the snapshotted unit cost."""
    conditions = _active_invoice_filter(SalesInvoice, SalesInvoice.invoice_date, date_from, date_to)
    base = (
        select(
            func.coalesce(func.sum(SalesInvoiceItem.line_total), 0),
            func.coalesce(func.sum(SalesInvoiceItem.stock_quantity * SalesInvoiceItem.unit_cost), 0),
        )
        .join(SalesInvoice, SalesInvoice.id == SalesInvoiceItem.invoice_id)
        .where(*conditions)
    )
    if group_by == "invoice":
        rows = db.session.execute(
            base.add_columns(SalesInvoice.id, SalesInvoice.number).group_by(
                SalesInvoice.id, SalesInvoice.number
            )
        ).all()
        return [
            {
                "key": number or str(invoice_id),
                "revenue": _money(revenue),
                "cost": _money(cost),
                "margin": _money(Decimal(str(revenue or 0)) - Decimal(str(cost or 0))),
            }
            for revenue, cost, invoice_id, number in rows
        ]

    rows = db.session.execute(
        base.add_columns(Product.id, Product.name)
        .join(Product, Product.id == SalesInvoiceItem.product_id)
        .group_by(Product.id, Product.name)
    ).all()
    return [
        {
            "key": name,
            "product_id": pid,
            "revenue": _money(revenue),
            "cost": _money(cost),
            "margin": _money(Decimal(str(revenue or 0)) - Decimal(str(cost or 0))),
        }
        for revenue, cost, pid, name in rows
    ]


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------

def stock_valuation_report(*, warehouse_id: int | None = None, limit: int = 200) -> list[dict]:
    query = (
        select(
            StockLevel.product_id,
            Product.name,
            StockLevel.warehouse_id,
            StockLevel.quantity,
            StockLevel.reserved,
            StockLevel.avg_cost,
        )
        .join(Product, Product.id == StockLevel.product_id)
        .order_by(Product.name)
    )
    if warehouse_id:
        query = query.where(StockLevel.warehouse_id == warehouse_id)
    rows = db.session.execute(query.limit(limit)).all()
    result = []
    for product_id, name, warehouse, quantity, reserved, avg_cost in rows:
        value = _money(Decimal(str(quantity or 0)) * Decimal(str(avg_cost or 0)))
        result.append(
            {
                "product_id": product_id,
                "name": name,
                "warehouse_id": warehouse,
                "quantity": Decimal(str(quantity or 0)),
                "reserved": Decimal(str(reserved or 0)),
                "available": Decimal(str(quantity or 0)) - Decimal(str(reserved or 0)),
                "avg_cost": _money(avg_cost),
                "value": value,
            }
        )
    return result


def low_stock_products(*, warehouse_id: int | None = None, limit: int = 20) -> list[dict]:
    """Products whose available stock is at or below their reorder level."""
    query = (
        select(
            Product.id,
            Product.name,
            func.coalesce(func.sum(StockLevel.quantity), 0),
            func.coalesce(func.sum(StockLevel.reserved), 0),
            Product.reorder_level,
            Product.stock_unit_id,
        )
        .join(StockLevel, StockLevel.product_id == Product.id, isouter=True)
        .where(Product.is_active.is_(True), Product.reorder_level > 0)
    )
    if warehouse_id:
        query = query.where(StockLevel.warehouse_id == warehouse_id)
    query = query.group_by(
        Product.id, Product.name, Product.reorder_level, Product.stock_unit_id
    )
    rows = db.session.execute(query).all()
    alerts = []
    for product_id, name, quantity, reserved, reorder, _unit_id in rows:
        available = Decimal(str(quantity or 0)) - Decimal(str(reserved or 0))
        if available <= Decimal(str(reorder or 0)):
            alerts.append(
                {
                    "product_id": product_id,
                    "name": name,
                    "available": available,
                    "reorder_level": Decimal(str(reorder or 0)),
                }
            )
    alerts.sort(key=lambda row: row["available"])
    return alerts[:limit]


def stock_movement_summary(*, date_from: date, date_to: date, warehouse_id: int | None = None) -> list[dict]:
    lower = datetime.combine(date_from, time.min)
    upper = datetime.combine(date_to + timedelta(days=1), time.min)
    conditions = [
        StockMovement.created_at >= lower,
        StockMovement.created_at < upper,
    ]
    if warehouse_id:
        conditions.append(StockMovement.warehouse_id == warehouse_id)
    rows = db.session.execute(
        select(
            StockMovement.reason,
            StockMovement.direction,
            func.coalesce(func.sum(StockMovement.quantity), 0),
            func.count(StockMovement.id),
        )
        .where(*conditions)
        .group_by(StockMovement.reason, StockMovement.direction)
        .order_by(StockMovement.reason)
    ).all()
    return [
        {"reason": reason, "direction": direction, "quantity": Decimal(str(quantity or 0)), "count": int(count)}
        for reason, direction, quantity, count in rows
    ]


# ---------------------------------------------------------------------------
# Weighbridge / fleet
# ---------------------------------------------------------------------------

def weighing_summary(*, date_from: date, date_to: date) -> dict:
    rows = db.session.execute(
        select(
            WeighingTicket.direction,
            func.count(WeighingTicket.id),
            func.coalesce(func.sum(WeighingTicket.net_weight), 0),
        )
        .where(WeighingTicket.ticket_date >= date_from, WeighingTicket.ticket_date <= date_to)
        .group_by(WeighingTicket.direction)
    ).all()
    summary = {
        "inbound": {"count": 0, "net_kg": ZERO},
        "outbound": {"count": 0, "net_kg": ZERO},
        "total_count": 0,
        "total_net_kg": ZERO,
    }
    for direction, count, net in rows:
        key = direction if direction in summary else "outbound"
        summary[key] = {"count": int(count), "net_kg": Decimal(str(net or 0))}
    summary["total_count"] = summary["inbound"]["count"] + summary["outbound"]["count"]
    summary["total_net_kg"] = summary["inbound"]["net_kg"] + summary["outbound"]["net_kg"]
    summary["tolerance_exceeded"] = int(
        db.session.execute(
            select(func.count(WeighingTicket.id)).where(
                WeighingTicket.ticket_date >= date_from,
                WeighingTicket.ticket_date <= date_to,
                WeighingTicket.tolerance_exceeded.is_(True),
            )
        ).scalar()
        or 0
    )
    return summary


def fleet_summary(*, date_from: date, date_to: date) -> dict:
    revenue = _scalar(
        select(func.coalesce(func.sum(Trip.transport_fee), 0)).where(
            Trip.trip_date >= date_from, Trip.trip_date <= date_to
        )
    )
    carrier = _scalar(
        select(func.coalesce(func.sum(Trip.carrier_cost), 0)).where(
            Trip.trip_date >= date_from, Trip.trip_date <= date_to
        )
    )
    expenses = _scalar(
        select(func.coalesce(func.sum(TripExpense.amount), 0))
        .join(Trip, Trip.id == TripExpense.trip_id)
        .where(Trip.trip_date >= date_from, Trip.trip_date <= date_to)
    )
    trips = int(
        db.session.execute(
            select(func.count(Trip.id)).where(
                Trip.trip_date >= date_from, Trip.trip_date <= date_to
            )
        ).scalar()
        or 0
    )
    total_cost = _money(carrier + expenses)
    return {
        "trips": trips,
        "transport_revenue": _money(revenue),
        "carrier_cost": _money(carrier),
        "expenses": _money(expenses),
        "total_cost": total_cost,
        "margin": _money(revenue - total_cost),
    }


def delivery_summary(*, date_from: date, date_to: date) -> dict:
    row = db.session.execute(
        select(
            func.count(DeliveryNote.id),
            func.coalesce(func.sum(DeliveryNote.transport_fee), 0),
        ).where(
            DeliveryNote.delivery_date >= date_from,
            DeliveryNote.delivery_date <= date_to,
            DeliveryNote.status != DocumentStatus.CANCELLED.value,
        )
    ).one()
    return {"deliveries": int(row[0] or 0), "transport_fees": _money(row[1])}


def payment_summary(*, date_from: date, date_to: date) -> dict:
    rows = db.session.execute(
        select(
            Payment.direction,
            func.coalesce(func.sum(Payment.amount), 0),
            func.count(Payment.id),
        )
        .where(
            Payment.payment_date >= date_from,
            Payment.payment_date <= date_to,
            Payment.status == DocumentStatus.POSTED.value,
        )
        .group_by(Payment.direction)
    ).all()
    inbound = outbound = ZERO
    inbound_count = outbound_count = 0
    for direction, total, count in rows:
        if direction == PaymentDirection.INBOUND.value:
            inbound, inbound_count = _money(total), int(count)
        else:
            outbound, outbound_count = _money(total), int(count)
    return {
        "received": inbound,
        "received_count": inbound_count,
        "paid": outbound,
        "paid_count": outbound_count,
        "net": _money(inbound - outbound),
    }


def default_period(*, days: int = 30) -> tuple[date, date]:
    today = today_local()
    return add_days(today, -(days - 1)), today

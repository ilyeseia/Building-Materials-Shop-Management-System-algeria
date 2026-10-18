"""Reports: aging, valuation, margin, sales, weighing and transport."""
from __future__ import annotations

from flask import Blueprint, render_template, request
from sqlalchemy import select

from app.extensions import db
from app.models import Warehouse
from app.blueprints.helpers import parse_date, parse_int
from app.security import requires
from app.services import reporting
from app.services.settings import module_enabled
from app.services.timeutil import today_local

bp = Blueprint("reports", __name__)


def _period():
    default_from, default_to = reporting.default_period(days=30)
    date_from = parse_date(request.args.get("from"), default_from) or default_from
    date_to = parse_date(request.args.get("to"), default_to) or default_to
    return date_from, date_to


@bp.route("/reports")
@requires("reports.view")
def index():
    date_from, date_to = _period()
    return render_template(
        "reports/index.html",
        period=(date_from, date_to),
        receivable=reporting.receivable_total(),
        payable=reporting.payable_total(),
        valuation=reporting.stock_valuation_report(limit=200),
        low_stock=reporting.low_stock_products(limit=20),
        modules={"weighbridge": module_enabled("weighbridge"), "fleet": module_enabled("fleet")},
    )


@bp.route("/reports/receivables")
@requires("reports.view")
def receivables():
    aging = reporting.receivables_aging()
    return render_template("reports/aging.html", aging=aging, kind="receivable")


@bp.route("/reports/payables")
@requires("reports.view")
def payables():
    aging = reporting.payables_aging()
    return render_template("reports/aging.html", aging=aging, kind="payable")


@bp.route("/reports/stock")
@requires("reports.view")
def stock_report():
    warehouse_id = parse_int(request.args.get("warehouse"))
    rows = reporting.stock_valuation_report(warehouse_id=warehouse_id, limit=500)
    total = sum((row["value"] for row in rows), __import__("decimal").Decimal("0"))
    warehouses = db.session.execute(
        select(Warehouse).order_by(Warehouse.code)
    ).scalars().all()
    return render_template(
        "reports/stock.html",
        rows=rows,
        total=total,
        low_stock=reporting.low_stock_products(warehouse_id=warehouse_id, limit=50),
        warehouse_id=warehouse_id,
        warehouses=warehouses,
    )


@bp.route("/reports/sales")
@requires("reports.view")
def sales_report():
    date_from, date_to = _period()
    return render_template(
        "reports/sales.html",
        period=(date_from, date_to),
        series=reporting.sales_by_day(date_from=date_from, date_to=date_to),
        by_customer=reporting.sales_by_customer(date_from=date_from, date_to=date_to, limit=20),
        top_products=reporting.top_products(date_from=date_from, date_to=date_to, limit=20),
        payments=reporting.payment_summary(date_from=date_from, date_to=date_to),
    )


@bp.route("/reports/margin")
@requires("reports.view")
def margin_report():
    date_from, date_to = _period()
    return render_template(
        "reports/margin.html",
        period=(date_from, date_to),
        rows=reporting.margin_report(date_from=date_from, date_to=date_to),
    )


@bp.route("/reports/weighing")
@requires("reports.view")
def weighing_report():
    date_from, date_to = _period()
    return render_template(
        "reports/weighing.html",
        period=(date_from, date_to),
        summary=reporting.weighing_summary(date_from=date_from, date_to=date_to),
    )


@bp.route("/reports/fleet")
@requires("reports.view")
def fleet_report():
    date_from, date_to = _period()
    return render_template(
        "reports/fleet.html",
        period=(date_from, date_to),
        summary=reporting.fleet_summary(date_from=date_from, date_to=date_to),
        deliveries=reporting.delivery_summary(date_from=date_from, date_to=date_to),
    )

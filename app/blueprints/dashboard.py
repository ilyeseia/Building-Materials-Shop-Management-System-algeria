"""Dashboard: the day's numbers, low stock and quick actions."""
from __future__ import annotations

from flask import Blueprint, render_template

from app.security import requires
from app.services import reporting
from app.services.settings import module_enabled

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@requires("dashboard.view")
def index():
    kpis = reporting.dashboard_kpis()
    date_from, date_to = reporting.default_period(days=30)
    return render_template(
        "dashboard.html",
        kpis=kpis,
        sales_series=reporting.sales_by_day(date_from=date_from, date_to=date_to),
        period=(date_from, date_to),
        top_products=reporting.top_products(date_from=date_from, date_to=date_to, limit=5),
        modules={
            "weighbridge": module_enabled("weighbridge"),
            "fleet": module_enabled("fleet"),
            "accounting": module_enabled("accounting"),
        },
    )

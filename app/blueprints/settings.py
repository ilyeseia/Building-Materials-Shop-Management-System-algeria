"""Business settings."""
from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, url_for
from sqlalchemy import select

from app.extensions import db
from app.forms import SettingsForm
from app.i18n import translate
from app.models import Warehouse
from app.blueprints.helpers import choices_for, fill_choices, handle_errors
from app.security import requires
from app.services import audit
from app.services import settings as settings_service
from app.services import system_status

bp = Blueprint("settings", __name__)


@bp.route("/settings", methods=["GET", "POST"])
@requires("settings.view")
@handle_errors
def index():
    settings = settings_service.ensure_settings()
    form = SettingsForm(obj=settings)
    fill_choices(
        form,
        default_warehouse_id=choices_for(Warehouse, order_by=Warehouse.code),
    )
    if not form.is_submitted() and settings.wilaya_code:
        form.wilaya_code.data = settings.wilaya_code

    if form.validate_on_submit():
        from app.security import can

        if not can("settings.manage"):
            flash(translate("auth.forbidden"), "danger")
            return redirect(url_for("settings.index"))
        changes = {
            "business_name": form.business_name.data,
            "legal_name": form.legal_name.data,
            "tax_id": form.tax_id.data,
            "registry_no": form.registry_no.data,
            "activity": form.activity.data,
            "phone": form.phone.data,
            "email": form.email.data,
            "street": form.street.data,
            "commune": form.commune.data,
            "locality": form.locality.data,
            "wilaya_code": form.wilaya_code.data or None,
            "currency": form.currency.data,
            "currency_symbol": form.currency_symbol.data,
            "timezone": form.timezone.data,
            "default_language": form.default_language.data,
            "vat_rate": form.vat_rate.data,
            "prices_include_tax": bool(form.prices_include_tax.data),
            "default_payment_terms_days": form.default_payment_terms_days.data or 0,
            "weigh_tolerance_percent": form.weigh_tolerance_percent.data or Decimal("0"),
            "allow_negative_stock": bool(form.allow_negative_stock.data),
            "enable_weighbridge": bool(form.enable_weighbridge.data),
            "enable_fleet": bool(form.enable_fleet.data),
            "enable_accounting": bool(form.enable_accounting.data),
            "enable_ai": bool(form.enable_ai.data),
            "default_warehouse_id": form.default_warehouse_id.data or None,
            "bank_details": form.bank_details.data,
            "invoice_footer": form.invoice_footer.data,
        }
        settings_service.update_settings(changes)
        audit.record("updated", "business_settings", settings.id, summary="settings saved")
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("settings.index"))

    return render_template("settings/index.html", form=form, settings=settings)


@bp.get("/settings/system")
@requires("settings.manage")
def system():
    """Read-only release, database, backup, license and installation status."""
    return render_template("settings/system.html", status=system_status.get_system_status())

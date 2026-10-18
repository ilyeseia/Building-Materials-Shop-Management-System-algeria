"""Finance: customer receipts and supplier payments."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.extensions import db
from app.forms import PaymentForm
from app.i18n import format_money, translate
from app.models import Customer, Payment, PaymentDirection, Supplier
from app.blueprints.helpers import choices_for, fill_choices, handle_errors, page_args, parse_int
from app.security import requires
from app.services import payments as payments_service, reporting
from app.services.errors import ValidationError
from app.services.timeutil import today_local

bp = Blueprint("finance", __name__)


@bp.route("/finance/payments")
@requires("payments.view")
def payments():
    page, per_page = page_args()
    direction = (request.args.get("direction") or "").strip()
    query = select(Payment).order_by(Payment.payment_date.desc(), Payment.id.desc())
    if direction in {PaymentDirection.INBOUND.value, PaymentDirection.OUTBOUND.value}:
        query = query.where(Payment.direction == direction)
    result = reporting.paginate(query, page, per_page)
    rows = [
        {
            "number": payment.number,
            "date": payment.payment_date.strftime("%d/%m/%Y"),
            "party": (payment.customer.name if payment.customer else payment.supplier.name if payment.supplier else ""),
            "direction": translate(f"payment.{payment.direction}"),
            "method": translate(f"payment.{payment.method}"),
            "amount": format_money(payment.amount),
            "allocated": format_money(payment.allocated_amount),
            "status": translate(f"status.{payment.status}"),
            "_url": url_for("finance.payment_detail", payment_id=payment.id),
        }
        for payment in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="nav.payments", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "party", "label": "common.party"},
            {"key": "direction", "label": "payment.direction"},
            {"key": "method", "label": "payment.method"},
            {"key": "amount", "label": "payment.amount"},
            {"key": "allocated", "label": "payment.allocated"},
            {"key": "status", "label": "common.status"},
        ],
        result=result, search="", search_url=url_for("finance.payments"),
        create_url=url_for("finance.payment_new") if _can("payments.manage") else None,
        extra_filters={
            "name": "direction", "label": "payment.direction", "selected": direction,
            "options": [(None, "—"), ("inbound", translate("payment.inbound")), ("outbound", translate("payment.outbound"))],
        },
    )


@bp.route("/finance/payments/new", methods=["GET", "POST"])
@requires("payments.manage")
@handle_errors
def payment_new():
    form = PaymentForm()
    fill_choices(
        form,
        customer_id=choices_for(Customer, order_by=Customer.name),
        supplier_id=choices_for(Supplier, order_by=Supplier.name),
    )
    preset = (request.args.get("direction") or "").strip()
    if preset in {"inbound", "outbound"} and not form.is_submitted():
        form.direction.data = preset

    if request.method == "POST":
        if form.direction.data == PaymentDirection.INBOUND.value:
            customer = db.session.get(Customer, form.customer_id.data) if form.customer_id.data else None
            if customer is None:
                raise ValidationError("sale.customer_required")
            payment = payments_service.record_payment(
                direction=PaymentDirection.INBOUND.value,
                customer=customer,
                amount=form.amount.data,
                payment_date=form.payment_date.data or today_local(),
                method=form.method.data,
                reference=form.reference.data,
                notes=form.notes.data,
                auto_allocate=bool(form.auto_allocate.data),
            )
        else:
            supplier = db.session.get(Supplier, form.supplier_id.data) if form.supplier_id.data else None
            if supplier is None:
                raise ValidationError("val.required")
            payment = payments_service.record_payment(
                direction=PaymentDirection.OUTBOUND.value,
                supplier=supplier,
                amount=form.amount.data,
                payment_date=form.payment_date.data or today_local(),
                method=form.method.data,
                reference=form.reference.data,
                notes=form.notes.data,
                auto_allocate=bool(form.auto_allocate.data),
            )
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("finance.payment_detail", payment_id=payment.id))
    return render_template("finance/payment_form.html", form=form, today=today_local())


@bp.route("/finance/payments/<int:payment_id>")
@requires("payments.view")
def payment_detail(payment_id: int):
    payment = db.session.get(Payment, payment_id)
    if payment is None:
        return redirect(url_for("finance.payments"))
    return render_template("finance/payment_detail.html", payment=payment)


@bp.route("/finance/payments/<int:payment_id>/reverse", methods=["POST"])
@requires("payments.manage")
@handle_errors
def payment_reverse(payment_id: int):
    payment = db.session.get(Payment, payment_id)
    if payment is None:
        return redirect(url_for("finance.payments"))
    payments_service.reverse_payment(payment, reason=request.form.get("reason") or "")
    db.session.commit()
    flash(translate("payment.reversed_done"), "success")
    return redirect(url_for("finance.payment_detail", payment_id=payment.id))


@bp.route("/finance/payments/<int:payment_id>/allocate", methods=["POST"])
@requires("payments.manage")
@handle_errors
def payment_allocate(payment_id: int):
    payment = db.session.get(Payment, payment_id)
    if payment is None:
        return redirect(url_for("finance.payments"))
    document_type = request.form.get("document_type")
    document_id = parse_int(request.form.get("document_id"))
    amount = request.form.get("amount")
    if document_type and document_id:
        payments_service.allocate(payment, document_type, document_id, amount)
        db.session.commit()
        flash(translate("payment.allocated_done"), "success")
    else:
        flash(translate("val.required"), "danger")
    return redirect(url_for("finance.payment_detail", payment_id=payment.id))


def _can(permission: str) -> bool:
    from app.security import can

    return can(permission)

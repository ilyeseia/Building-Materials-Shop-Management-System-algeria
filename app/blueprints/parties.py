"""Customers and suppliers."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select

from app.extensions import db
from app.forms import CustomerForm, SupplierForm
from app.i18n import translate, wilaya_name
from app.models import Customer, CustomerType, SalesInvoice, Supplier, SupplierInvoice
from app.blueprints.helpers import choices_for, fill_choices, handle_errors, parse_int, page_args
from app.security import requires
from app.services import audit, payments, reporting
from app.services.timeutil import today_local

bp = Blueprint("parties", __name__)


def _customer_to_form(form: CustomerForm, customer: Customer | None = None) -> None:
    if customer is None:
        return
    form.code.data = customer.code
    form.name.data = customer.name
    form.contact_person.data = customer.contact_person
    form.phone.data = customer.phone
    form.phone_secondary.data = customer.phone_secondary
    form.email.data = customer.email
    form.wilaya_code.data = customer.wilaya_code or 0
    form.commune.data = customer.commune
    form.locality.data = customer.locality
    form.street.data = customer.street
    form.address_details.data = customer.address_details
    form.tax_id.data = customer.tax_id
    form.registry_no.data = customer.registry_no
    form.payment_terms_days.data = customer.payment_terms_days
    form.opening_balance.data = customer.opening_balance
    form.notes.data = customer.notes
    form.customer_type.data = customer.customer_type
    form.credit_limit.data = customer.credit_limit
    form.is_blocked.data = customer.is_blocked


def _apply_party(form, party) -> None:
    party.code = form.code.data or None
    party.name = form.name.data
    party.contact_person = form.contact_person.data
    party.phone = form.phone.data
    party.phone_secondary = form.phone_secondary.data
    party.email = form.email.data
    party.wilaya_code = form.wilaya_code.data or None
    party.commune = form.commune.data
    party.locality = form.locality.data
    party.street = form.street.data
    party.address_details = form.address_details.data
    party.tax_id = form.tax_id.data
    party.registry_no = form.registry_no.data
    party.payment_terms_days = form.payment_terms_days.data or 0
    party.opening_balance = form.opening_balance.data or 0
    party.notes = form.notes.data


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@bp.route("/customers")
@requires("customers.view")
def customers():
    page, per_page = page_args()
    search = (request.args.get("q") or "").strip()
    query = select(Customer)
    if search:
        pattern = f"%{search}%"
        query = query.where(
            Customer.name.ilike(pattern)
            | Customer.phone.ilike(pattern)
            | Customer.code.ilike(pattern)
        )
    query = query.order_by(Customer.name)
    result = reporting.paginate(query, page, per_page)
    rows = []
    for customer in result["items"]:
        rows.append(
            {
                "code": customer.code or "",
                "name": customer.name,
                "phone": customer.phone or "",
                "wilaya": wilaya_name(customer.wilaya_code),
                "type": translate(f"customer.{customer.customer_type}"),
                "balance": customer_balance_display(customer.id),
                "_url": url_for("parties.customer_detail", customer_id=customer.id),
            }
        )
    return render_template(
        "crud/list.html",
        title_key="nav.customers",
        rows=rows,
        columns=[
            {"key": "code", "label": "common.code"},
            {"key": "name", "label": "common.name"},
            {"key": "phone", "label": "common.phone"},
            {"key": "wilaya", "label": "address.wilaya"},
            {"key": "type", "label": "customer.type"},
            {"key": "balance", "label": "customer.balance"},
        ],
        result=result,
        search=search,
        searchable=True,
        create_url=url_for("parties.customer_new") if _can("customers.manage") else None,
        search_url=url_for("parties.customers"),
    )


def customer_balance_display(customer_id: int) -> str:
    from app.i18n import format_money

    return format_money(payments.customer_balance(customer_id))


@bp.route("/customers/new", methods=["GET", "POST"])
@requires("customers.manage")
@handle_errors
def customer_new():
    form = CustomerForm()
    form.wilaya_code.choices = [(0, "—")] + [(code, name) for code, name in __import__("config").WILAYAS.items()]
    if form.validate_on_submit():
        customer = Customer()
        _apply_party(form, customer)
        customer.customer_type = form.customer_type.data
        customer.credit_limit = form.credit_limit.data or 0
        customer.is_blocked = bool(form.is_blocked.data)
        db.session.add(customer)
        db.session.flush()
        audit.record("created", "customer", customer.id, summary=customer.name)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("parties.customer_detail", customer_id=customer.id))
    return render_template("crud/form.html", form=form, title_key="customer.new",
                           cancel_url=url_for("parties.customers"))


@bp.route("/customers/<int:customer_id>")
@requires("customers.view")
def customer_detail(customer_id: int):
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return redirect(url_for("parties.customers"))
    invoices = db.session.execute(
        select(SalesInvoice)
        .where(SalesInvoice.customer_id == customer_id)
        .order_by(SalesInvoice.invoice_date.desc())
        .limit(20)
    ).scalars().all()
    from app.models import Payment, PaymentDirection

    receipts = db.session.execute(
        select(Payment)
        .where(Payment.customer_id == customer_id, Payment.direction == PaymentDirection.INBOUND.value)
        .order_by(Payment.payment_date.desc())
        .limit(10)
    ).scalars().all()
    return render_template(
        "parties/customer_detail.html",
        customer=customer,
        invoices=invoices,
        receipts=receipts,
        balance=payments.customer_balance(customer_id),
        credit=payments.customer_credit(customer_id),
    )


@bp.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@requires("customers.manage")
@handle_errors
def customer_edit(customer_id: int):
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return redirect(url_for("parties.customers"))
    form = CustomerForm(obj=customer)
    if not form.is_submitted():
        _customer_to_form(form, customer)
    if form.validate_on_submit():
        _apply_party(form, customer)
        customer.customer_type = form.customer_type.data
        customer.credit_limit = form.credit_limit.data or 0
        customer.is_blocked = bool(form.is_blocked.data)
        audit.record("updated", "customer", customer.id, summary=customer.name)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("parties.customer_detail", customer_id=customer.id))
    return render_template("crud/form.html", form=form, title_key="customer.edit",
                           cancel_url=url_for("parties.customer_detail", customer_id=customer.id))


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

@bp.route("/suppliers")
@requires("suppliers.view")
def suppliers():
    page, per_page = page_args()
    search = (request.args.get("q") or "").strip()
    query = select(Supplier)
    if search:
        pattern = f"%{search}%"
        query = query.where(Supplier.name.ilike(pattern) | Supplier.phone.ilike(pattern))
    query = query.order_by(Supplier.name)
    result = reporting.paginate(query, page, per_page)
    from app.i18n import format_money

    rows = [
        {
            "code": supplier.code or "",
            "name": supplier.name,
            "phone": supplier.phone or "",
            "wilaya": wilaya_name(supplier.wilaya_code),
            "terms": str(supplier.payment_terms_days or 0),
            "balance": format_money(payments.supplier_balance(supplier.id)),
            "_url": url_for("parties.supplier_detail", supplier_id=supplier.id),
        }
        for supplier in result["items"]
    ]
    return render_template(
        "crud/list.html",
        title_key="nav.suppliers",
        rows=rows,
        columns=[
            {"key": "code", "label": "common.code"},
            {"key": "name", "label": "common.name"},
            {"key": "phone", "label": "common.phone"},
            {"key": "wilaya", "label": "address.wilaya"},
            {"key": "terms", "label": "party.payment_terms"},
            {"key": "balance", "label": "supplier.balance"},
        ],
        result=result,
        search=search,
        searchable=True,
        create_url=url_for("parties.supplier_new") if _can("suppliers.manage") else None,
        search_url=url_for("parties.suppliers"),
    )


def _supplier_to_form(form: SupplierForm, supplier: Supplier) -> None:
    _customer_to_form(form, supplier)
    form.supplied_materials.data = supplier.supplied_materials


@bp.route("/suppliers/new", methods=["GET", "POST"])
@requires("suppliers.manage")
@handle_errors
def supplier_new():
    form = SupplierForm()
    if form.validate_on_submit():
        supplier = Supplier()
        _apply_party(form, supplier)
        supplier.supplied_materials = form.supplied_materials.data
        db.session.add(supplier)
        db.session.flush()
        audit.record("created", "supplier", supplier.id, summary=supplier.name)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("parties.supplier_detail", supplier_id=supplier.id))
    return render_template("crud/form.html", form=form, title_key="supplier.new",
                           cancel_url=url_for("parties.suppliers"))


@bp.route("/suppliers/<int:supplier_id>")
@requires("suppliers.view")
def supplier_detail(supplier_id: int):
    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        return redirect(url_for("parties.suppliers"))
    invoices = db.session.execute(
        select(SupplierInvoice)
        .where(SupplierInvoice.supplier_id == supplier_id)
        .order_by(SupplierInvoice.invoice_date.desc())
        .limit(20)
    ).scalars().all()
    return render_template(
        "parties/supplier_detail.html",
        supplier=supplier,
        invoices=invoices,
        balance=payments.supplier_balance(supplier_id),
    )


@bp.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
@requires("suppliers.manage")
@handle_errors
def supplier_edit(supplier_id: int):
    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        return redirect(url_for("parties.suppliers"))
    form = SupplierForm(obj=supplier)
    if not form.is_submitted():
        _supplier_to_form(form, supplier)
    if form.validate_on_submit():
        _apply_party(form, supplier)
        supplier.supplied_materials = form.supplied_materials.data
        audit.record("updated", "supplier", supplier.id, summary=supplier.name)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("parties.supplier_detail", supplier_id=supplier.id))
    return render_template("crud/form.html", form=form, title_key="supplier.edit",
                           cancel_url=url_for("parties.supplier_detail", supplier_id=supplier.id))


def _can(permission: str) -> bool:
    from app.security import can

    return can(permission)

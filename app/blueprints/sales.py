"""Sales: quotations, orders, delivery notes, invoices, credit notes, direct sale."""
from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.extensions import db
from app.forms import SalesDocumentForm
from app.i18n import format_money, format_quantity_with_unit, localized_name, translate
from app.models import (
    CreditNote,
    Customer,
    DeliveryNote,
    DocumentStatus,
    Product,
    Quotation,
    SalesInvoice,
    SalesOrder,
    Warehouse,
)
from app.blueprints.helpers import (
    choices_for,
    fill_choices,
    form_lines,
    handle_errors,
    page_args,
    parse_date,
    parse_decimal,
    parse_int,
)
from app.security import requires
from app.services import audit, reporting, sales as sales_service, stock
from app.services.errors import BusinessError, ValidationError
from app.services.timeutil import add_days, today_local

bp = Blueprint("sales", __name__)


# ---------------------------------------------------------------------------
# Shared form preparation
# ---------------------------------------------------------------------------

def _document_form(form: SalesDocumentForm) -> None:
    fill_choices(
        form,
        customer_id=choices_for(Customer, order_by=Customer.name),
        warehouse_id=choices_for(Warehouse, order_by=Warehouse.code, blank=False),
    )


def _line_inputs():
    return [
        sales_service.LineInput(
            product_id=line["product_id"],
            quantity=line["quantity"],
            unit_id=line["unit_id"],
            unit_price=line["unit_price"],
            discount_percent=line["discount_percent"] or Decimal("0"),
            description=line["description"],
        )
        for line in form_lines()
    ]


def _customer_from_form(form: SalesDocumentForm) -> Customer:
    customer = db.session.get(Customer, form.customer_id.data) if form.customer_id.data else None
    if customer is None:
        raise ValidationError("sale.customer_required")
    return customer


def _warehouse_from_form(form: SalesDocumentForm) -> Warehouse | None:
    return db.session.get(Warehouse, form.warehouse_id.data) if form.warehouse_id.data else None


def _products_payload():
    products = db.session.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.name)
    ).scalars().all()
    from app.services import uom

    return [
        {
            "id": product.id,
            "name": product.name,
            "sku": product.sku or "",
            "price": str(product.sale_price or 0),
            "wholesale": str(product.wholesale_price or product.sale_price or 0),
            "contractor": str(product.contractor_price or product.sale_price or 0),
            "cost": str(product.cost_price or 0),
            "stock_unit": product.stock_unit_code,
            "available": str(stock.available(product)),
            "units": [{"id": u.id, "code": u.code} for u in uom.allowed_units(product, "sale")],
        }
        for product in products
    ]


# ---------------------------------------------------------------------------
# Quotations
# ---------------------------------------------------------------------------

@bp.route("/sales/quotations")
@requires("sales.view")
def quotations():
    page, per_page = page_args()
    result = reporting.paginate(
        select(Quotation).order_by(Quotation.quotation_date.desc(), Quotation.id.desc()), page, per_page
    )
    rows = [
        {
            "number": item.number,
            "date": item.quotation_date.strftime("%d/%m/%Y"),
            "customer": item.customer.name if item.customer else "",
            "total": format_money(item.total_amount),
            "status": translate(f"status.{item.status}"),
            "_url": url_for("sales.quotation_detail", quotation_id=item.id),
        }
        for item in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="sale.quotations", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "customer", "label": "sale.customer"},
            {"key": "total", "label": "common.total"},
            {"key": "status", "label": "common.status"},
        ],
        result=result, search="", search_url=url_for("sales.quotations"),
        create_url=url_for("sales.quotation_new") if _can("sales.manage") else None,
    )


@bp.route("/sales/quotations/new", methods=["GET", "POST"])
@requires("sales.manage")
@handle_errors
def quotation_new():
    form = SalesDocumentForm()
    _document_form(form)
    if request.method == "POST":
        lines = _line_inputs()
        quotation = sales_service.create_quotation(
            customer=_customer_from_form(form),
            lines=lines,
            quotation_date=form.document_date.data or today_local(),
            valid_until=form.valid_until.data,
            warehouse=_warehouse_from_form(form),
            price_list=form.price_list.data,
            notes=form.notes.data,
        )
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("sales.quotation_detail", quotation_id=quotation.id))
    return render_template(
        "sales/quotation_form.html", form=form, products=_products_payload(),
        today=today_local(), default_valid=add_days(today_local(), 30),
    )


@bp.route("/sales/quotations/<int:quotation_id>")
@requires("sales.view")
def quotation_detail(quotation_id: int):
    quotation = db.session.get(Quotation, quotation_id)
    if quotation is None:
        return redirect(url_for("sales.quotations"))
    return render_template("sales/quotation_detail.html", quotation=quotation)


@bp.route("/sales/quotations/<int:quotation_id>/accept", methods=["POST"])
@requires("sales.manage")
@handle_errors
def quotation_accept(quotation_id: int):
    quotation = db.session.get(Quotation, quotation_id)
    if quotation is None:
        return redirect(url_for("sales.quotations"))
    order = sales_service.accept_quotation(quotation)
    db.session.commit()
    flash(translate("sale.quotation_accepted"), "success")
    return redirect(url_for("sales.order_detail", order_id=order.id))


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@bp.route("/sales/orders")
@requires("sales.view")
def orders():
    page, per_page = page_args()
    status = (request.args.get("status") or "").strip()
    query = select(SalesOrder).order_by(SalesOrder.order_date.desc(), SalesOrder.id.desc())
    if status:
        query = query.where(SalesOrder.status == status)
    result = reporting.paginate(query, page, per_page)
    rows = [
        {
            "number": order.number,
            "date": order.order_date.strftime("%d/%m/%Y"),
            "customer": order.customer.name if order.customer else "",
            "total": format_money(order.total_amount),
            "status": translate(f"status.{order.status}"),
            "_url": url_for("sales.order_detail", order_id=order.id),
        }
        for order in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="sale.orders", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "customer", "label": "sale.customer"},
            {"key": "total", "label": "common.total"},
            {"key": "status", "label": "common.status"},
        ],
        result=result, search="", search_url=url_for("sales.orders"),
        create_url=url_for("sales.order_new") if _can("sales.manage") else None,
        extra_filters={
            "name": "status", "label": "common.status", "selected": status,
            "options": [(None, "—")] + [
                (value, translate(f"status.{value}"))
                for value in ("draft", "confirmed", "partially_delivered", "delivered", "invoiced", "cancelled")
            ],
        },
    )


@bp.route("/sales/orders/new", methods=["GET", "POST"])
@requires("sales.manage")
@handle_errors
def order_new():
    form = SalesDocumentForm()
    _document_form(form)
    if request.method == "POST":
        order = sales_service.create_order(
            customer=_customer_from_form(form),
            lines=_line_inputs(),
            order_date=form.document_date.data or today_local(),
            delivery_date=form.delivery_date.data,
            warehouse=_warehouse_from_form(form),
            price_list=form.price_list.data,
            delivery_address=form.delivery_address.data,
            notes=form.notes.data,
            confirm=bool(request.form.get("confirm_now")),
        )
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("sales.order_detail", order_id=order.id))
    return render_template("sales/order_form.html", form=form, products=_products_payload(), today=today_local())


@bp.route("/sales/orders/<int:order_id>")
@requires("sales.view")
def order_detail(order_id: int):
    order = db.session.get(SalesOrder, order_id)
    if order is None:
        return redirect(url_for("sales.orders"))
    return render_template(
        "sales/order_detail.html", order=order,
        deliveries=list(order.delivery_notes), invoices=list(order.invoices),
    )


@bp.route("/sales/orders/<int:order_id>/confirm", methods=["POST"])
@requires("sales.manage")
@handle_errors
def order_confirm(order_id: int):
    order = db.session.get(SalesOrder, order_id)
    if order is None:
        return redirect(url_for("sales.orders"))
    sales_service.confirm_order(order)
    db.session.commit()
    flash(translate("sale.order_confirmed"), "success")
    return redirect(url_for("sales.order_detail", order_id=order.id))


@bp.route("/sales/orders/<int:order_id>/cancel", methods=["POST"])
@requires("sales.manage")
@handle_errors
def order_cancel(order_id: int):
    order = db.session.get(SalesOrder, order_id)
    if order is None:
        return redirect(url_for("sales.orders"))
    sales_service.cancel_order(order, reason=request.form.get("reason") or "", )
    db.session.commit()
    flash(translate("sale.order_cancelled"), "success")
    return redirect(url_for("sales.order_detail", order_id=order.id))


# ---------------------------------------------------------------------------
# Deliveries
# ---------------------------------------------------------------------------

@bp.route("/sales/orders/<int:order_id>/deliver", methods=["GET", "POST"])
@requires("sales.manage")
@handle_errors
def order_deliver(order_id: int):
    order = db.session.get(SalesOrder, order_id)
    if order is None:
        return redirect(url_for("sales.orders"))
    if request.method == "POST":
        deliveries = []
        for item in order.items:
            raw = request.form.get(f"deliver_{item.id}")
            quantity = parse_decimal(raw)
            if quantity and quantity > 0:
                deliveries.append({"order_item_id": item.id, "quantity": quantity})
        note = sales_service.deliver_order(
            order,
            deliveries=deliveries,
            delivery_date=parse_date(request.form.get("delivery_date")) or today_local(),
            transport_fee=parse_decimal(request.form.get("transport_fee")),
            received_by_name=request.form.get("received_by_name") or None,
            notes=request.form.get("notes") or None,
        )
        db.session.commit()
        flash(translate("sale.delivered"), "success")
        return redirect(url_for("sales.delivery_detail", delivery_id=note.id))
    return render_template("sales/deliver_form.html", order=order, today=today_local())


@bp.route("/sales/deliveries")
@requires("sales.view")
def deliveries():
    page, per_page = page_args()
    result = reporting.paginate(
        select(DeliveryNote).order_by(DeliveryNote.delivery_date.desc(), DeliveryNote.id.desc()),
        page, per_page,
    )
    rows = [
        {
            "number": note.number,
            "date": note.delivery_date.strftime("%d/%m/%Y"),
            "customer": note.customer.name if note.customer else "",
            "total": format_money(note.total_amount),
            "status": translate(f"status.{note.status}"),
            "_url": url_for("sales.delivery_detail", delivery_id=note.id),
        }
        for note in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="sale.deliveries", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "customer", "label": "sale.customer"},
            {"key": "total", "label": "common.total"},
            {"key": "status", "label": "common.status"},
        ],
        result=result, search="", search_url=url_for("sales.deliveries"),
    )


@bp.route("/sales/deliveries/<int:delivery_id>")
@requires("sales.view")
def delivery_detail(delivery_id: int):
    note = db.session.get(DeliveryNote, delivery_id)
    if note is None:
        return redirect(url_for("sales.deliveries"))
    return render_template("sales/delivery_detail.html", delivery=note, invoices=list(note.invoices))


@bp.route("/sales/deliveries/<int:delivery_id>/invoice", methods=["POST"])
@requires("sales.manage")
@handle_errors
def delivery_invoice(delivery_id: int):
    note = db.session.get(DeliveryNote, delivery_id)
    if note is None:
        return redirect(url_for("sales.deliveries"))
    terms = note.customer.payment_terms_days if note.customer else 0
    due = add_days(today_local(), int(terms or 0)) if terms else None
    invoice = sales_service.create_invoice_from_delivery(note, due_date=due)
    db.session.commit()
    flash(translate("sale.invoiced"), "success")
    return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

@bp.route("/sales/invoices")
@requires("sales.view")
def invoices():
    page, per_page = page_args()
    query = select(SalesInvoice).order_by(SalesInvoice.invoice_date.desc(), SalesInvoice.id.desc())
    result = reporting.paginate(query, page, per_page)
    rows = [
        {
            "number": invoice.number,
            "date": invoice.invoice_date.strftime("%d/%m/%Y"),
            "customer": invoice.customer.name if invoice.customer else "",
            "total": format_money(invoice.total_amount),
            "balance": format_money(invoice.balance_due),
            "settlement": translate(f"settlement.{invoice.settlement_status}"),
            "_url": url_for("sales.invoice_detail", invoice_id=invoice.id),
        }
        for invoice in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="sale.invoices", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "customer", "label": "sale.customer"},
            {"key": "total", "label": "common.total"},
            {"key": "balance", "label": "payment.balance"},
            {"key": "settlement", "label": "payment.status"},
        ],
        result=result, search="", search_url=url_for("sales.invoices"),
    )


@bp.route("/sales/invoices/<int:invoice_id>")
@requires("sales.view")
def invoice_detail(invoice_id: int):
    invoice = db.session.get(SalesInvoice, invoice_id)
    if invoice is None:
        return redirect(url_for("sales.invoices"))
    from app.models import Payment

    payments_list = db.session.execute(
        select(Payment).where(
            Payment.customer_id == invoice.customer_id,
            Payment.status == DocumentStatus.POSTED.value,
        ).order_by(Payment.payment_date.desc()).limit(10)
    ).scalars().all()
    return render_template(
        "sales/invoice_detail.html", invoice=invoice,
        credit_notes=list(invoice.credit_notes), payments=payments_list,
    )


@bp.route("/sales/invoices/<int:invoice_id>/print")
@requires("sales.view")
def invoice_print(invoice_id: int):
    invoice = db.session.get(SalesInvoice, invoice_id)
    if invoice is None:
        return redirect(url_for("sales.invoices"))
    return render_template("print/document.html", document=invoice, kind="invoice")


@bp.route("/sales/invoices/<int:invoice_id>.pdf")
@requires("sales.view")
def invoice_pdf(invoice_id: int):
    invoice = db.session.get(SalesInvoice, invoice_id)
    if invoice is None:
        return redirect(url_for("sales.invoices"))
    pdf_bytes = _invoice_pdf(invoice)
    if pdf_bytes is None:
        flash(translate("print.pdf_unavailable"), "warning")
        return redirect(url_for("sales.invoice_print", invoice_id=invoice.id))
    filename = f"{invoice.number or 'invoice'}.pdf"
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


def _invoice_pdf(invoice: SalesInvoice) -> bytes | None:
    from app.i18n import get_locale
    from app.services import pdf as pdf_service

    if not pdf_service.pdf_available():
        return None
    locale = get_locale()
    tables = []
    rows = []
    for item in invoice.items:
        rows.append([
            item.description or (item.product.name if item.product else ""),
            format_quantity_with_unit(item.quantity, item.unit.code if item.unit else None),
            format_money(item.unit_price),
            format_money(item.line_total, with_symbol=False),
        ])
    tables.append(
        pdf_service.PdfTable(
            headers=[translate("common.description"), translate("common.quantity"),
                     translate("common.unit_price"), translate("common.total")],
            rows=rows,
        )
    )
    return pdf_service.build_pdf(
        pdf_service.PdfDocument(
            title=translate("sale.invoice"),
            subtitle=invoice.number or "",
            meta=[
                (translate("sale.customer"), invoice.customer.name if invoice.customer else ""),
                (translate("common.date"), invoice.invoice_date.strftime("%d/%m/%Y")),
                (translate("sale.due_date"), invoice.due_date.strftime("%d/%m/%Y") if invoice.due_date else ""),
            ],
            tables=tables,
            totals=[
                (translate("common.subtotal"), format_money(invoice.subtotal, with_symbol=False)),
                (translate("common.tax"), format_money(invoice.tax_amount, with_symbol=False)),
                (translate("common.total"), format_money(invoice.total_amount)),
            ],
            language=locale,
        )
    )


# ---------------------------------------------------------------------------
# Credit notes
# ---------------------------------------------------------------------------

@bp.route("/sales/invoices/<int:invoice_id>/credit-note", methods=["GET", "POST"])
@requires("sales.manage")
@handle_errors
def credit_note_new(invoice_id: int):
    invoice = db.session.get(SalesInvoice, invoice_id)
    if invoice is None:
        return redirect(url_for("sales.invoices"))
    if request.method == "POST":
        lines = _line_inputs()
        note = sales_service.create_credit_note(
            invoice=invoice,
            lines=lines,
            reason=request.form.get("reason") or "",
            restock=bool(request.form.get("restock")),
        )
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))
    return render_template(
        "sales/credit_note_form.html", invoice=invoice, products=_products_payload(), today=today_local()
    )


# ---------------------------------------------------------------------------
# Direct (counter) sale
# ---------------------------------------------------------------------------

@bp.route("/sales/direct", methods=["GET", "POST"])
@requires("sales.manage")
@handle_errors
def direct_sale():
    form = SalesDocumentForm()
    _document_form(form)
    if request.method == "POST":
        customer = db.session.get(Customer, form.customer_id.data) if form.customer_id.data else None
        delivery, invoice = sales_service.direct_sale(
            customer=customer,
            lines=_line_inputs(),
            warehouse=_warehouse_from_form(form),
            price_list=form.price_list.data,
            sale_date=form.document_date.data or today_local(),
            transport_fee=parse_decimal(request.form.get("transport_fee")),
            notes=form.notes.data,
        )
        paid = parse_decimal(request.form.get("paid_amount"))
        if paid and paid > 0:
            from app.services import payments as payments_service

            payments_service.record_payment(
                direction="inbound",
                customer=invoice.customer,
                amount=paid,
                payment_date=invoice.invoice_date,
                method=request.form.get("payment_method") or "cash",
                allocations=[{
                    "document_type": payments_service.SALES_INVOICE,
                    "document_id": invoice.id,
                    "amount": min(paid, invoice.total_amount),
                }],
            )
        db.session.commit()
        flash(translate("sale.invoiced"), "success")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))
    return render_template("sales/direct_form.html", form=form, products=_products_payload(), today=today_local())


def _can(permission: str) -> bool:
    from app.security import can

    return can(permission)

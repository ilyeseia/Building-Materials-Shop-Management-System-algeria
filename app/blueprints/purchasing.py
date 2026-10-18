"""Purchasing: orders, goods receipts, supplier invoices, returns."""
from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.extensions import db
from app.forms import PurchaseDocumentForm, ReceiptCostsForm
from app.i18n import format_money, localized_name, translate
from app.models import (
    DocumentStatus,
    GoodsReceipt,
    PurchaseOrder,
    PurchaseReturn,
    Supplier,
    SupplierInvoice,
    Warehouse,
)
from app.blueprints.helpers import (
    choices_for,
    fill_choices,
    form_lines,
    handle_errors,
    page_args,
    parse_decimal,
    parse_int,
)
from app.security import requires
from app.services import purchasing as purchasing_service, reporting, stock
from app.services.errors import ValidationError
from app.services.timeutil import add_days, today_local

bp = Blueprint("purchasing", __name__)


def _form_choices(form) -> None:
    fill_choices(
        form,
        supplier_id=choices_for(Supplier, order_by=Supplier.name),
        warehouse_id=choices_for(Warehouse, order_by=Warehouse.code, blank=False),
    )


def _lines():
    return [
        purchasing_service.PurchaseLineInput(
            product_id=line["product_id"],
            quantity=line["quantity"],
            unit_id=line["unit_id"],
            unit_price=line["unit_price"],
            discount_percent=line["discount_percent"] or Decimal("0"),
            description=line["description"],
        )
        for line in form_lines()
    ]


def _purchase_products_payload():
    from app.models import Product
    from app.services import uom

    products = db.session.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.name)
    ).scalars().all()
    return [
        {
            "id": product.id,
            "name": product.name,
            "sku": product.sku or "",
            # ``price`` is the value the line-entry script drops into the price
            # input; for a purchase line that is the cost price.
            "price": str(product.cost_price or 0),
            "cost": str(product.cost_price or 0),
            "stock_unit": product.stock_unit_code,
            "available": str(stock.available(product)),
            "units": [{"id": u.id, "code": u.code} for u in uom.allowed_units(product, "purchase")],
        }
        for product in products
    ]


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------

@bp.route("/purchasing/orders")
@requires("purchasing.view")
def orders():
    page, per_page = page_args()
    result = reporting.paginate(
        select(PurchaseOrder).order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.id.desc()),
        page, per_page,
    )
    rows = [
        {
            "number": order.number,
            "date": order.order_date.strftime("%d/%m/%Y"),
            "supplier": order.supplier.name if order.supplier else "",
            "total": format_money(order.total_amount),
            "status": translate(f"status.{order.status}"),
            "_url": url_for("purchasing.order_detail", order_id=order.id),
        }
        for order in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="purchase.orders", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "supplier", "label": "purchase.supplier"},
            {"key": "total", "label": "common.total"},
            {"key": "status", "label": "common.status"},
        ],
        result=result, search="", search_url=url_for("purchasing.orders"),
        create_url=url_for("purchasing.order_new") if _can("purchasing.manage") else None,
    )


@bp.route("/purchasing/orders/new", methods=["GET", "POST"])
@requires("purchasing.manage")
@handle_errors
def order_new():
    form = PurchaseDocumentForm()
    _form_choices(form)
    if request.method == "POST":
        supplier = db.session.get(Supplier, form.supplier_id.data) if form.supplier_id.data else None
        if supplier is None:
            raise ValidationError("val.required")
        warehouse = db.session.get(Warehouse, form.warehouse_id.data) if form.warehouse_id.data else None
        order = purchasing_service.create_order(
            supplier=supplier,
            lines=_lines(),
            order_date=form.document_date.data or today_local(),
            expected_date=form.expected_date.data,
            warehouse=warehouse,
            supplier_reference=form.supplier_reference.data,
            notes=form.notes.data,
            confirm=bool(request.form.get("confirm_now")),
        )
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("purchasing.order_detail", order_id=order.id))
    return render_template("purchasing/order_form.html", form=form, products=_purchase_products_payload(), today=today_local())


@bp.route("/purchasing/orders/<int:order_id>")
@requires("purchasing.view")
def order_detail(order_id: int):
    order = db.session.get(PurchaseOrder, order_id)
    if order is None:
        return redirect(url_for("purchasing.orders"))
    return render_template("purchasing/order_detail.html", order=order, receipts=list(order.receipts))


@bp.route("/purchasing/orders/<int:order_id>/confirm", methods=["POST"])
@requires("purchasing.manage")
@handle_errors
def order_confirm(order_id: int):
    order = db.session.get(PurchaseOrder, order_id)
    if order is None:
        return redirect(url_for("purchasing.orders"))
    purchasing_service.confirm_order(order)
    db.session.commit()
    flash(translate("common.saved"), "success")
    return redirect(url_for("purchasing.order_detail", order_id=order.id))


@bp.route("/purchasing/orders/<int:order_id>/cancel", methods=["POST"])
@requires("purchasing.manage")
@handle_errors
def order_cancel(order_id: int):
    order = db.session.get(PurchaseOrder, order_id)
    if order is None:
        return redirect(url_for("purchasing.orders"))
    purchasing_service.cancel_order(order, reason=request.form.get("reason") or "")
    db.session.commit()
    flash(translate("common.saved"), "success")
    return redirect(url_for("purchasing.order_detail", order_id=order.id))


# ---------------------------------------------------------------------------
# Goods receipts
# ---------------------------------------------------------------------------

@bp.route("/purchasing/receipts")
@requires("purchasing.view")
def receipts():
    page, per_page = page_args()
    result = reporting.paginate(
        select(GoodsReceipt).order_by(GoodsReceipt.receipt_date.desc(), GoodsReceipt.id.desc()),
        page, per_page,
    )
    rows = [
        {
            "number": receipt.number,
            "date": receipt.receipt_date.strftime("%d/%m/%Y"),
            "supplier": receipt.supplier.name if receipt.supplier else "",
            "warehouse": localized_name(receipt.warehouse),
            "landed": format_money(receipt.total_landed_cost),
            "status": translate(f"status.{receipt.status}"),
            "_url": url_for("purchasing.receipt_detail", receipt_id=receipt.id),
        }
        for receipt in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="purchase.receipts", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "supplier", "label": "purchase.supplier"},
            {"key": "warehouse", "label": "stock.warehouse"},
            {"key": "landed", "label": "purchase.landed_cost"},
            {"key": "status", "label": "common.status"},
        ],
        result=result, search="", search_url=url_for("purchasing.receipts"),
        create_url=url_for("purchasing.receipt_new") if _can("purchasing.manage") else None,
    )


@bp.route("/purchasing/receipts/new", methods=["GET", "POST"])
@requires("purchasing.manage")
@handle_errors
def receipt_new():
    form = PurchaseDocumentForm()
    costs = ReceiptCostsForm(prefix="cost")
    order_id = parse_int(request.values.get("order_id"))
    order = db.session.get(PurchaseOrder, order_id) if order_id else None
    _form_choices(form)
    if order is not None and not form.is_submitted():
        form.supplier_id.data = order.supplier_id
        form.warehouse_id.data = order.warehouse_id

    if request.method == "POST":
        supplier = db.session.get(Supplier, form.supplier_id.data) if form.supplier_id.data else None
        if supplier is None:
            raise ValidationError("val.required")
        warehouse = db.session.get(Warehouse, form.warehouse_id.data) if form.warehouse_id.data else None
        if order is not None:
            receipt = purchasing_service.create_receipt(
                order=order,
                receipt_date=form.document_date.data or today_local(),
                supplier_reference=form.supplier_reference.data,
                transport_cost=costs.transport_cost.data,
                handling_cost=costs.handling_cost.data,
                other_cost=costs.other_cost.data,
                notes=form.notes.data,
            )
        else:
            receipt = purchasing_service.create_receipt(
                supplier=supplier,
                lines=_lines(),
                receipt_date=form.document_date.data or today_local(),
                warehouse=warehouse,
                supplier_reference=form.supplier_reference.data,
                transport_cost=costs.transport_cost.data,
                handling_cost=costs.handling_cost.data,
                other_cost=costs.other_cost.data,
                notes=form.notes.data,
            )
        db.session.commit()
        if request.form.get("post_now"):
            return redirect(url_for("purchasing.receipt_post", receipt_id=receipt.id))
        flash(translate("common.saved"), "success")
        return redirect(url_for("purchasing.receipt_detail", receipt_id=receipt.id))
    return render_template(
        "purchasing/receipt_form.html", form=form, costs=costs, order=order,
        products=_purchase_products_payload(), today=today_local(),
    )


@bp.route("/purchasing/receipts/<int:receipt_id>")
@requires("purchasing.view")
def receipt_detail(receipt_id: int):
    receipt = db.session.get(GoodsReceipt, receipt_id)
    if receipt is None:
        return redirect(url_for("purchasing.receipts"))
    from app.models import SupplierInvoice

    invoices = db.session.execute(
        select(SupplierInvoice).where(SupplierInvoice.goods_receipt_id == receipt.id)
    ).scalars().all()
    return render_template("purchasing/receipt_detail.html", receipt=receipt, invoices=invoices)


@bp.route("/purchasing/receipts/<int:receipt_id>/post", methods=["POST"])
@requires("purchasing.manage")
@handle_errors
def receipt_post(receipt_id: int):
    receipt = db.session.get(GoodsReceipt, receipt_id)
    if receipt is None:
        return redirect(url_for("purchasing.receipts"))
    purchasing_service.post_receipt(receipt)
    db.session.commit()
    flash(translate("stock.posted"), "success")
    return redirect(url_for("purchasing.receipt_detail", receipt_id=receipt.id))


@bp.route("/purchasing/receipts/<int:receipt_id>/cancel", methods=["POST"])
@requires("purchasing.manage")
@handle_errors
def receipt_cancel(receipt_id: int):
    receipt = db.session.get(GoodsReceipt, receipt_id)
    if receipt is None:
        return redirect(url_for("purchasing.receipts"))
    purchasing_service.cancel_receipt(receipt, reason=request.form.get("reason") or "")
    db.session.commit()
    flash(translate("common.saved"), "success")
    return redirect(url_for("purchasing.receipt_detail", receipt_id=receipt.id))


# ---------------------------------------------------------------------------
# Supplier invoices
# ---------------------------------------------------------------------------

@bp.route("/purchasing/invoices")
@requires("purchasing.view")
def invoices():
    page, per_page = page_args()
    result = reporting.paginate(
        select(SupplierInvoice).order_by(SupplierInvoice.invoice_date.desc(), SupplierInvoice.id.desc()),
        page, per_page,
    )
    rows = [
        {
            "number": invoice.number,
            "date": invoice.invoice_date.strftime("%d/%m/%Y"),
            "supplier": invoice.supplier.name if invoice.supplier else "",
            "reference": invoice.supplier_reference or "",
            "total": format_money(invoice.total_amount),
            "balance": format_money(invoice.balance_due),
            "settlement": translate(f"settlement.{invoice.settlement_status}"),
            "_url": url_for("purchasing.invoice_detail", invoice_id=invoice.id),
        }
        for invoice in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="purchase.supplier_invoices", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "supplier", "label": "purchase.supplier"},
            {"key": "reference", "label": "purchase.supplier_reference"},
            {"key": "total", "label": "common.total"},
            {"key": "balance", "label": "payment.balance"},
            {"key": "settlement", "label": "payment.status"},
        ],
        result=result, search="", search_url=url_for("purchasing.invoices"),
    )


@bp.route("/purchasing/invoices/<int:invoice_id>")
@requires("purchasing.view")
def invoice_detail(invoice_id: int):
    invoice = db.session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        return redirect(url_for("purchasing.invoices"))
    return render_template("purchasing/invoice_detail.html", invoice=invoice)


@bp.route("/purchasing/receipts/<int:receipt_id>/invoice", methods=["POST"])
@requires("purchasing.manage")
@handle_errors
def receipt_invoice(receipt_id: int):
    receipt = db.session.get(GoodsReceipt, receipt_id)
    if receipt is None:
        return redirect(url_for("purchasing.receipts"))
    terms = receipt.supplier.payment_terms_days if receipt.supplier else 0
    due = add_days(today_local(), int(terms or 0)) if terms else None
    invoice = purchasing_service.create_invoice_from_receipt(receipt, due_date=due)
    db.session.commit()
    flash(translate("common.saved"), "success")
    return redirect(url_for("purchasing.invoice_detail", invoice_id=invoice.id))


# ---------------------------------------------------------------------------
# Purchase returns
# ---------------------------------------------------------------------------

@bp.route("/purchasing/returns")
@requires("purchasing.view")
def returns():
    page, per_page = page_args()
    result = reporting.paginate(
        select(PurchaseReturn).order_by(PurchaseReturn.return_date.desc()), page, per_page
    )
    rows = [
        {
            "number": item.number,
            "date": item.return_date.strftime("%d/%m/%Y"),
            "supplier": item.supplier.name if item.supplier else "",
            "reason": item.reason,
            "total": format_money(item.total_amount),
            "status": translate(f"status.{item.status}"),
            "_url": url_for("purchasing.return_detail", return_id=item.id),
        }
        for item in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="nav.returns", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "supplier", "label": "purchase.supplier"},
            {"key": "reason", "label": "common.reason"},
            {"key": "total", "label": "common.total"},
            {"key": "status", "label": "common.status"},
        ],
        result=result, search="", search_url=url_for("purchasing.returns"),
        create_url=url_for("purchasing.return_new") if _can("purchasing.manage") else None,
    )


@bp.route("/purchasing/returns/new", methods=["GET", "POST"])
@requires("purchasing.manage")
@handle_errors
def return_new():
    form = PurchaseDocumentForm()
    _form_choices(form)
    if request.method == "POST":
        supplier = db.session.get(Supplier, form.supplier_id.data) if form.supplier_id.data else None
        if supplier is None:
            raise ValidationError("val.required")
        warehouse = db.session.get(Warehouse, form.warehouse_id.data) if form.warehouse_id.data else None
        document = purchasing_service.create_return(
            supplier=supplier,
            lines=_lines(),
            reason=request.form.get("reason") or "",
            return_date=form.document_date.data or today_local(),
            warehouse=warehouse,
        )
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("purchasing.return_detail", return_id=document.id))
    return render_template("purchasing/return_form.html", form=form, products=_purchase_products_payload(), today=today_local())


@bp.route("/purchasing/returns/<int:return_id>")
@requires("purchasing.view")
def return_detail(return_id: int):
    document = db.session.get(PurchaseReturn, return_id)
    if document is None:
        return redirect(url_for("purchasing.returns"))
    return render_template("purchasing/return_detail.html", document=document)


def _can(permission: str) -> bool:
    from app.security import can

    return can(permission)

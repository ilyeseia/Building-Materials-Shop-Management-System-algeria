"""Inventory: stock levels, the movement ledger, adjustments, transfers, counts."""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.extensions import db
from app.forms import AdjustmentForm, CountForm, TransferForm
from app.i18n import format_money, format_quantity_with_unit, localized_name, translate
from app.models import (
    DocumentStatus,
    MovementReason,
    Product,
    StockAdjustment,
    StockAdjustmentLine,
    StockCount,
    StockCountLine,
    StockLevel,
    StockMovement,
    StockTransfer,
    StockTransferLine,
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
from app.services import audit, numbering, reporting, stock
from app.services.errors import BusinessError
from app.services.timeutil import today_local

bp = Blueprint("inventory", __name__)


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------

@bp.route("/inventory")
@requires("inventory.view")
def levels():
    page, per_page = page_args()
    warehouse_id = parse_int(request.args.get("warehouse"))
    search = (request.args.get("q") or "").strip()

    query = (
        select(StockLevel)
        .join(Product, Product.id == StockLevel.product_id)
        .order_by(Product.name)
    )
    if warehouse_id:
        query = query.where(StockLevel.warehouse_id == warehouse_id)
    if search:
        pattern = f"%{search}%"
        query = query.where(Product.name.ilike(pattern) | Product.sku.ilike(pattern))
    result = reporting.paginate(query, page, per_page)

    rows = []
    for level in result["items"]:
        rows.append(
            {
                "product": level.product.name,
                "warehouse": localized_name(level.warehouse),
                "on_hand": format_quantity_with_unit(level.quantity, level.product.stock_unit_code),
                "reserved": format_quantity_with_unit(level.reserved, level.product.stock_unit_code),
                "available": format_quantity_with_unit(level.available, level.product.stock_unit_code),
                "avg_cost": format_money(level.avg_cost),
                "value": format_money(Decimal(level.quantity or 0) * Decimal(level.avg_cost or 0)),
                "_url": url_for("catalog.product_detail", product_id=level.product_id),
            }
        )
    warehouses = db.session.execute(select(Warehouse).order_by(Warehouse.code)).scalars().all()
    return render_template(
        "crud/list.html",
        title_key="nav.inventory",
        rows=rows,
        columns=[
            {"key": "product", "label": "product.title"},
            {"key": "warehouse", "label": "stock.warehouse"},
            {"key": "on_hand", "label": "stock.on_hand"},
            {"key": "reserved", "label": "stock.reserved"},
            {"key": "available", "label": "stock.available"},
            {"key": "avg_cost", "label": "stock.avg_cost"},
            {"key": "value", "label": "stock.value"},
        ],
        result=result,
        search=search,
        searchable=True,
        search_url=url_for("inventory.levels"),
        extra_filters={
            "name": "warehouse",
            "label": "stock.warehouse",
            "selected": warehouse_id or 0,
            "options": [(0, "—")] + [(w.id, localized_name(w)) for w in warehouses],
        },
    )


@bp.route("/inventory/movements")
@requires("inventory.view")
def movements():
    page, per_page = page_args(50)
    query = select(StockMovement).order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
    product_id = parse_int(request.args.get("product"))
    warehouse_id = parse_int(request.args.get("warehouse"))
    reason = (request.args.get("reason") or "").strip()
    date_from = parse_date(request.args.get("from"))
    date_to = parse_date(request.args.get("to"))
    if product_id:
        query = query.where(StockMovement.product_id == product_id)
    if warehouse_id:
        query = query.where(StockMovement.warehouse_id == warehouse_id)
    if reason:
        query = query.where(StockMovement.reason == reason)
    if date_from:
        query = query.where(StockMovement.created_at >= datetime.combine(date_from, time.min))
    if date_to:
        query = query.where(StockMovement.created_at <= datetime.combine(date_to, time.max))

    result = reporting.paginate(query, page, per_page)
    rows = []
    for movement in result["items"]:
        sign = "+" if movement.direction == "in" else "−"
        rows.append(
            {
                "when": movement.created_at.strftime("%d/%m/%Y %H:%M"),
                "product": movement.product.name if movement.product else "",
                "warehouse": localized_name(movement.warehouse) if movement.warehouse else "",
                "reason": translate(f"movement.{movement.reason}"),
                "quantity": f"{sign}{format_quantity_with_unit(movement.quantity, movement.product.stock_unit_code) if movement.product else movement.quantity}",
                "reserved": str(movement.reserved_delta or ""),
                "balance": format_quantity_with_unit(movement.on_hand_after, movement.product.stock_unit_code) if movement.product else "",
                "reference": movement.reference or "",
            }
        )
    warehouses = db.session.execute(select(Warehouse).order_by(Warehouse.code)).scalars().all()
    return render_template(
        "crud/list.html",
        title_key="stock.ledger",
        rows=rows,
        columns=[
            {"key": "when", "label": "common.date"},
            {"key": "product", "label": "product.title"},
            {"key": "warehouse", "label": "stock.warehouse"},
            {"key": "reason", "label": "stock.reason"},
            {"key": "quantity", "label": "common.quantity"},
            {"key": "balance", "label": "stock.balance"},
            {"key": "reference", "label": "common.reference"},
        ],
        result=result,
        search="",
        search_url=url_for("inventory.movements"),
        extra_filters={
            "name": "warehouse",
            "label": "stock.warehouse",
            "selected": warehouse_id or 0,
            "options": [(0, "—")] + [(w.id, localized_name(w)) for w in warehouses],
        },
    )


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------

@bp.route("/inventory/adjustments")
@requires("inventory.view")
def adjustments():
    page, per_page = page_args()
    result = reporting.paginate(
        select(StockAdjustment).order_by(StockAdjustment.adjustment_date.desc()), page, per_page
    )
    rows = [
        {
            "number": adjustment.number,
            "date": adjustment.adjustment_date.strftime("%d/%m/%Y"),
            "warehouse": localized_name(adjustment.warehouse),
            "reason": translate(f"movement.{adjustment.reason}"),
            "status": translate(f"status.{adjustment.status}"),
            "lines": str(len(adjustment.lines)),
            "_url": url_for("inventory.adjustment_detail", adjustment_id=adjustment.id),
        }
        for adjustment in result["items"]
    ]
    return render_template(
        "crud/list.html",
        title_key="stock.adjustments",
        rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "warehouse", "label": "stock.warehouse"},
            {"key": "reason", "label": "stock.reason"},
            {"key": "lines", "label": "common.lines"},
            {"key": "status", "label": "common.status"},
        ],
        result=result,
        search="",
        search_url=url_for("inventory.adjustments"),
        create_url=url_for("inventory.adjustment_new") if _can("inventory.manage") else None,
    )


@bp.route("/inventory/adjustments/new", methods=["GET", "POST"])
@requires("inventory.manage")
@handle_errors
def adjustment_new():
    form = AdjustmentForm()
    fill_choices(form, warehouse_id=choices_for(Warehouse, order_by=Warehouse.code, blank=False))
    if request.method == "POST":
        lines = form_lines(allow_negative=True)
        if not form.warehouse_id.data:
            flash(translate("val.required"), "danger")
        elif not lines:
            flash(translate("sale.no_lines"), "danger")
        else:
            adjustment = StockAdjustment(
                number=numbering.allocate("stock_adjustment"),
                adjustment_date=form.adjustment_date.data or today_local(),
                warehouse_id=form.warehouse_id.data,
                reason=MovementReason.ADJUSTMENT.value,
                justification=form.justification.data or "",
                status="draft",
            )
            db.session.add(adjustment)
            db.session.flush()
            for line in lines:
                db.session.add(
                    StockAdjustmentLine(
                        adjustment_id=adjustment.id,
                        product_id=line["product_id"],
                        quantity=line["quantity"],
                    )
                )
            audit.record("created", "stock_adjustment", adjustment.id, summary=adjustment.number)
            db.session.commit()
            if request.form.get("post_now"):
                return redirect(url_for("inventory.adjustment_post", adjustment_id=adjustment.id))
            flash(translate("common.saved"), "success")
            return redirect(url_for("inventory.adjustment_detail", adjustment_id=adjustment.id))
    return render_template(
        "inventory/adjustment_form.html", form=form,
        products=_product_choices(), today=today_local(),
    )


@bp.route("/inventory/adjustments/<int:adjustment_id>")
@requires("inventory.view")
def adjustment_detail(adjustment_id: int):
    adjustment = db.session.get(StockAdjustment, adjustment_id)
    if adjustment is None:
        return redirect(url_for("inventory.adjustments"))
    return render_template("inventory/adjustment_detail.html", adjustment=adjustment)


@bp.route("/inventory/adjustments/<int:adjustment_id>/post", methods=["POST"])
@requires("inventory.manage")
@handle_errors
def adjustment_post(adjustment_id: int):
    adjustment = db.session.get(StockAdjustment, adjustment_id)
    if adjustment is None:
        return redirect(url_for("inventory.adjustments"))
    if adjustment.status == DocumentStatus.POSTED.value:
        flash(translate("val.bad_status_transition"), "warning")
        return redirect(url_for("inventory.adjustment_detail", adjustment_id=adjustment.id))
    for line in adjustment.lines:
        stock.adjust(
            line.product, adjustment.warehouse, Decimal(line.quantity or 0),
            reason=adjustment.reason,
            justification=adjustment.justification,
            source_type="stock_adjustment", source_id=adjustment.id,
            note=line.note,
        )
    adjustment.status = DocumentStatus.POSTED.value
    adjustment.posted_at = datetime.utcnow()
    audit.record("posted", "stock_adjustment", adjustment.id, summary=adjustment.number)
    db.session.commit()
    flash(translate("stock.posted"), "success")
    return redirect(url_for("inventory.adjustment_detail", adjustment_id=adjustment.id))


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

@bp.route("/inventory/transfers")
@requires("inventory.view")
def transfers():
    page, per_page = page_args()
    result = reporting.paginate(
        select(StockTransfer).order_by(StockTransfer.transfer_date.desc()), page, per_page
    )
    rows = [
        {
            "number": transfer.number,
            "date": transfer.transfer_date.strftime("%d/%m/%Y"),
            "from": localized_name(transfer.source_warehouse),
            "to": localized_name(transfer.destination_warehouse),
            "lines": str(len(transfer.lines)),
            "status": translate(f"status.{transfer.status}"),
            "_url": url_for("inventory.transfer_detail", transfer_id=transfer.id),
        }
        for transfer in result["items"]
    ]
    return render_template(
        "crud/list.html",
        title_key="stock.transfers",
        rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "from", "label": "stock.source"},
            {"key": "to", "label": "stock.destination"},
            {"key": "lines", "label": "common.lines"},
            {"key": "status", "label": "common.status"},
        ],
        result=result,
        search="",
        search_url=url_for("inventory.transfers"),
        create_url=url_for("inventory.transfer_new") if _can("inventory.manage") else None,
    )


@bp.route("/inventory/transfers/new", methods=["GET", "POST"])
@requires("inventory.manage")
@handle_errors
def transfer_new():
    form = TransferForm()
    warehouse_choices = choices_for(Warehouse, order_by=Warehouse.code, blank=False)
    fill_choices(form, source_warehouse_id=warehouse_choices, destination_warehouse_id=list(warehouse_choices))
    if request.method == "POST":
        lines = form_lines()
        if not lines:
            flash(translate("sale.no_lines"), "danger")
        elif form.source_warehouse_id.data == form.destination_warehouse_id.data:
            flash(translate("val.bad_status_transition"), "danger")
        else:
            transfer = StockTransfer(
                number=numbering.allocate("stock_transfer"),
                transfer_date=form.transfer_date.data or today_local(),
                source_warehouse_id=form.source_warehouse_id.data,
                destination_warehouse_id=form.destination_warehouse_id.data,
                status="draft",
                note=form.note.data,
            )
            db.session.add(transfer)
            db.session.flush()
            for line in lines:
                db.session.add(
                    StockTransferLine(
                        transfer_id=transfer.id, product_id=line["product_id"], quantity=line["quantity"]
                    )
                )
            audit.record("created", "stock_transfer", transfer.id, summary=transfer.number)
            db.session.commit()
            if request.form.get("post_now"):
                return redirect(url_for("inventory.transfer_post", transfer_id=transfer.id))
            flash(translate("common.saved"), "success")
            return redirect(url_for("inventory.transfer_detail", transfer_id=transfer.id))
    return render_template("inventory/transfer_form.html", form=form, products=_product_choices(), today=today_local())


@bp.route("/inventory/transfers/<int:transfer_id>")
@requires("inventory.view")
def transfer_detail(transfer_id: int):
    transfer = db.session.get(StockTransfer, transfer_id)
    if transfer is None:
        return redirect(url_for("inventory.transfers"))
    return render_template("inventory/transfer_detail.html", transfer=transfer)


@bp.route("/inventory/transfers/<int:transfer_id>/post", methods=["POST"])
@requires("inventory.manage")
@handle_errors
def transfer_post(transfer_id: int):
    transfer = db.session.get(StockTransfer, transfer_id)
    if transfer is None:
        return redirect(url_for("inventory.transfers"))
    stock.post_transfer(transfer)
    db.session.commit()
    flash(translate("stock.posted"), "success")
    return redirect(url_for("inventory.transfer_detail", transfer_id=transfer.id))


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------

@bp.route("/inventory/counts")
@requires("inventory.view")
def counts():
    page, per_page = page_args()
    result = reporting.paginate(select(StockCount).order_by(StockCount.count_date.desc()), page, per_page)
    rows = [
        {
            "number": count.number,
            "date": count.count_date.strftime("%d/%m/%Y"),
            "warehouse": localized_name(count.warehouse),
            "lines": str(len(count.lines)),
            "status": translate(f"status.{count.status}"),
            "_url": url_for("inventory.count_detail", count_id=count.id),
        }
        for count in result["items"]
    ]
    return render_template(
        "crud/list.html",
        title_key="stock.counts",
        rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "warehouse", "label": "stock.warehouse"},
            {"key": "lines", "label": "common.lines"},
            {"key": "status", "label": "common.status"},
        ],
        result=result,
        search="",
        search_url=url_for("inventory.counts"),
        create_url=url_for("inventory.count_new") if _can("inventory.manage") else None,
    )


@bp.route("/inventory/counts/new", methods=["GET", "POST"])
@requires("inventory.manage")
@handle_errors
def count_new():
    form = CountForm()
    fill_choices(form, warehouse_id=choices_for(Warehouse, order_by=Warehouse.code, blank=False))
    if request.method == "POST":
        lines = form_lines(allow_zero=True)
        if not lines:
            flash(translate("sale.no_lines"), "danger")
        elif not form.warehouse_id.data:
            flash(translate("val.required"), "danger")
        else:
            count = StockCount(
                number=numbering.allocate("stock_count"),
                count_date=form.count_date.data or today_local(),
                warehouse_id=form.warehouse_id.data,
                status="draft",
                note=form.note.data,
            )
            db.session.add(count)
            db.session.flush()
            warehouse = db.session.get(Warehouse, form.warehouse_id.data)
            for line in lines:
                product = db.session.get(Product, line["product_id"])
                level = stock.get_level(product, warehouse) if product and warehouse else None
                db.session.add(
                    StockCountLine(
                        count_id=count.id,
                        product_id=line["product_id"],
                        system_quantity=(level.quantity if level else 0),
                        counted_quantity=line["quantity"],
                    )
                )
            audit.record("created", "stock_count", count.id, summary=count.number)
            db.session.commit()
            if request.form.get("post_now"):
                return redirect(url_for("inventory.count_post", count_id=count.id))
            flash(translate("common.saved"), "success")
            return redirect(url_for("inventory.count_detail", count_id=count.id))
    return render_template("inventory/count_form.html", form=form, products=_product_choices(), today=today_local())


@bp.route("/inventory/counts/<int:count_id>")
@requires("inventory.view")
def count_detail(count_id: int):
    count = db.session.get(StockCount, count_id)
    if count is None:
        return redirect(url_for("inventory.counts"))
    return render_template("inventory/count_detail.html", count=count)


@bp.route("/inventory/counts/<int:count_id>/post", methods=["POST"])
@requires("inventory.manage")
@handle_errors
def count_post(count_id: int):
    count = db.session.get(StockCount, count_id)
    if count is None:
        return redirect(url_for("inventory.counts"))
    stock.post_count(count)
    db.session.commit()
    flash(translate("stock.posted"), "success")
    return redirect(url_for("inventory.count_detail", count_id=count.id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _product_choices():
    products = db.session.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.name)
    ).scalars().all()
    return [
        {
            "id": product.id,
            "name": product.name,
            "unit": product.stock_unit_code,
            "units": [
                {"id": unit.id, "code": unit.code}
                for unit in _allowed_units(product)
            ],
        }
        for product in products
    ]


def _allowed_units(product: Product):
    from app.services import uom

    return uom.allowed_units(product, "sale")


def _can(permission: str) -> bool:
    from app.security import can

    return can(permission)

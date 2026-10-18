"""Weighbridge: weighing tickets (manual) and the device registry."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.extensions import db
from app.forms import WeighbridgeDeviceForm, WeighingTicketForm
from app.i18n import format_quantity_with_unit, localized_name, translate
from app.models import (
    Customer,
    DocumentStatus,
    Product,
    Supplier,
    WeighbridgeDevice,
    WeighingDirection,
    WeighingTicket,
    Warehouse,
)
from app.blueprints.helpers import choices_for, fill_choices, handle_errors, page_args, parse_decimal, parse_int
from app.security import requires
from app.services import reporting, weighing as weighing_service
from app.services.errors import ValidationError
from app.services.timeutil import today_local

bp = Blueprint("weighbridge", __name__)


@bp.route("/weighbridge/tickets")
@requires("weigh.view")
def tickets():
    page, per_page = page_args()
    direction = (request.args.get("direction") or "").strip()
    query = select(WeighingTicket).order_by(WeighingTicket.ticket_date.desc(), WeighingTicket.id.desc())
    if direction in {WeighingDirection.INBOUND.value, WeighingDirection.OUTBOUND.value}:
        query = query.where(WeighingTicket.direction == direction)
    result = reporting.paginate(query, page, per_page)
    rows = [
        {
            "number": ticket.number,
            "date": ticket.ticket_date.strftime("%d/%m/%Y"),
            "direction": translate(f"weigh.{ticket.direction}"),
            "plate": ticket.plate or "",
            "product": ticket.product.name if ticket.product else "",
            "net": format_quantity_with_unit(ticket.net_weight, "kg"),
            "status": translate(f"status.{ticket.status}"),
            "_url": url_for("weighbridge.ticket_detail", ticket_id=ticket.id),
        }
        for ticket in result["items"]
    ]
    return render_template(
        "crud/list.html", title_key="nav.weighbridge", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "direction", "label": "weigh.direction"},
            {"key": "plate", "label": "weigh.plate"},
            {"key": "product", "label": "product.title"},
            {"key": "net", "label": "weigh.net"},
            {"key": "status", "label": "common.status"},
        ],
        result=result, search="", search_url=url_for("weighbridge.tickets"),
        create_url=url_for("weighbridge.ticket_new") if _can("weigh.manage") else None,
        extra_filters={
            "name": "direction", "label": "weigh.direction", "selected": direction,
            "options": [(None, "—"), ("inbound", translate("weigh.inbound")), ("outbound", translate("weigh.outbound"))],
        },
    )


def _ticket_form_choices(form: WeighingTicketForm) -> None:
    fill_choices(
        form,
        product_id=choices_for(Product, order_by=Product.name),
        customer_id=choices_for(Customer, order_by=Customer.name),
        supplier_id=choices_for(Supplier, order_by=Supplier.name),
        warehouse_id=choices_for(Warehouse, order_by=Warehouse.code),
        device_id=choices_for(WeighbridgeDevice, order_by=WeighbridgeDevice.code),
    )


@bp.route("/weighbridge/tickets/new", methods=["GET", "POST"])
@requires("weigh.manage")
@handle_errors
def ticket_new():
    form = WeighingTicketForm()
    _ticket_form_choices(form)
    if request.method == "POST":
        product = db.session.get(Product, form.product_id.data) if form.product_id.data else None
        customer = db.session.get(Customer, form.customer_id.data) if form.customer_id.data else None
        supplier = db.session.get(Supplier, form.supplier_id.data) if form.supplier_id.data else None
        warehouse = db.session.get(Warehouse, form.warehouse_id.data) if form.warehouse_id.data else None
        device = db.session.get(WeighbridgeDevice, form.device_id.data) if form.device_id.data else None
        ticket = weighing_service.create_ticket(
            direction=form.direction.data,
            product=product,
            warehouse=warehouse,
            plate=form.plate.data,
            driver_name=form.driver_name.data,
            customer=customer,
            supplier=supplier,
            ticket_date=form.ticket_date.data or today_local(),
            device=device,
            gross_weight=form.gross_weight.data,
            tare_weight=form.tare_weight.data,
            expected_quantity=form.expected_quantity.data,
            notes=form.notes.data,
        )
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("weighbridge.ticket_detail", ticket_id=ticket.id))
    return render_template("weighbridge/ticket_form.html", form=form, today=today_local())


@bp.route("/weighbridge/tickets/<int:ticket_id>")
@requires("weigh.view")
def ticket_detail(ticket_id: int):
    ticket = db.session.get(WeighingTicket, ticket_id)
    if ticket is None:
        return redirect(url_for("weighbridge.tickets"))
    return render_template(
        "weighbridge/ticket_detail.html", ticket=ticket,
        net_in_stock=_net_in_stock(ticket),
    )


def _net_in_stock(ticket: WeighingTicket):
    if ticket.net_weight is None or ticket.product is None:
        return None
    try:
        quantity, is_estimate = weighing_service.net_in_stock_unit(ticket)
        return {"quantity": quantity, "unit": ticket.product.stock_unit_code, "estimate": is_estimate}
    except ValidationError as error:
        return {"error": error.message()}


@bp.route("/weighbridge/tickets/<int:ticket_id>/weigh", methods=["POST"])
@requires("weigh.manage")
@handle_errors
def ticket_weigh(ticket_id: int):
    ticket = db.session.get(WeighingTicket, ticket_id)
    if ticket is None:
        return redirect(url_for("weighbridge.tickets"))
    gross = request.form.get("gross_weight")
    tare = request.form.get("tare_weight")
    reason = request.form.get("reason") or None
    if gross not in (None, ""):
        weighing_service.set_gross(ticket, gross, reason=reason)
    if tare not in (None, ""):
        weighing_service.set_tare(ticket, tare, reason=reason)
    db.session.commit()
    flash(translate("common.saved"), "success")
    return redirect(url_for("weighbridge.ticket_detail", ticket_id=ticket.id))


@bp.route("/weighbridge/tickets/<int:ticket_id>/approve", methods=["POST"])
@requires("weigh.manage")
@handle_errors
def ticket_approve(ticket_id: int):
    ticket = db.session.get(WeighingTicket, ticket_id)
    if ticket is None:
        return redirect(url_for("weighbridge.tickets"))
    note = request.form.get("discrepancy_note")
    if note:
        ticket.discrepancy_note = note
    weighing_service.approve_ticket(ticket, post_stock=bool(request.form.get("post_now")))
    db.session.commit()
    flash(translate("weigh.approved"), "success")
    return redirect(url_for("weighbridge.ticket_detail", ticket_id=ticket.id))


@bp.route("/weighbridge/tickets/<int:ticket_id>/post", methods=["POST"])
@requires("weigh.manage")
@handle_errors
def ticket_post(ticket_id: int):
    ticket = db.session.get(WeighingTicket, ticket_id)
    if ticket is None:
        return redirect(url_for("weighbridge.tickets"))
    weighing_service.post_to_stock(ticket)
    db.session.commit()
    flash(translate("weigh.posted"), "success")
    return redirect(url_for("weighbridge.ticket_detail", ticket_id=ticket.id))


@bp.route("/weighbridge/tickets/<int:ticket_id>/correct", methods=["POST"])
@requires("weigh.manage")
@handle_errors
def ticket_correct(ticket_id: int):
    ticket = db.session.get(WeighingTicket, ticket_id)
    if ticket is None:
        return redirect(url_for("weighbridge.tickets"))
    weighing_service.correct_measurement(
        ticket,
        request.form.get("field_name") or "gross_weight",
        request.form.get("new_value"),
        reason=request.form.get("reason") or "",
    )
    db.session.commit()
    flash(translate("common.saved"), "success")
    return redirect(url_for("weighbridge.ticket_detail", ticket_id=ticket.id))


@bp.route("/weighbridge/tickets/<int:ticket_id>/cancel", methods=["POST"])
@requires("weigh.manage")
@handle_errors
def ticket_cancel(ticket_id: int):
    ticket = db.session.get(WeighingTicket, ticket_id)
    if ticket is None:
        return redirect(url_for("weighbridge.tickets"))
    weighing_service.cancel_ticket(ticket, reason=request.form.get("reason") or "")
    db.session.commit()
    flash(translate("common.saved"), "success")
    return redirect(url_for("weighbridge.ticket_detail", ticket_id=ticket.id))


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

@bp.route("/weighbridge/devices", methods=["GET", "POST"])
@requires("weigh.view")
def devices():
    form = WeighbridgeDeviceForm()
    if _can("weigh.manage") and form.validate_on_submit():
        if form.protocol.data != "manual" and form.protocol.data not in weighing_service.implemented_protocols():
            flash(translate("weigh.protocol_unimplemented"), "warning")
        device = WeighbridgeDevice(
            code=form.code.data, name=form.name.data, protocol=form.protocol.data,
            serial_number=form.serial_number.data, connection_config=form.connection_config.data,
            capacity_kg=form.capacity_kg.data, division_kg=form.division_kg.data,
            calibration_reference=form.calibration_reference.data,
            calibration_valid_until=form.calibration_valid_until.data, notes=form.notes.data,
        )
        db.session.add(device)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("weighbridge.devices"))
    items = db.session.execute(select(WeighbridgeDevice).order_by(WeighbridgeDevice.code)).scalars().all()
    return render_template(
        "weighbridge/devices.html", form=form, items=items,
        implemented=weighing_service.implemented_protocols(),
    )


@bp.route("/weighbridge/devices/<int:device_id>/edit", methods=["GET", "POST"])
@requires("weigh.manage")
@handle_errors
def device_edit(device_id: int):
    device = db.session.get(WeighbridgeDevice, device_id)
    if device is None:
        return redirect(url_for("weighbridge.devices"))
    form = WeighbridgeDeviceForm(obj=device)
    if form.validate_on_submit():
        device.code = form.code.data
        device.name = form.name.data
        device.protocol = form.protocol.data
        device.serial_number = form.serial_number.data
        device.connection_config = form.connection_config.data
        device.capacity_kg = form.capacity_kg.data
        device.division_kg = form.division_kg.data
        device.calibration_reference = form.calibration_reference.data
        device.calibration_valid_until = form.calibration_valid_until.data
        device.notes = form.notes.data
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("weighbridge.devices"))
    return render_template("crud/form.html", form=form, title_key="weigh.device_edit",
                           cancel_url=url_for("weighbridge.devices"))


def _can(permission: str) -> bool:
    from app.security import can

    return can(permission)

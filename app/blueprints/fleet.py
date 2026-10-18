"""Fleet: vehicles, drivers, carriers, trips and trip expenses."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.extensions import db
from app.forms import CarrierForm, DriverForm, TripExpenseForm, TripForm, VehicleForm
from app.i18n import format_money, localized_name, translate
from app.models import Carrier, Customer, Driver, Trip, TripExpense, UnitOfMeasure, Vehicle
from app.blueprints.helpers import choices_for, fill_choices, handle_errors, page_args, parse_int
from app.security import requires
from app.services import reporting, transport
from app.services.errors import ValidationError
from app.services.timeutil import today_local

bp = Blueprint("fleet", __name__)


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------

@bp.route("/fleet/trips")
@requires("fleet.view")
def trips():
    page, per_page = page_args()
    result = reporting.paginate(
        select(Trip).order_by(Trip.trip_date.desc(), Trip.id.desc()), page, per_page
    )
    rows = []
    for trip in result["items"]:
        summary = transport.trip_summary(trip)
        rows.append(
            {
                "number": trip.number,
                "date": trip.trip_date.strftime("%d/%m/%Y"),
                "vehicle": trip.vehicle.plate if trip.vehicle else (trip.carrier.name if trip.carrier else ""),
                "destination": trip.destination or "",
                "fee": format_money(summary["revenue"]),
                "cost": format_money(summary["total_cost"]),
                "margin": format_money(summary["margin"]),
                "status": translate(f"status.{trip.status}"),
                "_url": url_for("fleet.trip_detail", trip_id=trip.id),
            }
        )
    return render_template(
        "crud/list.html", title_key="nav.fleet", rows=rows,
        columns=[
            {"key": "number", "label": "common.number"},
            {"key": "date", "label": "common.date"},
            {"key": "vehicle", "label": "fleet.vehicle"},
            {"key": "destination", "label": "fleet.destination"},
            {"key": "fee", "label": "fleet.transport_fee"},
            {"key": "cost", "label": "fleet.total_cost"},
            {"key": "margin", "label": "fleet.margin"},
            {"key": "status", "label": "common.status"},
        ],
        result=result, search="", search_url=url_for("fleet.trips"),
        create_url=url_for("fleet.trip_new") if _can("fleet.manage") else None,
    )


def _trip_choices(form: TripForm) -> None:
    fill_choices(
        form,
        vehicle_id=choices_for(Vehicle, order_by=Vehicle.plate),
        driver_id=choices_for(Driver, order_by=Driver.name),
        carrier_id=choices_for(Carrier, order_by=Carrier.name),
        customer_id=choices_for(Customer, order_by=Customer.name),
    )


@bp.route("/fleet/trips/new", methods=["GET", "POST"])
@requires("fleet.manage")
@handle_errors
def trip_new():
    form = TripForm()
    _trip_choices(form)
    if request.method == "POST":
        vehicle = db.session.get(Vehicle, form.vehicle_id.data) if form.vehicle_id.data else None
        driver = db.session.get(Driver, form.driver_id.data) if form.driver_id.data else None
        carrier = db.session.get(Carrier, form.carrier_id.data) if form.carrier_id.data else None
        customer = db.session.get(Customer, form.customer_id.data) if form.customer_id.data else None
        trip = transport.create_trip(
            trip_date=form.trip_date.data or today_local(),
            planned_date=form.planned_date.data,
            vehicle=vehicle, driver=driver, carrier=carrier, customer=customer,
            origin=form.origin.data, destination=form.destination.data,
            transport_fee=form.transport_fee.data, carrier_cost=form.carrier_cost.data,
            notes=form.notes.data,
        )
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("fleet.trip_detail", trip_id=trip.id))
    return render_template("fleet/trip_form.html", form=form, today=today_local())


@bp.route("/fleet/trips/<int:trip_id>")
@requires("fleet.view")
def trip_detail(trip_id: int):
    trip = db.session.get(Trip, trip_id)
    if trip is None:
        return redirect(url_for("fleet.trips"))
    expense_form = TripExpenseForm()
    return render_template(
        "fleet/trip_detail.html", trip=trip, expense_form=expense_form,
        summary=transport.trip_summary(trip), deliveries=list(trip.deliveries),
    )


@bp.route("/fleet/trips/<int:trip_id>/expenses", methods=["POST"])
@requires("fleet.manage")
@handle_errors
def trip_expense_add(trip_id: int):
    trip = db.session.get(Trip, trip_id)
    if trip is None:
        return redirect(url_for("fleet.trips"))
    form = TripExpenseForm()
    if form.validate_on_submit():
        transport.add_expense(
            trip,
            expense_type=form.expense_type.data,
            amount=form.amount.data,
            expense_date=form.expense_date.data or trip.trip_date,
            paid_by=form.paid_by.data,
            note=form.note.data,
        )
        db.session.commit()
        flash(translate("common.saved"), "success")
    else:
        flash(translate("val.invalid_number"), "danger")
    return redirect(url_for("fleet.trip_detail", trip_id=trip.id))


@bp.route("/fleet/trips/<int:trip_id>/status", methods=["POST"])
@requires("fleet.manage", "fleet.trips.status", any_of=True)
@handle_errors
def trip_status(trip_id: int):
    trip = db.session.get(Trip, trip_id)
    if trip is None:
        return redirect(url_for("fleet.trips"))
    action = request.form.get("action")
    if action == "start":
        transport.start_trip(trip, odometer_start=request.form.get("odometer_start"))
    elif action == "complete":
        transport.complete_trip(trip, odometer_end=request.form.get("odometer_end"))
    elif action == "fail":
        transport.fail_trip(trip, reason=request.form.get("reason") or "")
    else:
        flash(translate("val.required"), "danger")
    db.session.commit()
    return redirect(url_for("fleet.trip_detail", trip_id=trip.id))


# ---------------------------------------------------------------------------
# Vehicles / drivers / carriers
# ---------------------------------------------------------------------------

@bp.route("/fleet/vehicles", methods=["GET", "POST"])
@requires("fleet.view")
def vehicles():
    form = VehicleForm()
    fill_choices(
        form,
        capacity_unit_id=choices_for(UnitOfMeasure, order_by=UnitOfMeasure.code),
        default_driver_id=choices_for(Driver, order_by=Driver.name),
    )
    if _can("fleet.manage") and form.validate_on_submit():
        vehicle = Vehicle(
            plate=form.plate.data, label=form.label.data, vehicle_type=form.vehicle_type.data,
            capacity_quantity=form.capacity_quantity.data,
            capacity_unit_id=form.capacity_unit_id.data or None,
            tare_weight_kg=form.tare_weight_kg.data, odometer_km=form.odometer_km.data,
            insurance_expiry=form.insurance_expiry.data, inspection_expiry=form.inspection_expiry.data,
            default_driver_id=form.default_driver_id.data or None, notes=form.notes.data,
        )
        db.session.add(vehicle)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("fleet.vehicles"))
    items = db.session.execute(select(Vehicle).order_by(Vehicle.plate)).scalars().all()
    return render_template("fleet/vehicles.html", form=form, items=items)


@bp.route("/fleet/drivers", methods=["GET", "POST"])
@requires("fleet.view")
def drivers():
    form = DriverForm()
    if _can("fleet.manage") and form.validate_on_submit():
        driver = Driver(
            name=form.name.data, phone=form.phone.data, licence_no=form.licence_no.data,
            licence_expiry=form.licence_expiry.data, licence_category=form.licence_category.data,
            notes=form.notes.data,
        )
        db.session.add(driver)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("fleet.drivers"))
    items = db.session.execute(select(Driver).order_by(Driver.name)).scalars().all()
    return render_template("fleet/drivers.html", form=form, items=items)


@bp.route("/fleet/carriers", methods=["GET", "POST"])
@requires("fleet.view")
def carriers():
    form = CarrierForm()
    if _can("fleet.manage") and form.validate_on_submit():
        carrier = Carrier(
            name=form.name.data, contact_person=form.contact_person.data, phone=form.phone.data,
            tax_id=form.tax_id.data, default_rate=form.default_rate.data, notes=form.notes.data,
        )
        db.session.add(carrier)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("fleet.carriers"))
    items = db.session.execute(select(Carrier).order_by(Carrier.name)).scalars().all()
    return render_template("fleet/carriers.html", form=form, items=items)


def _can(permission: str) -> bool:
    from app.security import can

    return can(permission)

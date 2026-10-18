"""Fleet and transport service: trips, expenses and honest trip costing.

The cost separation defined in the model is enforced here:

* the **transport fee** charged to the customer,
* the **carrier cost** paid to an external haulier,
* the **operating expenses** of the trip (fuel, tolls, repairs, allowances…).

A trip's margin is therefore ``transport_fee − carrier_cost − expenses``.  A
customer transport charge is never treated as pure profit, and a trip with no fee
recorded shows a negative margin rather than a flattering zero.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models import (
    Carrier,
    Customer,
    Driver,
    ExpenseType,
    OwnershipType,
    Trip,
    TripExpense,
    TripStatus,
    Vehicle,
)
from app.services import audit, numbering, uom
from app.services.errors import InvalidTransition, ValidationError
from app.services.timeutil import today_local, utcnow

MONEY_PLACES = Decimal("0.01")


def create_trip(
    *,
    trip_date: date | None = None,
    planned_date: date | None = None,
    vehicle: Vehicle | None = None,
    driver: Driver | None = None,
    carrier: Carrier | None = None,
    customer: Customer | None = None,
    origin: str | None = None,
    destination: str | None = None,
    transport_fee=None,
    carrier_cost=None,
    ownership: str | None = None,
    notes: str | None = None,
    start: bool = False,
    actor=None,
) -> Trip:
    if carrier is not None and ownership is None:
        ownership = OwnershipType.EXTERNAL.value
    ownership = ownership or OwnershipType.OWNED.value
    if ownership not in {o.value for o in OwnershipType}:
        raise ValidationError("val.required")
    if carrier is None and vehicle is None and ownership == OwnershipType.OWNED.value:
        # A company trip must name the vehicle it uses; otherwise costing and
        # capacity checks have nothing to attach to.
        raise ValidationError("fleet.vehicle_required")

    fee = uom.parse_money(transport_fee) if transport_fee not in (None, "") else Decimal("0")
    if fee < 0:
        raise ValidationError("val.must_not_be_negative")

    if carrier_cost in (None, "") and carrier is not None and carrier.default_rate is not None:
        cost = Decimal(carrier.default_rate)
    elif carrier_cost in (None, ""):
        cost = Decimal("0")
    else:
        cost = uom.parse_money(carrier_cost)
    if cost < 0:
        raise ValidationError("val.must_not_be_negative")

    trip = Trip(
        number=numbering.allocate("trip"),
        trip_date=trip_date or today_local(),
        planned_date=planned_date,
        vehicle_id=vehicle.id if vehicle else None,
        driver_id=driver.id if driver else (vehicle.default_driver_id if vehicle else None),
        carrier_id=carrier.id if carrier else None,
        customer_id=customer.id if customer else None,
        ownership=ownership,
        origin=origin,
        destination=destination,
        transport_fee=fee.quantize(MONEY_PLACES),
        carrier_cost=cost.quantize(MONEY_PLACES),
        status=TripStatus.DRAFT.value,
        notes=notes,
        created_by_id=getattr(actor, "id", None),
    )
    db.session.add(trip)
    db.session.flush()
    audit.record("created", "trip", trip.id, summary=trip.number)
    if start:
        start_trip(trip, actor=actor)
    return trip


def start_trip(trip: Trip, *, odometer_start=None, actor=None) -> Trip:
    if trip.status not in {TripStatus.DRAFT.value, TripStatus.OPEN.value}:
        raise InvalidTransition()
    trip.status = TripStatus.IN_TRANSIT.value
    trip.departed_at = utcnow()
    if odometer_start not in (None, ""):
        trip.odometer_start = uom.parse_quantity(odometer_start)
    audit.record("started", "trip", trip.id, summary=trip.number)
    return trip


def complete_trip(trip: Trip, *, odometer_end=None, actor=None) -> Trip:
    if trip.status not in {TripStatus.IN_TRANSIT.value, TripStatus.OPEN.value}:
        raise InvalidTransition()
    if odometer_end not in (None, ""):
        reading = uom.parse_quantity(odometer_end)
        if trip.odometer_start is not None and reading < Decimal(trip.odometer_start):
            raise ValidationError("fleet.odometer_backwards")
        trip.odometer_end = reading
    trip.status = TripStatus.COMPLETED.value
    trip.arrived_at = utcnow()
    if trip.vehicle is not None and trip.odometer_end is not None:
        trip.vehicle.odometer_km = trip.odometer_end
    audit.record("completed", "trip", trip.id, summary=f"{trip.number} margin={trip.margin}")
    return trip


def fail_trip(trip: Trip, reason: str, *, actor=None) -> Trip:
    if trip.status == TripStatus.COMPLETED.value:
        raise InvalidTransition()
    if not reason:
        raise ValidationError("val.required")
    trip.status = TripStatus.FAILED.value
    trip.failure_reason = reason
    audit.record("failed", "trip", trip.id, summary=f"{trip.number}: {reason}")
    return trip


def add_expense(
    trip: Trip,
    *,
    expense_type: str,
    amount,
    expense_date: date | None = None,
    paid_by: str | None = None,
    note: str | None = None,
    actor=None,
) -> TripExpense:
    if expense_type not in {e.value for e in ExpenseType}:
        raise ValidationError("val.required")
    value = uom.parse_money(amount)
    if value <= 0:
        raise ValidationError("val.must_be_positive")

    expense = TripExpense(
        trip_id=trip.id,
        expense_date=expense_date or trip.trip_date or today_local(),
        expense_type=expense_type,
        amount=value,
        paid_by=paid_by,
        note=note,
        created_by_id=getattr(actor, "id", None),
    )
    db.session.add(expense)
    db.session.flush()
    audit.record("recorded", "trip_expense", expense.id, summary=f"{trip.number}: {expense_type} {value}")
    return expense


def remove_expense(expense: TripExpense, *, reason: str = "", actor=None) -> None:
    """Remove a trip expense.

    Expenses are line items, not documents, so removal is allowed — but it is
    always audited so the change is not invisible.
    """
    audit.record(
        "removed", "trip_expense", expense.id,
        summary=f"{expense.expense_type} {expense.amount} ({reason})" if reason else str(expense.amount),
    )
    db.session.delete(expense)


def assign_delivery(trip: Trip, delivery, *, actor=None) -> Trip:
    """Attach a delivery note to the trip that carried it."""
    delivery.trip_id = trip.id
    if trip.vehicle_id and not delivery.vehicle_id:
        delivery.vehicle_id = trip.vehicle_id
    if trip.driver_id and not delivery.driver_id:
        delivery.driver_id = trip.driver_id
    if trip.carrier_id and not delivery.carrier_id:
        delivery.carrier_id = trip.carrier_id
    audit.record("linked", "trip", trip.id, summary=f"delivery {getattr(delivery, 'number', delivery.id)}")
    return trip


def trip_summary(trip: Trip) -> dict:
    """Revenue, attributable costs and margin for one trip."""
    revenue = Decimal(trip.transport_fee or 0)
    carrier_cost = Decimal(trip.carrier_cost or 0)
    expenses = Decimal(trip.expenses_total or 0)
    total_cost = (carrier_cost + expenses).quantize(MONEY_PLACES)
    margin = (revenue - total_cost).quantize(MONEY_PLACES)
    margin_percent = (
        (margin / revenue * Decimal("100")).quantize(Decimal("0.01"))
        if revenue > 0
        else None
    )
    return {
        "revenue": revenue.quantize(MONEY_PLACES),
        "carrier_cost": carrier_cost.quantize(MONEY_PLACES),
        "expenses": expenses.quantize(MONEY_PLACES),
        "total_cost": total_cost,
        "margin": margin,
        "margin_percent": margin_percent,
        "distance_km": trip.distance_km,
        "cost_per_km": (total_cost / trip.distance_km).quantize(MONEY_PLACES)
        if trip.distance_km and trip.distance_km > 0
        else None,
    }

"""Fleet and transport: trip lifecycle, expenses and *separated* costing.

The point of these tests is that transport money is never confused with goods
money: a delivery fee is revenue for the trip, and the trip's own costs are
subtracted from it, not from the margin on the sand.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Carrier,
    Driver,
    ExpenseType,
    OwnershipType,
    Trip,
    TripStatus,
    Vehicle,
)
from app.services import transport
from app.services.errors import InvalidTransition, ValidationError

from tests.conftest import days_ago, today


@pytest.fixture()
def vehicle(app) -> Vehicle:
    row = Vehicle(plate="19-12345-114", label="Camion 1", ownership=OwnershipType.OWNED.value, capacity_quantity=Decimal("10000"))
    db.session.add(row)
    db.session.flush()
    return row


@pytest.fixture()
def driver(app) -> Driver:
    row = Driver(name="Karim B.", phone="0661 22 33 44", licence_no="19-ABC-001")
    db.session.add(row)
    db.session.flush()
    return row


@pytest.fixture()
def carrier(app) -> Carrier:
    row = Carrier(name="Transport Belkacem", default_rate=Decimal("8000"))
    db.session.add(row)
    db.session.flush()
    return row


# ---------------------------------------------------------------------------
# Creation rules
# ---------------------------------------------------------------------------

def test_an_owned_trip_needs_a_vehicle():
    with pytest.raises(ValidationError) as error:
        transport.create_trip(origin="Sétif", destination="Béjaïa")
    assert error.value.message_key == "fleet.vehicle_required"


def test_a_carrier_trip_implies_external_ownership(carrier):
    trip = transport.create_trip(carrier=carrier, origin="Sétif", destination="Alger")

    assert trip.ownership == OwnershipType.EXTERNAL.value
    assert trip.carrier_id == carrier.id


def test_the_carrier_default_rate_is_used_when_no_cost_is_given(carrier):
    trip = transport.create_trip(carrier=carrier, origin="Sétif", destination="Alger")
    assert trip.carrier_cost == Decimal("8000.00")


def test_a_negative_fee_is_refused(vehicle):
    with pytest.raises(ValidationError):
        transport.create_trip(vehicle=vehicle, transport_fee=Decimal("-1"))


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_trip_goes_draft_open_in_transit_completed(vehicle, driver):
    trip = transport.create_trip(vehicle=vehicle, driver=driver, origin="Sétif", destination="Constantine")
    assert trip.status == TripStatus.DRAFT.value

    transport.start_trip(trip, odometer_start=Decimal("100000"))
    assert trip.status == TripStatus.IN_TRANSIT.value
    assert trip.departed_at is not None

    transport.complete_trip(trip, odometer_end=Decimal("100240"))
    assert trip.status == TripStatus.COMPLETED.value
    assert trip.distance_km == Decimal("240")


def test_a_trip_cannot_be_completed_before_it_starts(vehicle):
    trip = transport.create_trip(vehicle=vehicle)
    with pytest.raises(InvalidTransition):
        transport.complete_trip(trip, odometer_end=Decimal("10"))


def test_the_odometer_cannot_run_backwards(vehicle):
    trip = transport.create_trip(vehicle=vehicle)
    transport.start_trip(trip, odometer_start=Decimal("100000"))

    with pytest.raises(ValidationError) as error:
        transport.complete_trip(trip, odometer_end=Decimal("99000"))
    assert error.value.message_key == "fleet.odometer_backwards"


def test_a_failed_trip_records_why(vehicle):
    trip = transport.create_trip(vehicle=vehicle)
    transport.start_trip(trip)
    transport.fail_trip(trip, "panne moteur")

    assert trip.status == TripStatus.FAILED.value
    assert trip.failure_reason == "panne moteur"


# ---------------------------------------------------------------------------
# Costing
# ---------------------------------------------------------------------------

def test_trip_margin_subtracts_carrier_cost_and_expenses(vehicle, driver):
    trip = transport.create_trip(
        vehicle=vehicle, driver=driver,
        transport_fee=Decimal("30000"),
        origin="Sétif", destination="Alger",
    )
    transport.add_expense(trip, expense_type=ExpenseType.FUEL.value, amount=Decimal("12000"))
    transport.add_expense(trip, expense_type=ExpenseType.TOLL.value, amount=Decimal("1500"))
    transport.add_expense(trip, expense_type=ExpenseType.DRIVER_ALLOWANCE.value, amount=Decimal("2500"))

    summary = transport.trip_summary(trip)

    assert summary["revenue"] == Decimal("30000.00")
    assert summary["expenses"] == Decimal("16000.00")
    assert summary["total_cost"] == Decimal("16000.00")
    assert summary["margin"] == Decimal("14000.00")
    assert summary["margin_percent"] == Decimal("46.67")


def test_a_trip_with_no_fee_shows_a_loss_not_a_zero(vehicle):
    """A hidden subsidy is a loss; the report must not flatter it away."""
    trip = transport.create_trip(vehicle=vehicle)
    transport.add_expense(trip, expense_type=ExpenseType.FUEL.value, amount=Decimal("9000"))

    summary = transport.trip_summary(trip)

    assert summary["revenue"] == Decimal("0.00")
    assert summary["margin"] == Decimal("-9000.00")
    assert summary["margin_percent"] is None     # undefined, not "0 %"


def test_carrier_cost_is_counted_separately_from_own_expenses(carrier):
    trip = transport.create_trip(
        carrier=carrier, transport_fee=Decimal("25000"), carrier_cost=Decimal("18000"),
        origin="Sétif", destination="Skikda",
    )
    transport.add_expense(trip, expense_type=ExpenseType.TOLL.value, amount=Decimal("500"))

    summary = transport.trip_summary(trip)

    assert summary["carrier_cost"] == Decimal("18000.00")
    assert summary["expenses"] == Decimal("500.00")
    assert summary["total_cost"] == Decimal("18500.00")
    assert summary["margin"] == Decimal("6500.00")


def test_cost_per_km_is_derived_when_the_distance_is_known(vehicle):
    trip = transport.create_trip(vehicle=vehicle, transport_fee=Decimal("20000"))
    transport.start_trip(trip, odometer_start=Decimal("0"))
    transport.complete_trip(trip, odometer_end=Decimal("200"))
    transport.add_expense(trip, expense_type=ExpenseType.FUEL.value, amount=Decimal("10000"))

    summary = transport.trip_summary(trip)

    assert summary["distance_km"] == Decimal("200")
    assert summary["cost_per_km"] == Decimal("50.00")


def test_an_expense_can_be_removed_with_a_reason(vehicle):
    trip = transport.create_trip(vehicle=vehicle)
    expense = transport.add_expense(trip, expense_type=ExpenseType.REPAIR.value, amount=Decimal("4000"))
    assert trip.expenses_total == Decimal("4000.00")

    transport.remove_expense(expense, reason="erreur de saisie")

    assert trip.expenses_total == Decimal("0.00")


def test_a_negative_expense_is_refused(vehicle):
    trip = transport.create_trip(vehicle=vehicle)
    with pytest.raises(ValidationError):
        transport.add_expense(trip, expense_type=ExpenseType.FUEL.value, amount=Decimal("-100"))


def test_vehicle_plans_show_documents_expiring_within_the_horizon(app):
    from app.models import Vehicle as VehicleModel

    soon = VehicleModel(
        plate="19-99999-114", label="Camion 2",
        ownership=OwnershipType.OWNED.value,
        insurance_expiry=today() + timedelta(days=10),
        inspection_expiry=today() + timedelta(days=400),
    )
    db.session.add(soon)
    db.session.flush()

    expiring = soon.documents_expiring

    assert "insurance_expiry" in expiring
    assert "inspection_expiry" not in expiring

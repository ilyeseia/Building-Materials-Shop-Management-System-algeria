"""Fleet and transport: vehicles, drivers, carriers, trips and trip expenses.

Cost separation matters here and is reflected in the model:

1. the material sale price (on the sales documents),
2. the transport charged to the customer (``Trip.transport_fee``),
3. the operating cost of a company vehicle and the fee of an external carrier
   (``Trip.carrier_cost`` plus the rows of ``TripExpense``),
4. other trip expenses.

Profit is therefore computed from recorded revenue minus *attributable* costs —
a customer transport charge is never treated as pure profit.
"""
from __future__ import annotations

from decimal import Decimal

from app.extensions import db
from app.models.base import (
    MONEY,
    QUANTITY,
    ActorMixin,
    ExpenseType,
    OwnershipType,
    SoftDeleteMixin,
    TimestampMixin,
    TripStatus,
)


class Vehicle(TimestampMixin, ActorMixin, SoftDeleteMixin, db.Model):
    __tablename__ = "vehicles"

    id = db.Column(db.Integer, primary_key=True)
    plate = db.Column(db.String(30), unique=True, nullable=False, index=True)
    label = db.Column(db.String(80))
    vehicle_type = db.Column(db.String(60))
    ownership = db.Column(
        db.String(12), nullable=False, default=OwnershipType.OWNED.value
    )
    carrier_id = db.Column(db.Integer, db.ForeignKey("carriers.id"))
    capacity_quantity = db.Column(QUANTITY)
    capacity_unit_id = db.Column(db.Integer, db.ForeignKey("units_of_measure.id"))
    tare_weight_kg = db.Column(QUANTITY)
    odometer_km = db.Column(QUANTITY)
    insurance_expiry = db.Column(db.Date)
    inspection_expiry = db.Column(db.Date)
    default_driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"))
    status = db.Column(db.String(20), nullable=False, default="active")
    notes = db.Column(db.Text)

    carrier = db.relationship("Carrier", lazy="joined")
    capacity_unit = db.relationship("UnitOfMeasure", lazy="joined")
    default_driver = db.relationship("Driver", lazy="joined")

    @property
    def documents_expiring(self) -> list[str]:
        """Names of documents expiring within 30 days (or already expired)."""
        from app.services.timeutil import add_days, today_local

        today = today_local()
        horizon = add_days(today, 30)
        expiring: list[str] = []
        for field in ("insurance_expiry", "inspection_expiry"):
            value = getattr(self, field)
            if value and value <= horizon:
                expiring.append(field)
        return expiring

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Vehicle {self.plate}>"


class Driver(TimestampMixin, ActorMixin, SoftDeleteMixin, db.Model):
    __tablename__ = "drivers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    phone = db.Column(db.String(40))
    licence_no = db.Column(db.String(60))
    licence_expiry = db.Column(db.Date)
    licence_category = db.Column(db.String(20))
    #: Optional link to a login account (a driver who records their own trips).
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    status = db.Column(db.String(20), nullable=False, default="active")
    notes = db.Column(db.Text)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Driver {self.name}>"


class Carrier(TimestampMixin, ActorMixin, SoftDeleteMixin, db.Model):
    """External transport company."""

    __tablename__ = "carriers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), unique=True, nullable=False, index=True)
    contact_person = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    tax_id = db.Column(db.String(40))
    default_rate = db.Column(MONEY)
    status = db.Column(db.String(20), nullable=False, default="active")
    notes = db.Column(db.Text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Carrier {self.name}>"


class Trip(TimestampMixin, ActorMixin, db.Model):
    __tablename__ = "trips"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, index=True)
    trip_date = db.Column(db.Date, nullable=False, index=True)
    planned_date = db.Column(db.Date)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"))
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"))
    carrier_id = db.Column(db.Integer, db.ForeignKey("carriers.id"))
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    ownership = db.Column(
        db.String(12), nullable=False, default=OwnershipType.OWNED.value
    )
    origin = db.Column(db.String(160))
    destination = db.Column(db.String(160))
    #: Amount charged to the customer for this transport.
    transport_fee = db.Column(MONEY, nullable=False, default=0)
    #: Amount payable to an external carrier (0 for a company vehicle).
    carrier_cost = db.Column(MONEY, nullable=False, default=0)
    status = db.Column(
        db.String(20), nullable=False, default=TripStatus.DRAFT.value, index=True
    )
    departed_at = db.Column(db.DateTime)
    arrived_at = db.Column(db.DateTime)
    odometer_start = db.Column(QUANTITY)
    odometer_end = db.Column(QUANTITY)
    failure_reason = db.Column(db.String(255))
    notes = db.Column(db.Text)

    vehicle = db.relationship("Vehicle", lazy="joined")
    driver = db.relationship("Driver", lazy="joined")
    carrier = db.relationship("Carrier", lazy="joined")
    customer = db.relationship("Customer", lazy="joined")
    expenses = db.relationship(
        "TripExpense", backref="trip", cascade="all, delete-orphan", lazy="select"
    )
    deliveries = db.relationship(
        "DeliveryNote", backref="trip", lazy="select", foreign_keys="DeliveryNote.trip_id"
    )

    # --- Costing -------------------------------------------------------
    @property
    def expenses_total(self) -> Decimal:
        total = sum((row.amount or Decimal("0") for row in self.expenses), Decimal("0"))
        return total.quantize(Decimal("0.01"))

    @property
    def total_cost(self) -> Decimal:
        return ((self.carrier_cost or Decimal("0")) + self.expenses_total).quantize(
            Decimal("0.01")
        )

    @property
    def margin(self) -> Decimal:
        return ((self.transport_fee or Decimal("0")) - self.total_cost).quantize(
            Decimal("0.01")
        )

    @property
    def distance_km(self) -> Decimal | None:
        if self.odometer_start is None or self.odometer_end is None:
            return None
        return (self.odometer_end - self.odometer_start).quantize(Decimal("0.001"))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Trip {self.number} {self.status}>"


class TripExpense(TimestampMixin, ActorMixin, db.Model):
    __tablename__ = "trip_expenses"

    id = db.Column(db.Integer, primary_key=True)
    trip_id = db.Column(
        db.Integer, db.ForeignKey("trips.id"), nullable=False, index=True
    )
    expense_date = db.Column(db.Date, nullable=False)
    expense_type = db.Column(
        db.String(30), nullable=False, default=ExpenseType.OTHER.value, index=True
    )
    amount = db.Column(MONEY, nullable=False)
    paid_by = db.Column(db.String(120))
    note = db.Column(db.String(255))

    @property
    def expense_type_label_key(self) -> str:
        return f"fleet.expense_type.{self.expense_type}"

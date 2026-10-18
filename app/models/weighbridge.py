"""Weighbridge: scale registry, weighing tickets and correction history.

Scope statement (important, and repeated in the UI and in
``docs/WEIGHBRIDGE_AND_TRANSPORT.md``):

* **Manual weighing is implemented.**  An operator types or confirms the gross and
  tare readings; the application validates units, computes the net weight and
  keeps an audit trail.
* **No electronic scale protocol is implemented.**  :class:`WeighbridgeDevice`
  records the devices a business owns and the protocol it *intends* to use, but
  the driver layer currently contains only the manual adapter.  Claiming support
  for a specific indicator without testing it against that model would be false,
  so the registry is documentation plus a clean integration point.
* A weighing ticket is **not** proof of legal metrological compliance.  The
  calibration reference and validity date are recorded so an operator can see
  whether a verification is due; the responsibility for legal verification stays
  with the business.
"""
from __future__ import annotations

from decimal import Decimal

from app.extensions import db
from app.models.base import (
    PERCENT,
    QUANTITY,
    ActorMixin,
    DocumentStatus,
    MeasurementSource,
    TimestampMixin,
    WeighingDirection,
)


class WeighbridgeDevice(TimestampMixin, ActorMixin, db.Model):
    __tablename__ = "weighbridge_devices"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    #: "manual" is the only implemented protocol.  "serial"/"tcp" are placeholders
    #: that must be verified against the physical device before being used.
    protocol = db.Column(db.String(20), nullable=False, default="manual")
    serial_number = db.Column(db.String(60))
    connection_config = db.Column(db.Text)   # e.g. COM3:9600:8N1 — informational
    capacity_kg = db.Column(QUANTITY)
    division_kg = db.Column(QUANTITY)         # smallest displayed increment
    calibration_reference = db.Column(db.String(80))
    calibration_valid_until = db.Column(db.Date)
    last_verification_at = db.Column(db.Date)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    notes = db.Column(db.Text)

    @property
    def calibration_due(self) -> bool:
        from app.services.timeutil import today_local

        if not self.calibration_valid_until:
            return False
        return self.calibration_valid_until < today_local()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<WeighbridgeDevice {self.code} ({self.protocol})>"


class WeighingTicket(TimestampMixin, ActorMixin, db.Model):
    __tablename__ = "weighing_tickets"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, index=True)
    direction = db.Column(
        db.String(10), nullable=False, default=WeighingDirection.OUTBOUND.value, index=True
    )
    ticket_date = db.Column(db.Date, nullable=False, index=True)

    # Truck / people
    plate = db.Column(db.String(30), index=True)
    driver_name = db.Column(db.String(120))
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"))
    carrier_id = db.Column(db.Integer, db.ForeignKey("carriers.id"))
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"))

    # What is being weighed
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"))
    sales_order_id = db.Column(db.Integer, db.ForeignKey("sales_orders.id"))
    delivery_note_id = db.Column(db.Integer, db.ForeignKey("delivery_notes.id"))
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"))
    goods_receipt_id = db.Column(db.Integer, db.ForeignKey("goods_receipts.id"))

    # Measurements — always stored in kilograms (the scale's natural unit).
    unit_id = db.Column(db.Integer, db.ForeignKey("units_of_measure.id"))
    gross_weight = db.Column(QUANTITY)
    tare_weight = db.Column(QUANTITY)
    net_weight = db.Column(QUANTITY, index=True)
    gross_measured_at = db.Column(db.DateTime)
    tare_measured_at = db.Column(db.DateTime)
    gross_source = db.Column(
        db.String(10), nullable=False, default=MeasurementSource.MANUAL.value
    )
    tare_source = db.Column(
        db.String(10), nullable=False, default=MeasurementSource.MANUAL.value
    )
    device_id = db.Column(db.Integer, db.ForeignKey("weighbridge_devices.id"))
    calibration_reference = db.Column(db.String(80))

    # Control
    operator_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    status = db.Column(
        db.String(24), nullable=False,
        default=DocumentStatus.PENDING_APPROVAL.value, index=True,
    )
    expected_quantity = db.Column(QUANTITY)
    expected_unit_id = db.Column(db.Integer, db.ForeignKey("units_of_measure.id"))
    tolerance_percent = db.Column(PERCENT)
    tolerance_exceeded = db.Column(db.Boolean, nullable=False, default=False)
    discrepancy_note = db.Column(db.String(255))
    estimated_volume_m3 = db.Column(QUANTITY)
    density_used_kg_m3 = db.Column(QUANTITY)
    #: Set when the net weight has been turned into a stock movement.
    posted_movement_id = db.Column(db.Integer, db.ForeignKey("stock_movements.id"))
    approved_at = db.Column(db.DateTime)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    notes = db.Column(db.Text)

    product = db.relationship("Product", lazy="joined")
    warehouse = db.relationship("Warehouse", lazy="joined")
    customer = db.relationship("Customer", lazy="joined")
    supplier = db.relationship("Supplier", lazy="joined")
    device = db.relationship("WeighbridgeDevice", lazy="joined")
    operator = db.relationship("User", foreign_keys=[operator_id], lazy="joined")
    approved_by = db.relationship("User", foreign_keys=[approved_by_id], lazy="joined")
    vehicle = db.relationship("Vehicle", lazy="joined")
    driver = db.relationship("Driver", lazy="joined")
    carrier = db.relationship("Carrier", lazy="joined")
    unit = db.relationship("UnitOfMeasure", foreign_keys=[unit_id], lazy="joined")
    corrections = db.relationship(
        "WeighingCorrection", backref="ticket",
        cascade="all, delete-orphan", lazy="select",
        order_by="WeighingCorrection.created_at",
    )

    # --- Computation --------------------------------------------------
    @staticmethod
    def compute_net(gross: Decimal | None, tare: Decimal | None) -> Decimal | None:
        """Net = gross − tare.  Returns ``None`` while a side is missing."""
        if gross is None or tare is None:
            return None
        return (gross - tare).quantize(Decimal("0.001"))

    @property
    def is_posted(self) -> bool:
        return self.posted_movement_id is not None

    @property
    def is_approved(self) -> bool:
        return self.status == DocumentStatus.APPROVED.value

    def __repr__(self) -> str:  # pragma: no cover
        return f"<WeighingTicket {self.number} net={self.net_weight}>"


class WeighingCorrection(db.Model):
    """Append-only correction of a measurement.

    The original value stays on the ticket's history: a correction never
    overwrites silently, it states which field changed, from what to what, why and
    by whom.
    """

    __tablename__ = "weighing_corrections"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(
        db.Integer, db.ForeignKey("weighing_tickets.id"), nullable=False, index=True
    )
    field_name = db.Column(db.String(40), nullable=False)
    old_value = db.Column(db.String(60))
    new_value = db.Column(db.String(60))
    reason = db.Column(db.String(255), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, nullable=False)

    actor = db.relationship("User", lazy="joined")

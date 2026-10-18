"""Shared model building blocks: column types, mixins and enums.

Monetary columns are :class:`~decimal.Decimal` with two decimals; commercial
quantities carry four decimals so that tonnes, cubic metres and linear metres can
be expressed without loss.  Binary floating point is never used for money.
"""
from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import declared_attr

from app.extensions import db
from app.services.timeutil import utcnow

# --- Reusable column types -------------------------------------------------
#: Money: 2 decimals, generous range for Algerian dinar amounts.
MONEY = db.Numeric(18, 2)
#: Commercial quantity: 4 decimals (0.0001 t = 100 g).
QUANTITY = db.Numeric(18, 4)
#: Conversion factors and densities: 6 decimals.
FACTOR = db.Numeric(18, 6)
#: Percentages (tolerances, tax rates expressed as numbers like 19.0000).
PERCENT = db.Numeric(9, 4)


class TimestampMixin:
    """Creation/modification timestamps stored as naive UTC."""

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class ActorMixin:
    """Who created/last modified a record.

    Relationships on a declarative mixin must be :func:`~sqlalchemy.orm.declared_attr`
    callables, otherwise SQLAlchemy raises ``InvalidRequestError`` when the mixin is
    combined with a mapped class.
    """

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    @declared_attr
    def created_by(cls):  # noqa: N805 - declarative mixin signature
        return db.relationship("User", foreign_keys=[cls.created_by_id], lazy="joined")

    @declared_attr
    def updated_by(cls):  # noqa: N805 - declarative mixin signature
        return db.relationship("User", foreign_keys=[cls.updated_by_id], lazy="joined")


class SoftDeleteMixin:
    """Records referenced by documents are deactivated, never deleted."""

    is_active = db.Column(db.Boolean, default=True, nullable=False)

    def deactivate(self) -> None:
        self.is_active = False


# --- Enumerations ----------------------------------------------------------

class UnitKind(str, enum.Enum):
    MASS = "mass"
    VOLUME = "volume"
    COUNT = "count"
    LENGTH = "length"

    @property
    def base_code(self) -> str:
        return {"mass": "kg", "volume": "m3", "count": "piece", "length": "m"}[self.value]


class MovementDirection(str, enum.Enum):
    IN = "in"
    OUT = "out"


class MovementReason(str, enum.Enum):
    OPENING = "opening"
    PURCHASE = "purchase"
    SALE = "sale"
    RETURN_IN = "return_in"
    RETURN_OUT = "return_out"
    ADJUSTMENT = "adjustment"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    COUNT = "count"
    DAMAGE = "damage"
    RESERVATION = "reservation"
    RELEASE = "release"


class DocumentStatus(str, enum.Enum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    CONFIRMED = "confirmed"
    PARTIALLY_DELIVERED = "partially_delivered"
    DELIVERED = "delivered"
    INVOICED = "invoiced"
    CANCELLED = "cancelled"
    REVERSED = "reversed"
    OPEN = "open"
    CLOSED = "closed"
    POSTED = "posted"
    RECEIVED = "received"
    COMPLETED = "completed"
    FAILED = "failed"


class PaymentMethod(str, enum.Enum):
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    CHEQUE = "cheque"
    CARD = "card"
    OTHER = "other"


class PaymentDirection(str, enum.Enum):
    INBOUND = "inbound"     # money received from a customer
    OUTBOUND = "outbound"   # money paid to a supplier


class CustomerType(str, enum.Enum):
    RETAIL = "retail"
    CONTRACTOR = "contractor"
    WHOLESALE = "wholesale"
    PUBLIC = "public"


class PriceList(str, enum.Enum):
    RETAIL = "retail"
    WHOLESALE = "wholesale"
    CONTRACTOR = "contractor"


class WeighingDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MeasurementSource(str, enum.Enum):
    MANUAL = "manual"
    DEVICE = "device"


class OwnershipType(str, enum.Enum):
    OWNED = "owned"
    EXTERNAL = "external"


class TripStatus(str, enum.Enum):
    DRAFT = "draft"
    OPEN = "open"
    IN_TRANSIT = "in_transit"
    COMPLETED = "completed"
    FAILED = "failed"


class ExpenseType(str, enum.Enum):
    FUEL = "fuel"
    TOLL = "toll"
    REPAIR = "repair"
    DRIVER_ALLOWANCE = "driver_allowance"
    CARRIER_FEE = "carrier_fee"
    OTHER = "other"


class ValuationMethod(str, enum.Enum):
    WEIGHTED_AVERAGE = "weighted_average"
    FIFO = "fifo"


ZERO = Decimal("0")
ONE = Decimal("1")

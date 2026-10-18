"""Customers and suppliers (parties).

Legacy ``customers`` rows are preserved: the new columns are nullable or have
defaults, so the migration can extend the existing table in place.
"""
from __future__ import annotations

from app.extensions import db
from app.models.base import (
    MONEY,
    ActorMixin,
    CustomerType,
    SoftDeleteMixin,
    TimestampMixin,
)


class PartyMixin(TimestampMixin, ActorMixin, SoftDeleteMixin):
    """Fields shared by customers and suppliers."""

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, index=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    contact_person = db.Column(db.String(120))
    phone = db.Column(db.String(40), index=True)
    phone_secondary = db.Column(db.String(40))
    email = db.Column(db.String(120))
    # Algerian address structure: wilaya (province) -> commune -> locality.
    wilaya_code = db.Column(db.Integer)
    commune = db.Column(db.String(80))
    locality = db.Column(db.String(80))
    street = db.Column(db.String(160))
    address_details = db.Column(db.Text)
    tax_id = db.Column(db.String(40))
    registry_no = db.Column(db.String(40))
    payment_terms_days = db.Column(db.Integer, nullable=False, default=0)
    opening_balance = db.Column(MONEY, nullable=False, default=0)
    notes = db.Column(db.Text)

    @property
    def full_address(self) -> str:
        parts = [
            self.street,
            self.locality,
            self.commune,
            f"wilaya {self.wilaya_code}" if self.wilaya_code else None,
        ]
        parts.append(self.address_details)
        return " — ".join(part for part in parts if part)


class Customer(PartyMixin, db.Model):
    __tablename__ = "customers"

    customer_type = db.Column(
        db.String(20), nullable=False, default=CustomerType.RETAIL.value, index=True
    )
    #: 0 means "no credit allowed" (cash only).
    credit_limit = db.Column(MONEY, nullable=False, default=0)
    is_blocked = db.Column(db.Boolean, nullable=False, default=False)

    #: Legacy free-text ``address`` column, retained so existing rows remain
    #: readable; the migration backfills the structured fields from it and makes
    #: it nullable.  Deprecated — prefer street/locality/commune/wilaya_code.
    legacy_address = db.Column("address", db.Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Customer {self.code or self.id} {self.name}>"


class Supplier(PartyMixin, db.Model):
    __tablename__ = "suppliers"

    supplied_materials = db.Column(db.String(200))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Supplier {self.code or self.id} {self.name}>"

"""Market reference prices.

The shop's selling price and an observed market price are different facts:

* ``Product.sale_price`` is the price the business has decided to charge.
* ``MarketPrice`` records what an external source reported, where, when, in
  which unit and with which verification status.

A market observation never overwrites a selling price automatically.  Keeping
the observations in their own append-only table also preserves the history that
is needed to explain a purchasing or pricing decision months later.
"""
from __future__ import annotations

import enum

from app.extensions import db
from app.models.base import MONEY, ActorMixin, TimestampMixin


class MarketVerificationStatus(str, enum.Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class MarketPrice(TimestampMixin, ActorMixin, db.Model):
    __tablename__ = "market_prices"
    __table_args__ = (
        db.UniqueConstraint(
            "product_id",
            "unit_id",
            "source_name",
            "observed_on",
            name="uq_market_price_observation",
        ),
        db.Index(
            "ix_market_prices_product_observed",
            "product_id",
            "observed_on",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id"), nullable=False, index=True
    )
    #: Unit to which ``price`` applies.  It is deliberately explicit: 1 m3 of
    #: sand and 1 tonne of sand are not interchangeable observations.
    unit_id = db.Column(
        db.Integer, db.ForeignKey("units_of_measure.id"), nullable=False, index=True
    )
    price = db.Column(MONEY, nullable=False)
    currency = db.Column(db.String(8), nullable=False, default="DZD")

    source_name = db.Column(db.String(160), nullable=False, index=True)
    source_reference = db.Column(db.String(255))
    wilaya_code = db.Column(db.Integer, index=True)
    location = db.Column(db.String(160))
    observed_on = db.Column(db.Date, nullable=False, index=True)
    verification_status = db.Column(
        db.String(20),
        nullable=False,
        default=MarketVerificationStatus.UNVERIFIED.value,
        index=True,
    )
    notes = db.Column(db.Text)

    product = db.relationship("Product", lazy="joined")
    unit = db.relationship("UnitOfMeasure", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<MarketPrice product={self.product_id} {self.price} "
            f"{self.unit_id} on {self.observed_on}>"
        )

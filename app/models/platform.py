"""Platform models: business settings, document numbering, audit trail."""
from __future__ import annotations

from app.extensions import db
from app.models.base import PERCENT, TimestampMixin, ValuationMethod
from app.services.timeutil import utcnow


class BusinessSettings(TimestampMixin, db.Model):
    """Single-row table (``id = 1``) holding configurable business parameters.

    Every value here is *configuration*, not regulatory advice.  See
    ``docs/ALGERIA_LOCALIZATION.md`` for what has and has not been verified.
    """

    __tablename__ = "business_settings"

    id = db.Column(db.Integer, primary_key=True)

    # Identity -------------------------------------------------------
    business_name = db.Column(db.String(160), nullable=False, default="")
    legal_name = db.Column(db.String(160))
    tax_id = db.Column(db.String(40))          # NIF
    registry_no = db.Column(db.String(40))     # RC
    activity = db.Column(db.String(160))

    # Contact / address ---------------------------------------------
    phone = db.Column(db.String(40))
    email = db.Column(db.String(120))
    street = db.Column(db.String(160))
    commune = db.Column(db.String(80))
    locality = db.Column(db.String(80))
    wilaya_code = db.Column(db.Integer)
    bank_details = db.Column(db.Text)
    invoice_footer = db.Column(db.Text)

    # Localisation ---------------------------------------------------
    currency = db.Column(db.String(8), nullable=False, default="DZD")
    currency_symbol = db.Column(db.String(8), nullable=False, default="DA")
    timezone = db.Column(db.String(64), nullable=False, default="Africa/Algiers")
    default_language = db.Column(db.String(5), nullable=False, default="ar")
    decimal_separator = db.Column(db.String(4), nullable=False, default=",")
    thousands_separator = db.Column(db.String(4), nullable=False, default=" ")

    # Tax / accounting ----------------------------------------------
    #: Configurable rate, expressed as a percentage (19 means 19 %).
    vat_rate = db.Column(PERCENT, nullable=False, default=19)
    prices_include_tax = db.Column(db.Boolean, nullable=False, default=False)
    valuation_method = db.Column(
        db.String(24), nullable=False, default=ValuationMethod.WEIGHTED_AVERAGE.value
    )

    # Modules --------------------------------------------------------
    enable_weighbridge = db.Column(db.Boolean, nullable=False, default=True)
    enable_fleet = db.Column(db.Boolean, nullable=False, default=True)
    enable_accounting = db.Column(db.Boolean, nullable=False, default=False)
    #: Administrators may switch the AI workspace off for the whole business.
    #: Turning it off never affects sales, stock or accounting.
    enable_ai = db.Column(db.Boolean, nullable=False, default=True)

    # Operational parameters ----------------------------------------
    default_warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    weigh_tolerance_percent = db.Column(PERCENT, nullable=False, default=2)
    default_payment_terms_days = db.Column(db.Integer, nullable=False, default=0)
    allow_negative_stock = db.Column(db.Boolean, nullable=False, default=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BusinessSettings {self.business_name!r}>"


class NumberSequence(TimestampMixin, db.Model):
    """Atomic per-document-type numbering.

    ``next_number`` is incremented inside a locked transaction by
    :func:`app.services.numbering.allocate`, so concurrent document creation
    cannot produce duplicates.
    """

    __tablename__ = "number_sequences"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    label_key = db.Column(db.String(60), nullable=False)
    prefix = db.Column(db.String(12), nullable=False, default="")
    next_number = db.Column(db.Integer, nullable=False, default=1)
    padding = db.Column(db.Integer, nullable=False, default=5)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NumberSequence {self.code} -> {self.prefix}{self.next_number}>"


class AuditEvent(db.Model):
    """Append-only audit trail.

    Nothing in the application deletes from this table.  Actions are recorded by
    :func:`app.services.audit.record`, which is called from the service layer for
    every state change that matters (documents, stock, prices, payments,
    weighing corrections, settings and authentication).
    """

    __tablename__ = "audit_events"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    actor_name = db.Column(db.String(80))
    action = db.Column(db.String(40), nullable=False, index=True)
    entity_type = db.Column(db.String(60), nullable=False, index=True)
    entity_id = db.Column(db.Integer, index=True)
    summary = db.Column(db.String(255))
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(64))
    user_agent = db.Column(db.String(255))

    actor = db.relationship("User", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditEvent {self.action} {self.entity_type}#{self.entity_id}>"

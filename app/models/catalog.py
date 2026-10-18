"""Product catalog: categories, brands, units of measure, products, conversions.

Design rules taken from the audit and the Algerian scope:

* A product always has an explicit **stock unit**.  Quantities are stored in that
  unit; anything else must be converted through a *declared* rule.
* Units of different kinds (mass ⇄ volume) are **never** converted by a global
  constant.  A cubic metre of sand is not a fixed number of kilograms: the
  business must declare a density, with its source and effective date, and the
  result is then flagged as an estimate.
* Packaging units (bag, bundle, pallet, truckload, bar) carry no universal
  weight, so their conversion is always product-specific.
"""
from __future__ import annotations

from app.extensions import db
from app.models.base import (
    FACTOR,
    MONEY,
    QUANTITY,
    ActorMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UnitKind,
)


class Category(TimestampMixin, SoftDeleteMixin, db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name_ar = db.Column(db.String(120), nullable=False)
    name_fr = db.Column(db.String(120), nullable=False)
    name_en = db.Column(db.String(120), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    parent = db.relationship("Category", remote_side=[id], backref="children")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Category {self.code}>"


class Brand(TimestampMixin, SoftDeleteMixin, db.Model):
    __tablename__ = "brands"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    country = db.Column(db.String(80))


class UnitOfMeasure(TimestampMixin, SoftDeleteMixin, db.Model):
    """Unit of measure with a factor relative to the base unit of its kind.

    Kind bases: ``mass`` → kg, ``volume`` → m³, ``count`` → piece, ``length`` → m.
    ``truckload``, ``bag``, ``pallet`` and ``bundle`` are *count* units with
    factor 1 because their content is supply-specific; converting them to a
    physical quantity always requires a product-level conversion rule.
    """

    __tablename__ = "units_of_measure"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    kind = db.Column(db.String(12), nullable=False, default=UnitKind.COUNT.value)
    factor = db.Column(FACTOR, nullable=False, default=1)
    name_ar = db.Column(db.String(60), nullable=False)
    name_fr = db.Column(db.String(60), nullable=False)
    name_en = db.Column(db.String(60), nullable=False)
    allows_fraction = db.Column(db.Boolean, nullable=False, default=True)
    is_system = db.Column(db.Boolean, nullable=False, default=False)

    @property
    def kind_enum(self) -> UnitKind:
        return UnitKind(self.kind)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UnitOfMeasure {self.code}>"


class ProductConversion(TimestampMixin, ActorMixin, db.Model):
    """Declared conversion rule between two units **for a given product**.

    ``effective_from`` plus ``source`` make the rule auditable: a conversion that
    was applied to an old transaction can always be explained afterwards.
    """

    __tablename__ = "product_conversions"
    __table_args__ = (
        db.UniqueConstraint(
            "product_id", "from_unit_id", "to_unit_id", "effective_from",
            name="uq_product_conversion",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id"), nullable=False, index=True
    )
    from_unit_id = db.Column(
        db.Integer, db.ForeignKey("units_of_measure.id"), nullable=False
    )
    to_unit_id = db.Column(
        db.Integer, db.ForeignKey("units_of_measure.id"), nullable=False
    )
    #: 1 ``from_unit`` = ``factor`` × ``to_unit``.
    factor = db.Column(FACTOR, nullable=False)
    effective_from = db.Column(db.Date, nullable=False)
    #: "packaging" (bag → kg), "density" (m³ → kg, an estimate) or "other".
    conversion_type = db.Column(db.String(20), nullable=False, default="other")
    source = db.Column(db.String(200))
    notes = db.Column(db.Text)

    product = db.relationship("Product", backref=db.backref("conversions", lazy="select"))
    from_unit = db.relationship("UnitOfMeasure", foreign_keys=[from_unit_id])
    to_unit = db.relationship("UnitOfMeasure", foreign_keys=[to_unit_id])

    @property
    def is_density_based(self) -> bool:
        return self.conversion_type == "density"


class ProductUnit(db.Model):
    """Which units may be used to sell/buy a given product."""

    __tablename__ = "product_units"
    __table_args__ = (
        db.UniqueConstraint("product_id", "unit_id", name="uq_product_unit"),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id"), nullable=False, index=True
    )
    unit_id = db.Column(
        db.Integer, db.ForeignKey("units_of_measure.id"), nullable=False
    )
    allow_sale = db.Column(db.Boolean, nullable=False, default=True)
    allow_purchase = db.Column(db.Boolean, nullable=False, default=True)

    unit = db.relationship("UnitOfMeasure", lazy="joined")


class Product(TimestampMixin, ActorMixin, SoftDeleteMixin, db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(40), unique=True, index=True)
    barcode = db.Column(db.String(60), index=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    description = db.Column(db.Text)

    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), index=True)
    brand_id = db.Column(db.Integer, db.ForeignKey("brands.id"))
    default_supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"))
    supplier_reference = db.Column(db.String(60))

    #: The unit in which stock is held.  Mandatory: no product may exist without
    #: one, because every stock movement is expressed in this unit.
    stock_unit_id = db.Column(
        db.Integer, db.ForeignKey("units_of_measure.id"), nullable=False, index=True
    )
    pack_size = db.Column(QUANTITY)

    # Prices are per stock unit unless a price list overrides them.
    cost_price = db.Column(MONEY, nullable=False, default=0)
    sale_price = db.Column(MONEY, nullable=False, default=0)
    wholesale_price = db.Column(MONEY)
    contractor_price = db.Column(MONEY)
    vat_rate = db.Column(db.Numeric(9, 4))

    reorder_level = db.Column(QUANTITY, nullable=False, default=0)
    # NOTE: batch/lot tracking is *not* implemented.  It is listed as an open
    # item in docs/IMPLEMENTATION_ROADMAP.md rather than exposed as a switch that
    # would do nothing.

    # Material-specific specification (all optional and business-defined).
    steel_diameter_mm = db.Column(db.Numeric(9, 2))
    steel_length_m = db.Column(db.Numeric(9, 3))
    steel_grade = db.Column(db.String(60))
    bag_weight_kg = db.Column(db.Numeric(9, 3))
    aggregate_grading = db.Column(db.String(60))
    spec_notes = db.Column(db.Text)

    # --- Legacy mirror columns (deprecated) --------------------------
    # The pre-refactor table stored a single ``price``, an INTEGER
    # ``stock_quantity`` and a free-text ``unit``.  They are retained so that
    # existing queries, exports and customer habits keep working; the service
    # layer keeps them loosely in sync.  They will be dropped in a later release
    # once no external report depends on them (see docs/IMPLEMENTATION_ROADMAP.md).
    legacy_price = db.Column("price", MONEY, nullable=True)
    legacy_stock_quantity = db.Column("stock_quantity", db.Integer, nullable=True)
    legacy_unit = db.Column("unit", db.String(20), nullable=True)

    category = db.relationship("Category", lazy="joined")
    brand = db.relationship("Brand", lazy="joined")
    stock_unit = db.relationship("UnitOfMeasure", lazy="joined")
    default_supplier = db.relationship("Supplier", lazy="joined")
    units = db.relationship(
        "ProductUnit", backref="product", cascade="all, delete-orphan", lazy="select"
    )

    # --- Helper accessors -------------------------------------------
    def price_for(self, price_list: str = "retail"):
        from app.models.base import PriceList

        mapping = {
            PriceList.RETAIL.value: self.sale_price,
            PriceList.WHOLESALE.value: self.wholesale_price or self.sale_price,
            PriceList.CONTRACTOR.value: self.contractor_price or self.sale_price,
        }
        return mapping.get(price_list) or self.sale_price

    @property
    def stock_unit_code(self) -> str:
        return self.stock_unit.code if self.stock_unit else ""

    def sync_legacy_mirrors(self, on_hand=None) -> None:
        """Refresh the deprecated mirror columns from the new fields."""
        if self.sale_price is not None:
            self.legacy_price = self.sale_price
        if self.stock_unit is not None:
            self.legacy_unit = self.stock_unit.code
        if on_hand is not None:
            self.legacy_stock_quantity = int(on_hand)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Product {self.sku or self.id} {self.name}>"

"""Unit-of-measure conversion.

Rules encoded here
------------------
* **Same kind** (mass↔mass, volume↔volume, count↔count, length↔length) converts
  through the units' declared factors, e.g. ``1 t = 1000 kg``.
* **Different kinds** (mass↔volume) converts **only** when a product-level rule
  exists, is effective on the transaction date and carries a source.  A rule of
  type ``density`` is reported as an *estimate*: the caller decides whether an
  estimate is acceptable for that transaction.
* A **packaging** rule (bag → kg, pallet → piece, truckload → t) is likewise
  product-specific, because a bag of cement, a bundle of steel and a truckload of
  sand have no universal weight.
* Nothing is ever guessed: a missing rule raises
  ``val.conversion_missing`` instead of substituting a default.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.extensions import db
from app.i18n import to_decimal
from app.models import Product, ProductConversion, UnitOfMeasure
from app.models.base import UnitKind
from app.services.errors import NotFoundError, ValidationError
from app.services.timeutil import today_local

QUANTITY_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class Conversion:
    """Outcome of a conversion request."""

    quantity: Decimal
    factor: Decimal
    from_unit: UnitOfMeasure
    to_unit: UnitOfMeasure
    is_estimate: bool = False
    rule: ProductConversion | None = None

    @property
    def source(self) -> str | None:
        return self.rule.source if self.rule else None


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(QUANTITY_PLACES)


def parse_quantity(raw, field: str = "quantity") -> Decimal:
    """Parse a user-supplied quantity, accepting ``,`` or ``.`` decimals."""
    if raw is None or str(raw).strip() == "":
        raise ValidationError("val.required")
    try:
        value = to_decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError("val.invalid_number") from None
    return _quantize(value)


def parse_money(raw, field: str = "amount") -> Decimal:
    if raw is None or str(raw).strip() == "":
        raise ValidationError("val.required")
    try:
        value = to_decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError("val.invalid_number") from None
    return value.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Unit lookups
# ---------------------------------------------------------------------------

def unit_by_code(code: str) -> UnitOfMeasure:
    unit = db.session.execute(
        select(UnitOfMeasure).filter_by(code=code)
    ).scalars().first()
    if unit is None:
        raise NotFoundError("error.not_found")
    return unit


def unit_by_id(unit_id: int) -> UnitOfMeasure:
    unit = db.session.get(UnitOfMeasure, unit_id)
    if unit is None:
        raise NotFoundError("error.not_found")
    return unit


def default_stock_units() -> dict[str, UnitOfMeasure]:
    """Code → unit for the seeded units (used by forms and seeding)."""
    return {
        unit.code: unit
        for unit in db.session.execute(select(UnitOfMeasure)).scalars().all()
    }


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _find_rule(
    product: Product,
    from_unit: UnitOfMeasure,
    to_unit: UnitOfMeasure,
    on_date: date,
    inverse: bool = False,
) -> ProductConversion | None:
    query = select(ProductConversion).filter(
        ProductConversion.product_id == product.id,
        ProductConversion.effective_from <= on_date,
    )
    if inverse:
        query = query.filter(
            ProductConversion.from_unit_id == to_unit.id,
            ProductConversion.to_unit_id == from_unit.id,
        )
    else:
        query = query.filter(
            ProductConversion.from_unit_id == from_unit.id,
            ProductConversion.to_unit_id == to_unit.id,
        )
    query = query.order_by(ProductConversion.effective_from.desc())
    return db.session.execute(query).scalars().first()


def convert(
    product: Product,
    quantity: Decimal,
    from_unit: UnitOfMeasure,
    to_unit: UnitOfMeasure,
    on_date: date | None = None,
) -> Conversion:
    """Convert ``quantity`` between two units for ``product``.

    Raises :class:`~app.services.errors.ValidationError` when no valid rule
    exists — the caller must not fall back to an assumption.
    """
    on_date = on_date or today_local()
    quantity = _quantize(Decimal(quantity))

    if from_unit.id == to_unit.id:
        return Conversion(quantity, Decimal("1"), from_unit, to_unit)

    if from_unit.kind == to_unit.kind:
        factor = (Decimal(from_unit.factor) / Decimal(to_unit.factor)).normalize()
        return Conversion(_quantize(quantity * factor), factor, from_unit, to_unit)

    rule = _find_rule(product, from_unit, to_unit, on_date)
    if rule is not None:
        return Conversion(
            _quantize(quantity * Decimal(rule.factor)),
            Decimal(rule.factor),
            from_unit,
            to_unit,
            is_estimate=rule.is_density_based,
            rule=rule,
        )

    inverse = _find_rule(product, from_unit, to_unit, on_date, inverse=True)
    if inverse is not None and Decimal(inverse.factor) != 0:
        factor = (Decimal("1") / Decimal(inverse.factor)).normalize()
        return Conversion(
            _quantize(quantity * factor),
            factor,
            from_unit,
            to_unit,
            is_estimate=inverse.is_density_based,
            rule=inverse,
        )

    raise ValidationError(
        "val.conversion_missing",
        from_unit=from_unit.code,
        to_unit=to_unit.code,
    )


def to_stock_unit(
    product: Product,
    quantity: Decimal,
    unit: UnitOfMeasure,
    on_date: date | None = None,
) -> Conversion:
    """Convert an entered quantity into the product's stock unit."""
    if product.stock_unit is None:
        raise ValidationError("val.invalid_unit", unit=unit.code)
    return convert(product, quantity, unit, product.stock_unit, on_date=on_date)


def allowed_units(product: Product, purpose: str = "sale") -> list[UnitOfMeasure]:
    """Units explicitly permitted for selling/buying ``product``.

    At minimum the stock unit is always allowed, so a product is always usable
    even before the administrator configures extra units.
    """
    units: list[UnitOfMeasure] = []
    for link in product.units:
        if purpose == "sale" and not link.allow_sale:
            continue
        if purpose == "purchase" and not link.allow_purchase:
            continue
        if link.unit is not None:
            units.append(link.unit)
    if product.stock_unit is not None and all(u.id != product.stock_unit.id for u in units):
        units.insert(0, product.stock_unit)
    return units


def validate_unit_allowed(product: Product, unit: UnitOfMeasure, purpose: str = "sale") -> None:
    if unit is None:
        raise ValidationError("val.invalid_unit", unit="")
    allowed = {u.id for u in allowed_units(product, purpose)}
    if unit.id not in allowed:
        raise ValidationError("val.invalid_unit", unit=unit.code)


def density_rule(product: Product, on_date: date | None = None) -> ProductConversion | None:
    """The product's effective density rule (m³ ↔ kg), if the business declared one."""
    on_date = on_date or today_local()
    rules = db.session.execute(
        select(ProductConversion).filter(
            ProductConversion.product_id == product.id,
            ProductConversion.conversion_type == "density",
            ProductConversion.effective_from <= on_date,
        )
    ).scalars().all()
    if not rules:
        return None
    return max(rules, key=lambda rule: rule.effective_from)


def estimate_volume_from_mass(
    product: Product, mass_kg: Decimal, on_date: date | None = None
) -> tuple[Decimal, Decimal] | None:
    """Return ``(volume_m3, density_used)`` or ``None`` when no density is declared.

    The result is by construction an estimate and the caller must label it as
    such; this function never invents a density.
    """
    rule = density_rule(product, on_date)
    if rule is None:
        return None
    density = Decimal(rule.factor)
    if density <= 0:
        return None
    volume = _quantize(Decimal(mass_kg) / density)
    return volume, density


def mass_from_volume(
    product: Product, volume_m3: Decimal, on_date: date | None = None
) -> tuple[Decimal, Decimal] | None:
    rule = density_rule(product, on_date)
    if rule is None:
        return None
    density = Decimal(rule.factor)
    return _quantize(Decimal(volume_m3) * density), density


def theoretical_steel_weight(
    diameter_mm: Decimal, length_m: Decimal, bars: Decimal = Decimal("1")
) -> Decimal:
    """Theoretical mass of reinforcing steel in kilograms.

    Uses the standard nominal-mass formula for steel::

        mass per metre (kg/m) = diameter² / 162

    which follows from a density of 7 850 kg/m³ for a round bar
    (``π/4 · d² · ρ``).  This is a *theoretical* value: it is offered for
    estimating and stock-checking only and must never replace an actual certified
    weighing when the transaction requires measured weight.
    """
    diameter = Decimal(diameter_mm)
    if diameter <= 0:
        raise ValidationError("val.must_be_positive")
    per_metre = (diameter * diameter) / Decimal("162")
    total = per_metre * Decimal(length_m) * Decimal(bars)
    return _quantize(total)


def same_kind_ratio(from_unit: UnitOfMeasure, to_unit: UnitOfMeasure) -> Decimal | None:
    """Factor between two units of the same kind, or ``None`` if kinds differ."""
    if from_unit.kind != to_unit.kind:
        return None
    return (Decimal(from_unit.factor) / Decimal(to_unit.factor)).normalize()


def base_unit_for(kind: str) -> UnitKind:
    return UnitKind(kind)

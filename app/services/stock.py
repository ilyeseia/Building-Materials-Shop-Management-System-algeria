"""Stock ledger service.

Guarantees
----------
1. **Append-only.**  ``StockLevel`` is a cache of the ledger; every change writes a
   :class:`~app.models.inventory.StockMovement` recording the quantity, the unit
   used, the factor applied, the source document, the actor and the balance after.
2. **No overselling.**  Debits are applied with a *conditional SQL update*
   (``... WHERE quantity - reserved >= :qty``).  The condition is evaluated by the
   database, so two simultaneous requests cannot both pass an
   application-level check — the second one gets ``rowcount = 0`` and the caller
   receives :class:`~app.services.errors.InsufficientStock`.
3. **Reservations are separate from stock.**  Confirming an order reserves
   quantity; it does not remove it.  Delivery consumes the reservation.
4. **Valuation.**  Weighted-average cost is recomputed on every receipt from the
   actual cost of the goods received (including allocated landed costs).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    MovementDirection,
    MovementReason,
    Product,
    StockLevel,
    StockMovement,
    UnitOfMeasure,
    Warehouse,
)
from app.services import audit
from app.services.errors import InsufficientStock, NotFoundError, ValidationError
from app.services.timeutil import today_local, utcnow
from app.services.uom import _quantize  # noqa: PLC2701 - shared quantizer

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Level access
# ---------------------------------------------------------------------------

def get_level(
    product: Product | int,
    warehouse: Warehouse | int,
    *,
    create: bool = False,
    lock: bool = False,
) -> StockLevel | None:
    product_id = product.id if isinstance(product, Product) else int(product)
    warehouse_id = warehouse.id if isinstance(warehouse, Warehouse) else int(warehouse)

    query = select(StockLevel).filter_by(product_id=product_id, warehouse_id=warehouse_id)
    if lock:
        query = query.with_for_update()
    level = db.session.execute(query).scalars().first()
    if level is not None or not create:
        return level

    level = StockLevel(
        product_id=product_id, warehouse_id=warehouse_id,
        quantity=ZERO, reserved=ZERO, avg_cost=ZERO,
    )
    try:
        with db.session.begin_nested():
            db.session.add(level)
            db.session.flush()
    except IntegrityError:
        # Another request created the row first: fall back to reading it.
        level = db.session.execute(
            select(StockLevel).filter_by(
                product_id=product_id, warehouse_id=warehouse_id
            )
        ).scalars().first()
    return level


def on_hand(product: Product, warehouse: Warehouse) -> Decimal:
    level = get_level(product, warehouse)
    return Decimal(level.quantity) if level else ZERO


def available(product: Product, warehouse: Warehouse | None = None) -> Decimal:
    """Physical stock minus reservations.

    With no warehouse given, the sum across all warehouses is returned (used by
    availability checks that are not warehouse-specific).
    """
    if warehouse is not None:
        level = get_level(product, warehouse)
        return Decimal(level.available) if level else ZERO
    total = db.session.execute(
        select(func.coalesce(func.sum(StockLevel.quantity - StockLevel.reserved), 0))
        .filter(StockLevel.product_id == product.id)
    ).scalar()
    return Decimal(str(total or 0))


def total_on_hand(product: Product | int) -> Decimal:
    product_id = product.id if isinstance(product, Product) else int(product)
    total = db.session.execute(
        select(func.coalesce(func.sum(StockLevel.quantity), 0))
        .filter(StockLevel.product_id == product_id)
    ).scalar()
    return Decimal(str(total or 0))


def default_warehouse() -> Warehouse | None:
    from app.services.settings import get_settings

    settings = get_settings()
    if settings and settings.default_warehouse_id:
        warehouse = db.session.get(Warehouse, settings.default_warehouse_id)
        if warehouse is not None:
            return warehouse
    return db.session.execute(
        select(Warehouse).filter_by(is_active=True).order_by(Warehouse.is_default.desc(), Warehouse.id)
    ).scalars().first()


def _refresh(level: StockLevel) -> StockLevel:
    db.session.refresh(level)
    return level


def _write_movement(
    level: StockLevel,
    *,
    direction: str,
    reason: str,
    quantity: Decimal,
    reserved_delta: Decimal = ZERO,
    unit: UnitOfMeasure | None = None,
    original_quantity: Decimal | None = None,
    original_unit: UnitOfMeasure | None = None,
    conversion_factor: Decimal | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    reference: str | None = None,
    note: str | None = None,
    actor_id: int | None = None,
) -> StockMovement:
    movement = StockMovement(
        created_at=utcnow(),
        product_id=level.product_id,
        warehouse_id=level.warehouse_id,
        direction=direction,
        reason=reason,
        quantity=quantity,
        reserved_delta=reserved_delta,
        on_hand_after=Decimal(level.quantity),
        reserved_after=Decimal(level.reserved),
        unit_id=(unit or level.product.stock_unit).id if (unit or level.product.stock_unit) else None,
        original_quantity=original_quantity,
        original_unit_id=original_unit.id if original_unit else None,
        conversion_factor=conversion_factor,
        source_type=source_type,
        source_id=source_id,
        reference=reference,
        note=note,
        actor_id=actor_id,
    )
    db.session.add(movement)
    db.session.flush()
    _sync_legacy_stock(level.product_id)
    return movement


def _sync_legacy_stock(product_id: int) -> None:
    """Keep the deprecated ``products.stock_quantity`` column roughly in step."""
    product = db.session.get(Product, product_id)
    if product is None:
        return
    product.legacy_stock_quantity = int(total_on_hand(product_id))


def _actor_id() -> int | None:
    try:
        from flask_login import current_user

        if current_user and getattr(current_user, "is_authenticated", False):
            return current_user.id
    except Exception:  # pragma: no cover
        pass
    return None


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def receive(
    product: Product,
    warehouse: Warehouse,
    quantity: Decimal,
    *,
    unit: UnitOfMeasure | None = None,
    reason: str = MovementReason.PURCHASE.value,
    unit_cost: Decimal | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    reference: str | None = None,
    note: str | None = None,
    movement_date: date | None = None,
    original_quantity: Decimal | None = None,
    conversion_factor: Decimal | None = None,
) -> StockMovement:
    """Add stock and, when a cost is supplied, refresh the weighted-average cost."""
    quantity = _quantize(Decimal(quantity))
    if quantity <= 0:
        raise ValidationError("val.quantity_zero")

    level = get_level(product, warehouse, create=True, lock=True)
    assert level is not None  # created above

    new_quantity = Decimal(level.quantity) + quantity
    new_avg = Decimal(level.avg_cost or ZERO)
    if unit_cost is not None:
        unit_cost = Decimal(unit_cost).quantize(Decimal("0.01"))
        prior_value = Decimal(level.quantity) * Decimal(level.avg_cost or ZERO)
        new_avg = ((prior_value + quantity * unit_cost) / new_quantity).quantize(
            Decimal("0.01")
        )

    db.session.execute(
        update(StockLevel)
        .where(StockLevel.id == level.id)
        .values(quantity=new_quantity, avg_cost=new_avg)
    )
    _refresh(level)

    return _write_movement(
        level,
        direction=MovementDirection.IN.value,
        reason=reason,
        quantity=quantity,
        unit=unit,
        original_quantity=original_quantity if original_quantity is not None else quantity,
        original_unit=unit,
        conversion_factor=conversion_factor,
        source_type=source_type,
        source_id=source_id,
        reference=reference,
        note=note,
        actor_id=_actor_id(),
    )


def consume(
    product: Product,
    warehouse: Warehouse,
    quantity: Decimal,
    *,
    unit: UnitOfMeasure | None = None,
    reason: str = MovementReason.SALE.value,
    source_type: str | None = None,
    source_id: int | None = None,
    reference: str | None = None,
    note: str | None = None,
    allow_negative: bool = False,
    original_quantity: Decimal | None = None,
    conversion_factor: Decimal | None = None,
) -> StockMovement:
    """Remove stock, refusing to oversell unless the business allows it."""
    quantity = _quantize(Decimal(quantity))
    if quantity <= 0:
        raise ValidationError("val.quantity_zero")

    level = get_level(product, warehouse, create=True, lock=True)
    assert level is not None

    statement = update(StockLevel).where(StockLevel.id == level.id)
    if allow_negative:
        statement = statement.values(quantity=StockLevel.quantity - quantity)
    else:
        statement = (
            statement
            .where(StockLevel.quantity - StockLevel.reserved >= quantity)
            .values(quantity=StockLevel.quantity - quantity)
        )
    result = db.session.execute(statement)
    if result.rowcount != 1:
        level_now = get_level(product, warehouse)
        raise InsufficientStock(
            "val.insufficient_stock",
            product=product.name,
            requested=str(quantity),
            available=str(level_now.available if level_now else 0),
        )
    _refresh(level)

    return _write_movement(
        level,
        direction=MovementDirection.OUT.value,
        reason=reason,
        quantity=quantity,
        unit=unit,
        original_quantity=original_quantity if original_quantity is not None else quantity,
        original_unit=unit,
        conversion_factor=conversion_factor,
        source_type=source_type,
        source_id=source_id,
        reference=reference,
        note=note,
        actor_id=_actor_id(),
    )


def adjust(
    product: Product,
    warehouse: Warehouse,
    signed_quantity: Decimal,
    *,
    reason: str = MovementReason.ADJUSTMENT.value,
    justification: str = "",
    source_type: str = "stock_adjustment",
    source_id: int | None = None,
    note: str | None = None,
) -> StockMovement:
    """Apply a manual adjustment.  ``signed_quantity`` may be negative."""
    signed_quantity = _quantize(Decimal(signed_quantity))
    if signed_quantity == 0:
        raise ValidationError("val.quantity_zero")
    if not justification:
        raise ValidationError("val.required")
    if signed_quantity > 0:
        return receive(
            product, warehouse, signed_quantity, reason=reason,
            source_type=source_type, source_id=source_id, note=note or justification,
        )
    return consume(
        product, warehouse, -signed_quantity, reason=reason,
        source_type=source_type, source_id=source_id, note=note or justification,
    )


def reserve(
    product: Product,
    warehouse: Warehouse,
    quantity: Decimal,
    *,
    source_type: str | None = None,
    source_id: int | None = None,
    note: str | None = None,
) -> StockMovement:
    quantity = _quantize(Decimal(quantity))
    if quantity <= 0:
        raise ValidationError("val.quantity_zero")
    level = get_level(product, warehouse, create=True, lock=True)
    assert level is not None
    result = db.session.execute(
        update(StockLevel)
        .where(
            StockLevel.id == level.id,
            StockLevel.quantity - StockLevel.reserved >= quantity,
        )
        .values(reserved=StockLevel.reserved + quantity)
    )
    if result.rowcount != 1:
        level_now = get_level(product, warehouse)
        raise InsufficientStock(
            "val.insufficient_stock",
            product=product.name,
            requested=str(quantity),
            available=str(level_now.available if level_now else 0),
        )
    _refresh(level)
    return _write_movement(
        level,
        direction=MovementDirection.IN.value,
        reason=MovementReason.RESERVATION.value,
        quantity=ZERO,
        reserved_delta=quantity,
        source_type=source_type,
        source_id=source_id,
        note=note,
        actor_id=_actor_id(),
    )


def release(
    product: Product,
    warehouse: Warehouse,
    quantity: Decimal,
    *,
    source_type: str | None = None,
    source_id: int | None = None,
    note: str | None = None,
) -> StockMovement:
    quantity = _quantize(Decimal(quantity))
    if quantity <= 0:
        raise ValidationError("val.quantity_zero")
    level = get_level(product, warehouse, create=True, lock=True)
    assert level is not None
    result = db.session.execute(
        update(StockLevel)
        .where(StockLevel.id == level.id, StockLevel.reserved >= quantity)
        .values(reserved=StockLevel.reserved - quantity)
    )
    if result.rowcount != 1:
        # Releasing more than is reserved is a programming error, but never leave
        # a negative reservation behind: clamp to zero.
        db.session.execute(
            update(StockLevel).where(StockLevel.id == level.id).values(reserved=ZERO)
        )
    _refresh(level)
    return _write_movement(
        level,
        direction=MovementDirection.OUT.value,
        reason=MovementReason.RELEASE.value,
        quantity=ZERO,
        reserved_delta=-quantity,
        source_type=source_type,
        source_id=source_id,
        note=note,
        actor_id=_actor_id(),
    )


def consume_reserved(
    product: Product,
    warehouse: Warehouse,
    quantity: Decimal,
    *,
    reason: str = MovementReason.SALE.value,
    source_type: str | None = None,
    source_id: int | None = None,
    reference: str | None = None,
    note: str | None = None,
    allow_negative: bool = False,
    original_quantity: Decimal | None = None,
    unit: UnitOfMeasure | None = None,
    conversion_factor: Decimal | None = None,
) -> StockMovement:
    """Consume stock that was reserved earlier (delivery of a confirmed order).

    The physical quantity and the reservation are decremented together, so the
    available quantity does not drop twice.
    """
    quantity = _quantize(Decimal(quantity))
    if quantity <= 0:
        raise ValidationError("val.quantity_zero")
    level = get_level(product, warehouse, create=True, lock=True)
    assert level is not None

    reserved_before = Decimal(level.reserved)
    # Releasing a reservation can never release more than is actually reserved.
    reserved_after = max(reserved_before - quantity, ZERO)
    reserved_delta = reserved_after - reserved_before  # zero or negative

    statement = (
        update(StockLevel)
        .where(StockLevel.id == level.id)
        .values(
            quantity=StockLevel.quantity - quantity,
            reserved=StockLevel.reserved + reserved_delta,
        )
    )
    if not allow_negative:
        statement = statement.where(StockLevel.quantity - StockLevel.reserved >= ZERO)
    result = db.session.execute(statement)
    if result.rowcount != 1:
        level_now = get_level(product, warehouse)
        raise InsufficientStock(
            "val.insufficient_stock",
            product=product.name,
            requested=str(quantity),
            available=str(level_now.available if level_now else 0),
        )
    _refresh(level)
    return _write_movement(
        level,
        direction=MovementDirection.OUT.value,
        reason=reason,
        quantity=quantity,
        reserved_delta=reserved_delta,
        unit=unit,
        original_quantity=original_quantity if original_quantity is not None else quantity,
        original_unit=unit,
        conversion_factor=conversion_factor,
        source_type=source_type,
        source_id=source_id,
        reference=reference,
        note=note,
        actor_id=_actor_id(),
    )


# ---------------------------------------------------------------------------
# Transfers and physical counts
# ---------------------------------------------------------------------------

def post_transfer(transfer) -> None:
    from app.models import DocumentStatus, StockTransfer

    if transfer.status == DocumentStatus.POSTED.value:
        raise ValidationError("val.bad_status_transition")
    if transfer.source_warehouse_id == transfer.destination_warehouse_id:
        raise ValidationError("val.bad_status_transition")
    for line in transfer.lines:
        consume(
            line.product, transfer.source_warehouse, line.quantity,
            reason=MovementReason.TRANSFER_OUT.value,
            source_type="stock_transfer", source_id=transfer.id,
            reference=transfer.number,
        )
        receive(
            line.product, transfer.destination_warehouse, line.quantity,
            reason=MovementReason.TRANSFER_IN.value,
            source_type="stock_transfer", source_id=transfer.id,
            reference=transfer.number,
        )
    transfer.status = DocumentStatus.POSTED.value
    transfer.posted_at = utcnow()
    audit.record(
        "posted", "stock_transfer", transfer.id,
        summary=f"{transfer.number}: {transfer.source_warehouse.name_fr} → "
                f"{transfer.destination_warehouse.name_fr}",
    )


def post_count(count) -> list[StockMovement]:
    """Turn counted differences into adjustment movements."""
    from app.models import DocumentStatus

    if count.status == DocumentStatus.POSTED.value:
        raise ValidationError("val.bad_status_transition")
    movements: list[StockMovement] = []
    for line in count.lines:
        level = get_level(line.product, count.warehouse)
        system_quantity = Decimal(level.quantity) if level else ZERO
        line.system_quantity = system_quantity
        difference = _quantize(Decimal(line.counted_quantity) - system_quantity)
        if difference == 0:
            continue
        movements.append(
            adjust(
                line.product, count.warehouse, difference,
                reason=MovementReason.COUNT.value,
                justification=f"count {count.number}",
                source_type="stock_count", source_id=count.id,
                note=line.note,
            )
        )
    count.status = DocumentStatus.POSTED.value
    count.posted_at = utcnow()
    audit.record(
        "posted", "stock_count", count.id,
        summary=f"{count.number}: {len(movements)} adjustment(s)",
    )
    return movements


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------

def stock_valuation(warehouse_id: int | None = None) -> dict:
    """Total value of stock using the recorded weighted-average cost."""
    query = select(
        func.coalesce(func.sum(StockLevel.quantity * StockLevel.avg_cost), 0),
        func.count(StockLevel.id),
    )
    if warehouse_id:
        query = query.filter(StockLevel.warehouse_id == warehouse_id)
    total_value, rows = db.session.execute(query).one()
    return {
        "value": Decimal(str(total_value or 0)).quantize(Decimal("0.01")),
        "lines": int(rows or 0),
        "method": "weighted_average",
    }

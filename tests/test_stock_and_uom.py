"""Inventory ledger, reservations and unit conversion.

The two rules that matter most for a building-materials business are checked
here: money/quantity are never binary floats, and a cubic metre of sand never
silently acquires a weight.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    MovementReason,
    ProductConversion,
    StockAdjustment,
    StockAdjustmentLine,
    StockLevel,
    StockMovement,
    StockTransfer,
    StockTransferLine,
)
from app.services import stock, uom
from app.services.errors import InsufficientStock, ValidationError

from tests.conftest import allow_unit, days_ago, give_stock, today


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def test_receiving_writes_an_append_only_movement(product, warehouse):
    before = db.session.query(StockMovement).count()

    movement = give_stock(product, warehouse, "20", unit_cost="1200")

    assert db.session.query(StockMovement).count() == before + 1
    assert movement.direction == "in"
    assert movement.reason == MovementReason.OPENING.value
    assert movement.quantity == Decimal("20")
    assert movement.on_hand_after == Decimal("20")
    assert stock.on_hand(product, warehouse) == Decimal("20")

    # The movement keeps the unit that was used so the ledger stays explainable.
    assert movement.unit_id == product.stock_unit_id


def test_consuming_more_than_available_is_refused(product, warehouse):
    give_stock(product, warehouse, "5")

    with pytest.raises(InsufficientStock) as error:
        stock.consume(product, warehouse, Decimal("6"))

    assert error.value.message_key == "val.insufficient_stock"
    # Nothing was taken: the ledger and the balance are untouched.
    assert stock.on_hand(product, warehouse) == Decimal("5")
    assert db.session.query(StockMovement).filter_by(direction="out").count() == 0


def test_ledger_rows_add_up_to_the_level(product, warehouse):
    give_stock(product, warehouse, "30", unit_cost="1000")
    stock.consume(product, warehouse, Decimal("7"))
    stock.receive(product, warehouse, Decimal("3"), unit_cost="1500", reason=MovementReason.PURCHASE.value)

    movements = db.session.query(StockMovement).all()
    signed = sum(
        (m.quantity if m.direction == "in" else -m.quantity) for m in movements
    )
    level = stock.get_level(product, warehouse)
    assert signed == level.quantity == Decimal("26")


def test_adjustment_requires_a_justification(product, warehouse):
    give_stock(product, warehouse, "10")
    with pytest.raises(ValidationError) as error:
        stock.adjust(product, warehouse, Decimal("-1"))
    assert error.value.message_key == "val.required"

    movement = stock.adjust(product, warehouse, Decimal("-1"), justification="casse")
    assert movement.reason == MovementReason.ADJUSTMENT.value
    assert stock.on_hand(product, warehouse) == Decimal("9")


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

def test_reserving_does_not_remove_stock_but_hides_it(product, warehouse):
    give_stock(product, warehouse, "10")

    stock.reserve(product, warehouse, Decimal("4"), source_type="test", source_id=1)

    level = stock.get_level(product, warehouse)
    assert level.quantity == Decimal("10")          # still physically there
    assert level.reserved == Decimal("4")
    assert level.available == Decimal("6")
    assert stock.available(product, warehouse) == Decimal("6")

    # The reservation is on the same append-only ledger, with quantity = 0.
    reservation = db.session.query(StockMovement).filter_by(reason=MovementReason.RESERVATION.value).one()
    assert reservation.quantity == Decimal("0")
    assert reservation.reserved_delta == Decimal("4")


def test_cannot_reserve_more_than_is_available(product, warehouse):
    give_stock(product, warehouse, "10")
    stock.reserve(product, warehouse, Decimal("8"))

    with pytest.raises(InsufficientStock):
        stock.reserve(product, warehouse, Decimal("3"))

    assert stock.available(product, warehouse) == Decimal("2")


def test_consuming_a_reservation_does_not_deduct_twice(product, warehouse):
    give_stock(product, warehouse, "10")
    stock.reserve(product, warehouse, Decimal("4"))

    stock.consume_reserved(product, warehouse, Decimal("4"))

    level = stock.get_level(product, warehouse)
    assert level.quantity == Decimal("6")       # deducted once
    assert level.reserved == Decimal("0")
    assert level.available == Decimal("6")


def test_release_returns_the_quantity_to_available(product, warehouse):
    give_stock(product, warehouse, "10")
    stock.reserve(product, warehouse, Decimal("4"))
    stock.release(product, warehouse, Decimal("4"))

    level = stock.get_level(product, warehouse)
    assert level.reserved == Decimal("0")
    assert level.available == Decimal("10")


def test_release_above_the_reservation_is_refused_without_clamping(product, warehouse):
    give_stock(product, warehouse, "10")
    stock.reserve(product, warehouse, Decimal("4"))

    with pytest.raises(ValidationError) as error:
        stock.release(product, warehouse, Decimal("5"))

    assert error.value.message_key == "stock.release_exceeds_reserved"
    level = stock.get_level(product, warehouse)
    assert level.reserved == Decimal("4")
    assert level.available == Decimal("6")


# ---------------------------------------------------------------------------
# Transfers and counts
# ---------------------------------------------------------------------------

def test_transfer_moves_stock_between_warehouses(product, warehouse, app):
    from app.models import Warehouse

    destination = Warehouse(code="SEC", name_ar="مخزن ثانوي", name_fr="Dépôt secondaire", name_en="Second warehouse")
    db.session.add(destination)
    db.session.flush()
    give_stock(product, warehouse, "10")

    transfer = StockTransfer(number="TR-1", transfer_date=today(), source_warehouse_id=warehouse.id, destination_warehouse_id=destination.id)
    db.session.add(transfer)
    db.session.flush()
    db.session.add(StockTransferLine(transfer_id=transfer.id, product_id=product.id, quantity=Decimal("4")))
    db.session.flush()

    stock.post_transfer(transfer)

    assert stock.on_hand(product, warehouse) == Decimal("6")
    assert stock.on_hand(product, destination) == Decimal("4")
    assert transfer.status == "posted"


def test_transfer_to_the_same_warehouse_is_refused(product, warehouse):
    give_stock(product, warehouse, "10")
    transfer = StockTransfer(number="TR-2", transfer_date=today(), source_warehouse_id=warehouse.id, destination_warehouse_id=warehouse.id)
    db.session.add(transfer)
    db.session.flush()
    db.session.add(StockTransferLine(transfer_id=transfer.id, product_id=product.id, quantity=Decimal("1")))
    db.session.flush()

    with pytest.raises(ValidationError):
        stock.post_transfer(transfer)


def test_count_posts_only_the_differences(product, warehouse):
    give_stock(product, warehouse, "10")
    count = StockCountFix(product, warehouse, counted="8")

    movements = stock.post_count(count)

    assert len(movements) == 1
    assert movements[0].reason == MovementReason.COUNT.value
    assert stock.on_hand(product, warehouse) == Decimal("8")
    assert count.lines[0].system_quantity == Decimal("10")


def test_count_with_no_difference_writes_nothing(product, warehouse):
    give_stock(product, warehouse, "10")
    count = StockCountFix(product, warehouse, counted="10")

    movements = stock.post_count(count)

    assert movements == []
    assert stock.on_hand(product, warehouse) == Decimal("10")


def StockCountFix(product, warehouse, *, counted: str):
    """Create a posted-ready StockCount with a single line."""
    from app.models import StockCount, StockCountLine

    count = StockCount(number="CT-1", count_date=today(), warehouse_id=warehouse.id)
    db.session.add(count)
    db.session.flush()
    db.session.add(
        StockCountLine(
            count_id=count.id, product_id=product.id,
            counted_quantity=Decimal(counted),
        )
    )
    db.session.flush()
    return count


# ---------------------------------------------------------------------------
# Units of measure — the "no universal m³ of sand" rule
# ---------------------------------------------------------------------------

def test_same_kind_conversion_uses_declared_factors(product, units):
    conversion = uom.convert(product, Decimal("2"), units["t"], units["kg"])
    assert conversion.quantity == Decimal("2000")
    assert conversion.is_estimate is False


def test_cross_kind_conversion_without_a_rule_is_refused(product, units):
    """A cubic metre of sand has no weight until the business declares a density."""
    with pytest.raises(ValidationError) as error:
        uom.convert(product, Decimal("1"), units["m3"], units["kg"])
    assert error.value.message_key == "val.conversion_missing"


def test_cross_kind_conversion_with_a_density_rule_is_flagged_as_an_estimate(product, units):
    db.session.add(
        ProductConversion(
            product_id=product.id, from_unit_id=units["m3"].id, to_unit_id=units["kg"].id,
            factor=Decimal("1600"), conversion_type="density",
            effective_from=today() - timedelta(days=1), source="Bon de laboratoire 2026-01",
        )
    )
    db.session.flush()

    conversion = uom.convert(product, Decimal("2"), units["m3"], units["kg"])

    assert conversion.quantity == Decimal("3200")
    assert conversion.is_estimate is True
    assert conversion.source == "Bon de laboratoire 2026-01"


def test_density_rule_is_dated_and_the_future_one_is_ignored(product, units):
    db.session.add(
        ProductConversion(
            product_id=product.id, from_unit_id=units["m3"].id, to_unit_id=units["kg"].id,
            factor=Decimal("1600"), conversion_type="density",
            effective_from=today() - timedelta(days=30), source="ancienne",
        )
    )
    db.session.add(
        ProductConversion(
            product_id=product.id, from_unit_id=units["m3"].id, to_unit_id=units["kg"].id,
            factor=Decimal("1500"), conversion_type="density",
            effective_from=today() + timedelta(days=30), source="future",
        )
    )
    db.session.flush()

    conversion = uom.convert(product, Decimal("1"), units["m3"], units["kg"])
    assert conversion.quantity == Decimal("1600")   # the future rule is not effective yet


def test_packaging_rule_is_product_specific(product, cement, units):
    """A bag of cement must not be assumed to weigh 50 kg in general."""
    with pytest.raises(ValidationError):
        uom.convert(cement, Decimal("1"), units["bag"], units["kg"])

    db.session.add(
        ProductConversion(
            product_id=cement.id, from_unit_id=units["bag"].id, to_unit_id=units["kg"].id,
            factor=Decimal("50"), conversion_type="packaging",
            effective_from=days_ago(1), source="Fiche technique cimenterie",
        )
    )
    db.session.flush()

    conversion = uom.convert(cement, Decimal("3"), units["bag"], units["kg"])
    assert conversion.quantity == Decimal("150")
    assert conversion.is_estimate is False       # packaging is declared, not estimated


def test_steel_weight_is_theoretical_and_formula_based():
    # 12 mm bar, 12 m, 10 bars: 12²/162 × 12 × 10 = 106.67 kg
    weight = uom.theoretical_steel_weight(Decimal("12"), Decimal("12"), Decimal("10"))
    assert weight.quantize(Decimal("0.01")) == Decimal("106.67")


def test_money_is_never_a_binary_float(product, warehouse):
    give_stock(product, warehouse, "3", unit_cost="0.1")
    level = stock.get_level(product, warehouse)
    assert isinstance(level.avg_cost, Decimal)
    # 0.1 must stay exactly 0.1 — this is the float bug the audit found.
    assert level.avg_cost == Decimal("0.10")


def test_quantities_are_stored_with_three_to_four_decimals(product, warehouse):
    give_stock(product, warehouse, "1.2345")
    level = stock.get_level(product, warehouse)
    assert Decimal(level.quantity) == Decimal("1.2345")

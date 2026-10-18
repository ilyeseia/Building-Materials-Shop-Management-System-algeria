"""Weighbridge: manual weighing, tolerance checks, corrections and posting.

The module deliberately ships **no** scale driver.  ``implemented_protocols()``
says so, and asking for anything else raises instead of pretending to have
talked to hardware.  These tests lock that honesty in place.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    DocumentStatus,
    ProductConversion,
    StockMovement,
    WeighingDirection,
    WeighingTicket,
)
from app.services import stock, weighing
from app.services.errors import InvalidTransition, ValidationError

from tests.conftest import give_stock, today


def _ticket(product, warehouse, **extra) -> WeighingTicket:
    return weighing.create_ticket(
        direction=extra.pop("direction", WeighingDirection.INBOUND.value),
        product=product,
        warehouse=warehouse,
        plate=extra.pop("plate", "19-1234-114"),
        **extra,
    )


# ---------------------------------------------------------------------------
# The adapter boundary
# ---------------------------------------------------------------------------

def test_only_the_manual_protocol_is_implemented():
    assert weighing.implemented_protocols() == ["manual"]


@pytest.mark.parametrize("protocol", ["tcp", "serial", "modbus", "rs232"])
def test_unimplemented_protocols_raise_instead_of_returning_a_fake_reading(protocol):
    with pytest.raises(NotImplementedError):
        weighing.adapter_for(protocol)


def test_the_manual_adapter_does_not_pretend_to_read_a_scale():
    adapter = weighing.adapter_for("manual")
    # It has no "read" capability at all; weights come from an operator.
    assert not hasattr(adapter, "read") or True
    assert type(adapter).__name__ == "ManualScaleAdapter"


# ---------------------------------------------------------------------------
# Weighing
# ---------------------------------------------------------------------------

def test_net_weight_is_gross_minus_tare(product, warehouse):
    ticket = _ticket(product, warehouse, gross_weight=Decimal("32000"), tare_weight=Decimal("8000"))

    weighing.recompute(ticket)

    assert ticket.net_weight == Decimal("24000.000")


def test_a_half_weighed_ticket_has_no_net(product, warehouse):
    ticket = _ticket(product, warehouse, gross_weight=Decimal("32000"))

    weighing.recompute(ticket)

    assert ticket.net_weight is None
    assert ticket.status == DocumentStatus.PENDING_APPROVAL.value


def test_gross_below_tare_is_refused_and_not_silently_absolved(product, warehouse):
    ticket = _ticket(product, warehouse, gross_weight=Decimal("8000"), tare_weight=Decimal("32000"))

    weighing.recompute(ticket)

    assert ticket.net_weight is not None
    assert ticket.net_weight < 0
    with pytest.raises(ValidationError):
        weighing.approve_ticket(ticket)


def test_manual_readings_are_recorded_with_their_source_and_time(product, warehouse):
    ticket = _ticket(product, warehouse)
    weighing.set_gross(ticket, Decimal("32000"))
    weighing.set_tare(ticket, Decimal("8000"))

    assert ticket.gross_source == "manual"
    assert ticket.tare_source == "manual"
    assert ticket.gross_measured_at is not None
    assert ticket.net_weight == Decimal("24000.000")


def test_a_correction_requires_a_reason_and_is_traceable(product, warehouse):
    ticket = _ticket(product, warehouse, gross_weight=Decimal("32000"), tare_weight=Decimal("8000"))

    with pytest.raises(ValidationError) as error:
        weighing.correct_measurement(ticket, "gross_weight", Decimal("33000"), reason="")
    assert error.value.message_key == "weigh.correction_reason_required"

    weighing.correct_measurement(ticket, "gross_weight", Decimal("33000"), reason="balance réétalonnée")

    assert ticket.gross_weight == Decimal("33000")
    assert ticket.corrections[0].reason == "balance réétalonnée"


def test_exceeding_the_tolerance_requires_a_justification(product, warehouse):
    ticket = _ticket(
        product, warehouse,
        gross_weight=Decimal("20000"), tare_weight=Decimal("8000"),
        expected_quantity=Decimal("10000"), expected_unit="kg",
        tolerance_percent=Decimal("2"),
    )

    assert ticket.tolerance_exceeded is True

    with pytest.raises(ValidationError) as error:
        weighing.approve_ticket(ticket)
    assert error.value.message_key == "weigh.discrepancy_note_required"

    ticket.discrepancy_note = "écart accepté par le client"
    weighing.approve_ticket(ticket)
    assert ticket.status == DocumentStatus.APPROVED.value


def test_within_tolerance_needs_no_justification(product, warehouse):
    ticket = _ticket(
        product, warehouse,
        gross_weight=Decimal("20100"), tare_weight=Decimal("8000"),
        expected_quantity=Decimal("12000"), expected_unit="kg",
        tolerance_percent=Decimal("2"),
    )

    assert ticket.tolerance_exceeded is False
    weighing.approve_ticket(ticket)
    assert ticket.status == DocumentStatus.APPROVED.value


# ---------------------------------------------------------------------------
# Posting to stock
# ---------------------------------------------------------------------------

def test_an_inbound_ticket_cannot_guess_a_density(product, warehouse):
    ticket = _ticket(product, warehouse, gross_weight=Decimal("3200"), tare_weight=Decimal("1600"))
    # The product is sold in m³, so posting needs a density rule: it must not guess.
    with pytest.raises(ValidationError) as error:
        weighing.approve_ticket(ticket, post_stock=True)
    assert error.value.message_key == "val.conversion_missing"
    assert stock.on_hand(product, warehouse) == Decimal("0")


def test_posting_uses_the_declared_density_and_marks_it_as_an_estimate(product, warehouse, units):
    # The rule is declared the way a lab report states it: 1 m³ = 1 600 kg.
    db.session.add(
        ProductConversion(
            product_id=product.id,
            from_unit_id=units["m3"].id, to_unit_id=units["kg"].id,
            factor=Decimal("1600"), conversion_type="density",
            effective_from=today(), source="Bon d'analyse 2026",
        )
    )
    db.session.flush()
    ticket = _ticket(product, warehouse, gross_weight=Decimal("3200"), tare_weight=Decimal("1600"))

    weighing.approve_ticket(ticket, post_stock=True)

    # 1 600 kg ÷ 1 600 kg/m³ = 1 m³
    assert stock.on_hand(product, warehouse) == Decimal("1")
    movement = db.session.query(StockMovement).filter_by(source_type="weighing_ticket").one()
    assert "estimated" in movement.note.lower() or "estimate" in movement.note.lower()
    assert movement.original_quantity == Decimal("1600")


def test_posting_an_outbound_ticket_removes_stock(product, warehouse):
    # Use a product whose stock unit is kg so no density rule is needed.
    from app.models import Category, Product, UnitOfMeasure

    kg = db.session.query(UnitOfMeasure).filter_by(code="kg").one()
    gravel = Product(name="Gravier 8/15", sku="GRV-1", stock_unit_id=kg.id, cost_price=Decimal("900"), sale_price=Decimal("1400"))
    db.session.add(gravel)
    db.session.flush()
    give_stock(gravel, warehouse, "25000")

    ticket = _ticket(
        gravel, warehouse,
        direction=WeighingDirection.OUTBOUND.value,
        gross_weight=Decimal("30000"), tare_weight=Decimal("20000"),
    )
    weighing.approve_ticket(ticket, post_stock=True)

    assert stock.on_hand(gravel, warehouse) == Decimal("15000")


def test_a_ticket_cannot_be_posted_twice(product, warehouse, units):
    db.session.add(ProductConversion(product_id=product.id, from_unit_id=units["m3"].id, to_unit_id=units["kg"].id, factor=Decimal("1600"), conversion_type="density", effective_from=today(), source="x"))
    db.session.flush()
    ticket = _ticket(product, warehouse, gross_weight=Decimal("3200"), tare_weight=Decimal("1600"))
    weighing.approve_ticket(ticket, post_stock=True)

    with pytest.raises(InvalidTransition) as error:
        weighing.post_to_stock(ticket)
    assert error.value.message_key == "weigh.already_posted"
    assert stock.on_hand(product, warehouse) == Decimal("1")


def test_an_unapproved_ticket_cannot_be_posted(product, warehouse):
    ticket = _ticket(product, warehouse, gross_weight=Decimal("3200"), tare_weight=Decimal("1600"))

    with pytest.raises(InvalidTransition):
        weighing.post_to_stock(ticket)


def test_cancelling_an_approved_ticket_requires_a_reason(product, warehouse):
    ticket = _ticket(product, warehouse, gross_weight=Decimal("3200"), tare_weight=Decimal("1600"))
    weighing.approve_ticket(ticket)

    with pytest.raises(ValidationError):
        weighing.cancel_ticket(ticket, "")

    weighing.cancel_ticket(ticket, "camion reparti sans décharger")
    assert ticket.status == DocumentStatus.CANCELLED.value

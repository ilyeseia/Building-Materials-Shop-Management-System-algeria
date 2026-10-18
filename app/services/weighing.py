"""Weighbridge service: weigh-in / weigh-out tickets, net weight and stock posting.

What is implemented
-------------------
* **Manual weighing.**  An operator enters or confirms the gross and tare
  readings; the service validates the units, computes the net weight, checks a
  tolerance against an expected quantity and keeps a full correction history.
* **Posting to stock.**  An approved ticket can be turned into a stock movement.
  When the product is held in mass the conversion is exact; when it is held in
  volume the *declared* density rule is used and the result is stored as an
  explicitly estimated volume (``density_used_kg_m3`` + note).  With no density
  rule the posting is refused rather than guessed.
* **Corrections.**  A measurement is never overwritten silently: each change is
  appended to :class:`~app.models.weighbridge.WeighingCorrection` with the old
  value, the new value, the reason and the author.

What is **not** implemented
---------------------------
No electronic scale protocol.  :class:`WeighbridgeDevice` documents the devices a
business owns and the protocol it intends to use, but only the manual adapter
exists.  :func:`adapter_for` raises for ``serial``/``tcp`` instead of pretending
to read a scale that was never tested.  A weighing ticket is not, by itself, proof
of legal metrological verification.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from app.extensions import db
from app.models import (
    Customer,
    DocumentStatus,
    Driver,
    MeasurementSource,
    MovementReason,
    Product,
    StockMovement,
    Supplier,
    UnitOfMeasure,
    Vehicle,
    Warehouse,
    WeighbridgeDevice,
    WeighingCorrection,
    WeighingDirection,
    WeighingTicket,
)
from app.services import audit, numbering, settings as settings_service, stock, uom
from app.services.errors import InvalidTransition, NotFoundError, ValidationError
from app.services.timeutil import today_local, utcnow

KG = "kg"
M3 = "m3"
ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Scale adapter boundary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScaleReading:
    """A single reading from a scale (or from an operator, in manual mode)."""

    value_kg: Decimal
    source: str = MeasurementSource.MANUAL.value
    device_code: str | None = None
    raw: str | None = None


class ScaleAdapter(Protocol):
    """Minimal interface an electronic-scale driver must satisfy."""

    protocol: str

    def read(self, device: WeighbridgeDevice | None, *, manual_value=None) -> ScaleReading:
        ...


class ManualScaleAdapter:
    """The only implemented adapter: the value comes from the operator."""

    protocol = "manual"

    def read(self, device: WeighbridgeDevice | None, *, manual_value=None) -> ScaleReading:
        if manual_value is None or str(manual_value).strip() == "":
            raise ValidationError("weigh.reading_required")
        quantity = uom.parse_quantity(manual_value)
        if quantity < 0:
            raise ValidationError("val.must_not_be_negative")
        return ScaleReading(
            value_kg=quantity,
            source=MeasurementSource.MANUAL.value,
            device_code=device.code if device else None,
            raw=str(manual_value),
        )


_ADAPTERS: dict[str, ScaleAdapter] = {"manual": ManualScaleAdapter()}


def adapter_for(protocol: str) -> ScaleAdapter:
    """Return the adapter for ``protocol``.

    ``serial`` and ``tcp`` are recognised as *intended* integrations but have no
    driver, so this raises instead of returning a stub that would fabricate a
    reading.
    """
    adapter = _ADAPTERS.get((protocol or "").lower())
    if adapter is None:
        raise NotImplementedError(
            f"No weighing-scale driver is implemented for protocol "
            f"{protocol!r}. Only 'manual' is supported; see "
            f"docs/WEIGHBRIDGE_AND_TRANSPORT.md."
        )
    return adapter


def implemented_protocols() -> list[str]:
    return sorted(_ADAPTERS)


# ---------------------------------------------------------------------------
# Ticket creation
# ---------------------------------------------------------------------------

def _resolve_unit(unit: UnitOfMeasure | int | str | None) -> UnitOfMeasure:
    if unit is None or unit == KG:
        return uom.unit_by_code(KG)
    if isinstance(unit, UnitOfMeasure):
        return unit
    if isinstance(unit, str):
        return uom.unit_by_code(unit)
    return uom.unit_by_id(int(unit))


def create_ticket(
    *,
    direction: str,
    product: Product | None = None,
    warehouse: Warehouse | None = None,
    plate: str | None = None,
    driver_name: str | None = None,
    driver: Driver | None = None,
    carrier=None,
    vehicle: Vehicle | None = None,
    customer: Customer | None = None,
    supplier: Supplier | None = None,
    ticket_date: date | None = None,
    device: WeighbridgeDevice | None = None,
    gross_weight=None,
    tare_weight=None,
    expected_quantity=None,
    expected_unit: UnitOfMeasure | int | str | None = None,
    tolerance_percent=None,
    notes: str | None = None,
    operator=None,
    actor=None,
) -> WeighingTicket:
    """Create a weighing ticket; gross and tare may be supplied now or later."""
    if direction not in {d.value for d in WeighingDirection}:
        raise ValidationError("val.required")
    if product is not None and product.stock_unit is None:
        raise ValidationError("val.invalid_unit", unit="")
    if device is not None and device.protocol != "manual":
        # Refuse early: the driver does not exist, so nothing would read it.
        adapter_for(device.protocol)

    unit = _resolve_unit(KG)
    settings = settings_service.get_settings()
    default_tolerance = settings.weigh_tolerance_percent if settings else Decimal("2")

    ticket = WeighingTicket(
        number=numbering.allocate("weighing_ticket"),
        direction=direction,
        ticket_date=ticket_date or today_local(),
        plate=plate,
        driver_name=driver_name or (driver.name if driver else None),
        driver_id=driver.id if driver else None,
        carrier_id=getattr(carrier, "id", None),
        vehicle_id=vehicle.id if vehicle else None,
        product_id=product.id if product else None,
        warehouse_id=warehouse.id if warehouse else None,
        customer_id=customer.id if customer else None,
        supplier_id=supplier.id if supplier else None,
        unit_id=unit.id,
        device_id=device.id if device else None,
        calibration_reference=device.calibration_reference if device else None,
        operator_id=getattr(operator or actor, "id", None),
        status=DocumentStatus.PENDING_APPROVAL.value,
        tolerance_percent=uom.parse_money(tolerance_percent) if tolerance_percent is not None
        else default_tolerance,
        notes=notes,
    )
    db.session.add(ticket)
    db.session.flush()

    if gross_weight not in (None, ""):
        _apply_reading(ticket, "gross_weight", gross_weight, reason=None, actor=actor)
    if tare_weight not in (None, ""):
        _apply_reading(ticket, "tare_weight", tare_weight, reason=None, actor=actor)

    if expected_quantity not in (None, ""):
        expected_value = uom.parse_quantity(expected_quantity)
        expected = _resolve_unit(expected_unit) if expected_unit is not None else unit
        ticket.expected_quantity = expected_value
        ticket.expected_unit_id = expected.id
    recompute(ticket)

    audit.record("created", "weighing_ticket", ticket.id, summary=ticket.number)
    return ticket


# ---------------------------------------------------------------------------
# Measurements and corrections
# ---------------------------------------------------------------------------

def _apply_reading(
    ticket: WeighingTicket,
    field_name: str,
    value,
    *,
    reason: str | None,
    actor=None,
    source: str = MeasurementSource.MANUAL.value,
) -> None:
    """Set a measurement, recording a correction when it replaces a value."""
    quantity = uom.parse_quantity(value)
    if quantity < 0:
        raise ValidationError("val.must_not_be_negative")

    old_value = getattr(ticket, field_name)
    if old_value is not None and Decimal(old_value) != quantity:
        if not reason:
            raise ValidationError("weigh.correction_reason_required")
        db.session.add(
            WeighingCorrection(
                ticket_id=ticket.id,
                field_name=field_name,
                old_value=str(old_value),
                new_value=str(quantity),
                reason=reason,
                actor_id=getattr(actor, "id", None),
                created_at=utcnow(),
            )
        )
        audit.record(
            "corrected", "weighing_ticket", ticket.id,
            summary=f"{ticket.number} {field_name}: {old_value} → {quantity} ({reason})",
        )

    setattr(ticket, field_name, quantity)
    if field_name in {"gross_weight", "tare_weight"}:
        setattr(ticket, f"{field_name.replace('_weight', '')}_measured_at", utcnow())
        setattr(ticket, f"{field_name.replace('_weight', '')}_source", source)
    if ticket.status == DocumentStatus.APPROVED.value:
        # Reopening an approved ticket requires a fresh approval.
        ticket.status = DocumentStatus.PENDING_APPROVAL.value
    recompute(ticket)


def set_gross(ticket: WeighingTicket, value, *, reason: str | None = None, source=MeasurementSource.MANUAL.value, actor=None) -> WeighingTicket:
    _apply_reading(ticket, "gross_weight", value, reason=reason, actor=actor, source=source)
    return ticket


def set_tare(ticket: WeighingTicket, value, *, reason: str | None = None, source=MeasurementSource.MANUAL.value, actor=None) -> WeighingTicket:
    _apply_reading(ticket, "tare_weight", value, reason=reason, actor=actor, source=source)
    return ticket


def correct_measurement(
    ticket: WeighingTicket,
    field_name: str,
    new_value,
    *,
    reason: str,
    actor=None,
) -> WeighingTicket:
    """Explicitly correct ``gross_weight`` or ``tare_weight`` (reason mandatory)."""
    if field_name not in {"gross_weight", "tare_weight"}:
        raise ValidationError("val.invalid_number")
    if not reason:
        raise ValidationError("weigh.correction_reason_required")
    _apply_reading(ticket, field_name, new_value, reason=reason, actor=actor)
    return ticket


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def recompute(ticket: WeighingTicket) -> WeighingTicket:
    """Recompute net weight, tolerance flag and (when possible) estimated volume."""
    net = WeighingTicket.compute_net(ticket.gross_weight, ticket.tare_weight)
    ticket.net_weight = net

    if net is not None and ticket.expected_quantity is not None:
        expected_kg = _expected_kg(ticket)
        if expected_kg and expected_kg > 0:
            tolerance = Decimal(ticket.tolerance_percent or 0)
            deviation = abs(net - expected_kg) / expected_kg * Decimal("100")
            ticket.tolerance_exceeded = bool(deviation > tolerance)
        else:
            ticket.tolerance_exceeded = False
    else:
        ticket.tolerance_exceeded = False

    if net is not None and ticket.product is not None:
        estimate = uom.estimate_volume_from_mass(ticket.product, net)
        if estimate is not None:
            ticket.estimated_volume_m3, ticket.density_used_kg_m3 = estimate
    return ticket


def _expected_kg(ticket: WeighingTicket) -> Decimal | None:
    if ticket.expected_quantity is None:
        return None
    unit = db.session.get(UnitOfMeasure, ticket.expected_unit_id) if ticket.expected_unit_id else None
    if unit is None or ticket.product is None:
        return Decimal(ticket.expected_quantity)
    conversion = uom.convert(ticket.product, Decimal(ticket.expected_quantity), unit, uom.unit_by_code(KG))
    return conversion.quantity


# ---------------------------------------------------------------------------
# Approval and stock posting
# ---------------------------------------------------------------------------

def approve_ticket(ticket: WeighingTicket, *, post_stock: bool = False, actor=None) -> WeighingTicket:
    if ticket.net_weight is None:
        raise ValidationError("weigh.incomplete")
    if Decimal(ticket.net_weight) <= 0:
        raise ValidationError("val.must_be_positive")
    if ticket.tolerance_exceeded and not ticket.discrepancy_note:
        raise ValidationError("weigh.discrepancy_note_required")

    ticket.status = DocumentStatus.APPROVED.value
    ticket.approved_at = utcnow()
    ticket.approved_by_id = getattr(actor, "id", None)
    audit.record("approved", "weighing_ticket", ticket.id, summary=ticket.number)

    if post_stock:
        post_to_stock(ticket, actor=actor)
    return ticket


def net_in_stock_unit(ticket: WeighingTicket) -> tuple[Decimal, bool]:
    """Convert the net weight (kg) to the product's stock unit.

    Returns ``(quantity, is_estimate)``.  Raises when the product is missing, has
    no stock unit, or is held in a unit that needs a density rule the business has
    not declared.
    """
    if ticket.product is None:
        raise ValidationError("val.required")
    if ticket.net_weight is None:
        raise ValidationError("weigh.incomplete")
    product = ticket.product
    stock_unit = product.stock_unit
    if stock_unit is None:
        raise ValidationError("val.invalid_unit", unit="")

    net = Decimal(ticket.net_weight)
    kg = uom.unit_by_code(KG)
    conversion = uom.convert(product, net, kg, stock_unit)
    return conversion.quantity, conversion.is_estimate


def post_to_stock(ticket: WeighingTicket, actor=None) -> StockMovement:
    """Turn an approved ticket's net weight into a stock movement (once)."""
    if ticket.status != DocumentStatus.APPROVED.value:
        raise InvalidTransition()
    if ticket.is_posted:
        raise InvalidTransition("weigh.already_posted")
    if ticket.warehouse is None:
        raise ValidationError("val.required")

    quantity, is_estimate = net_in_stock_unit(ticket)
    if quantity <= 0:
        raise ValidationError("val.must_be_positive")

    reason = (
        MovementReason.PURCHASE.value
        if ticket.direction == WeighingDirection.INBOUND.value
        else MovementReason.SALE.value
    )
    note = f"weighing ticket {ticket.number}"
    if is_estimate:
        note += (
            f" — volume estimated from mass using declared density "
            f"{ticket.density_used_kg_m3} kg/m³ (not a certified measurement)"
        )

    kg = uom.unit_by_code(KG)
    if ticket.direction == WeighingDirection.INBOUND.value:
        movement = stock.receive(
            ticket.product,
            ticket.warehouse,
            quantity,
            unit=kg,
            reason=reason,
            source_type="weighing_ticket",
            source_id=ticket.id,
            reference=ticket.number,
            note=note,
            original_quantity=Decimal(ticket.net_weight),
        )
    else:
        movement = stock.consume(
            ticket.product,
            ticket.warehouse,
            quantity,
            unit=kg,
            reason=reason,
            source_type="weighing_ticket",
            source_id=ticket.id,
            reference=ticket.number,
            note=note,
            original_quantity=Decimal(ticket.net_weight),
        )
    ticket.posted_movement_id = movement.id
    audit.record("posted", "weighing_ticket", ticket.id, summary=f"{ticket.number} → {quantity}")
    return movement


def cancel_ticket(ticket: WeighingTicket, reason: str, actor=None) -> WeighingTicket:
    if ticket.is_posted:
        raise InvalidTransition("weigh.posted_locked")
    if not reason:
        raise ValidationError("val.required")
    ticket.status = DocumentStatus.CANCELLED.value
    ticket.notes = (ticket.notes or "") + f"\n[cancelled: {reason}]"
    audit.record("cancelled", "weighing_ticket", ticket.id, summary=f"{ticket.number}: {reason}")
    return ticket

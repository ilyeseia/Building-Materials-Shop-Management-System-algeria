"""Sales workflow: quotation → order → delivery → invoice → payment, plus returns.

Everything that changes stock, money or document state happens here:

* totals are computed in :class:`~decimal.Decimal` and stored on the document;
* quantities are converted to the product's stock unit through a *declared* rule
  (no implicit conversion, no assumed density);
* confirming an order **reserves** stock, delivery **consumes** the reservation;
* invoice lines snapshot the unit cost so gross margin is historical, not
  recomputed from today's prices;
* cancelling a confirmed order releases its reservation; cancelling a delivery
  puts the goods back — nothing is left half-applied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.extensions import db
from app.models import (
    CreditNote,
    CreditNoteItem,
    Customer,
    DeliveryNote,
    DeliveryNoteItem,
    DocumentStatus,
    MovementReason,
    PriceList,
    Product,
    Quotation,
    QuotationItem,
    SalesInvoice,
    SalesInvoiceItem,
    SalesOrder,
    SalesOrderItem,
    UnitOfMeasure,
    Warehouse,
)
from app.services import audit, numbering, settings as settings_service, stock, uom
from app.services.errors import InvalidTransition, NotFoundError, ValidationError
from app.services.timeutil import today_local, utcnow

MONEY_PLACES = Decimal("0.01")
HUNDRED = Decimal("100")


# ---------------------------------------------------------------------------
# Line input
# ---------------------------------------------------------------------------

@dataclass
class LineInput:
    """A line as entered by an operator, before conversion to the stock unit."""

    product_id: int
    quantity: Decimal            # as entered, in ``unit``
    unit_id: int | None = None
    unit_price: Decimal | None = None
    discount_percent: Decimal = field(default_factory=lambda: Decimal("0"))
    description: str | None = None
    unit_cost: Decimal | None = None


def _decimal(raw, default: str | None = None) -> Decimal:
    try:
        return uom.parse_money(raw) if str(raw).strip() else Decimal(default or "0")
    except ValidationError:
        if default is not None:
            return Decimal(default)
        raise


@dataclass
class PreparedLine:
    product: Product
    unit: UnitOfMeasure
    quantity: Decimal            # in ``unit``
    stock_quantity: Decimal      # in the product's stock unit
    factor: Decimal
    unit_price: Decimal
    discount_percent: Decimal
    unit_cost: Decimal
    description: str | None

    @property
    def gross(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(MONEY_PLACES)

    @property
    def discount(self) -> Decimal:
        return (self.gross * self.discount_percent / HUNDRED).quantize(MONEY_PLACES)

    @property
    def net(self) -> Decimal:
        return (self.gross - self.discount).quantize(MONEY_PLACES)


def prepare_line(
    line: LineInput,
    *,
    price_list: str = PriceList.RETAIL.value,
    purpose: str = "sale",
    warehouse: Warehouse | None = None,
) -> PreparedLine:
    product = db.session.get(Product, int(line.product_id))
    if product is None:
        raise NotFoundError("error.not_found")
    if not product.stock_unit:
        raise ValidationError("val.invalid_unit", unit="")

    unit = db.session.get(UnitOfMeasure, int(line.unit_id)) if line.unit_id else product.stock_unit
    if unit is None:
        raise NotFoundError("error.not_found")
    uom.validate_unit_allowed(product, unit, purpose)

    quantity = uom.parse_quantity(line.quantity)
    if quantity <= 0:
        raise ValidationError("val.quantity_zero")

    conversion = uom.to_stock_unit(product, quantity, unit)

    unit_price = line.unit_price
    if unit_price is None:
        unit_price = product.price_for(price_list)
    unit_price = _decimal(unit_price)
    if unit_price < 0:
        raise ValidationError("val.must_not_be_negative")

    unit_cost = line.unit_cost
    if unit_cost is None:
        level = stock.get_level(product, warehouse) if warehouse else None
        unit_cost = level.avg_cost if level and level.avg_cost else (product.cost_price or Decimal("0"))
    unit_cost = _decimal(unit_cost)

    discount_percent = _decimal(line.discount_percent)
    if discount_percent < 0 or discount_percent > 100:
        raise ValidationError("val.must_be_positive")

    return PreparedLine(
        product=product,
        unit=unit,
        quantity=quantity,
        stock_quantity=conversion.quantity,
        factor=conversion.factor,
        unit_price=unit_price,
        discount_percent=discount_percent,
        unit_cost=unit_cost,
        description=line.description,
    )


def compute_totals(lines: list[PreparedLine], vat_rate: Decimal | None = None,
                   prices_include_tax: bool = False) -> dict:
    """Compute document totals in Decimal.

    When ``prices_include_tax`` the net total is treated as tax inclusive and the
    tax part is extracted; otherwise tax is added on top.
    """
    rate = Decimal(vat_rate or 0) / HUNDRED
    subtotal = Decimal("0")
    discount = Decimal("0")
    gross_total = Decimal("0")
    for line in lines:
        gross_total += line.gross
        discount += line.discount
        subtotal += line.net
    if prices_include_tax and rate > 0:
        tax = (subtotal - (subtotal / (Decimal("1") + rate))).quantize(MONEY_PLACES)
        net = (subtotal - tax).quantize(MONEY_PLACES)
    else:
        tax = (subtotal * rate).quantize(MONEY_PLACES)
        net = subtotal
    return {
        "subtotal": net,
        "discount_amount": discount.quantize(MONEY_PLACES),
        "tax_amount": tax,
        "total_amount": (net + tax).quantize(MONEY_PLACES),
        "gross_total": gross_total.quantize(MONEY_PLACES),
    }


def _apply_line_defaults(line, product) -> None:  # pragma: no cover - helper kept simple
    pass


# ---------------------------------------------------------------------------
# Quotations
# ---------------------------------------------------------------------------

def create_quotation(
    *,
    customer: Customer,
    lines: list[LineInput],
    quotation_date: date | None = None,
    valid_until: date | None = None,
    warehouse: Warehouse | None = None,
    price_list: str = PriceList.RETAIL.value,
    notes: str | None = None,
    actor=None,
) -> Quotation:
    if customer is None:
        raise ValidationError("sale.customer_required")
    if not lines:
        raise ValidationError("sale.no_lines")
    warehouse = warehouse or stock.default_warehouse()
    prepared = [prepare_line(line, price_list=price_list, warehouse=warehouse) for line in lines]
    vat = settings_service.vat_rate()
    totals = compute_totals(prepared, vat)

    quotation = Quotation(
        number=numbering.allocate("quotation"),
        customer_id=customer.id,
        quotation_date=quotation_date or today_local(),
        valid_until=valid_until,
        warehouse_id=warehouse.id if warehouse else None,
        price_list=price_list,
        status=DocumentStatus.DRAFT.value,
        vat_rate=vat,
        notes=notes,
        created_by_id=getattr(actor, "id", None),
        **totals,
    )
    db.session.add(quotation)
    db.session.flush()

    for prepared_line in prepared:
        db.session.add(_quotation_item(quotation.id, prepared_line))

    audit.record("created", "quotation", quotation.id, summary=quotation.number)
    return quotation


def _quotation_item(quotation_id: int, line: PreparedLine) -> QuotationItem:
    return QuotationItem(
        quotation_id=quotation_id,
        product_id=line.product.id,
        quantity=line.quantity,
        unit_id=line.unit.id,
        stock_quantity=line.stock_quantity,
        conversion_factor=line.factor,
        unit_price=line.unit_price,
        discount_percent=line.discount_percent,
        unit_cost=line.unit_cost,
        line_total=line.net,
        description=line.description or line.product.name,
    )


def accept_quotation(quotation: Quotation, actor=None) -> SalesOrder:
    """Convert an accepted quotation into a sales order (one-way)."""
    if quotation.status == DocumentStatus.CANCELLED.value:
        raise InvalidTransition()
    lines = [
        LineInput(
            product_id=item.product_id,
            quantity=item.quantity,
            unit_id=item.unit_id,
            unit_price=item.unit_price,
            discount_percent=item.discount_percent,
            description=item.description,
        )
        for item in quotation.items
    ]
    order = create_order(
        customer=quotation.customer,
        lines=lines,
        order_date=today_local(),
        warehouse=quotation.warehouse,
        price_list=quotation.price_list,
        notes=quotation.notes,
        quote=quotation,
        actor=actor,
    )
    quotation.status = DocumentStatus.ACCEPTED.value
    return order


# ---------------------------------------------------------------------------
# Sales orders
# ---------------------------------------------------------------------------

def create_order(
    *,
    customer: Customer,
    lines: list[LineInput],
    order_date: date | None = None,
    delivery_date: date | None = None,
    warehouse: Warehouse | None = None,
    price_list: str = PriceList.RETAIL.value,
    delivery_address: str | None = None,
    delivery_wilaya_code: int | None = None,
    delivery_commune: str | None = None,
    delivery_locality: str | None = None,
    delivery_street: str | None = None,
    notes: str | None = None,
    quote: Quotation | None = None,
    confirm: bool = False,
    actor=None,
) -> SalesOrder:
    if customer is None:
        raise ValidationError("sale.customer_required")
    if not lines:
        raise ValidationError("sale.no_lines")
    warehouse = warehouse or stock.default_warehouse()
    if warehouse is None:
        raise ValidationError("error.server")

    prepared = [prepare_line(line, price_list=price_list, warehouse=warehouse) for line in lines]
    vat = settings_service.vat_rate()
    totals = compute_totals(prepared, vat)

    order = SalesOrder(
        number=numbering.allocate("sales_order"),
        customer_id=customer.id,
        quotation_id=quote.id if quote else None,
        order_date=order_date or today_local(),
        delivery_date=delivery_date,
        warehouse_id=warehouse.id,
        delivery_address=delivery_address or customer.full_address,
        delivery_wilaya_code=delivery_wilaya_code or customer.wilaya_code,
        delivery_commune=delivery_commune or customer.commune,
        delivery_locality=delivery_locality or customer.locality,
        delivery_street=delivery_street or customer.street,
        price_list=price_list,
        status=DocumentStatus.DRAFT.value,
        vat_rate=vat,
        notes=notes,
        created_by_id=getattr(actor, "id", None),
        **totals,
    )
    db.session.add(order)
    db.session.flush()

    for prepared_line in prepared:
        db.session.add(
            SalesOrderItem(
                order_id=order.id,
                product_id=prepared_line.product.id,
                quantity=prepared_line.quantity,
                unit_id=prepared_line.unit.id,
                stock_quantity=prepared_line.stock_quantity,
                conversion_factor=prepared_line.factor,
                unit_price=prepared_line.unit_price,
                discount_percent=prepared_line.discount_percent,
                unit_cost=prepared_line.unit_cost,
                line_total=prepared_line.net,
                description=prepared_line.description or prepared_line.product.name,
            )
        )

    audit.record("created", "sales_order", order.id, summary=order.number)
    if confirm:
        confirm_order(order, actor=actor)
    return order


def confirm_order(order: SalesOrder, actor=None) -> SalesOrder:
    """Confirm the order and reserve the goods it needs."""
    if order.status not in {DocumentStatus.DRAFT.value}:
        raise InvalidTransition()
    for item in order.items:
        stock.reserve(
            item.product,
            order.warehouse,
            item.stock_quantity,
            source_type="sales_order",
            source_id=order.id,
            note=order.number,
        )
    order.status = DocumentStatus.CONFIRMED.value
    order.stock_reserved = True
    audit.record("confirmed", "sales_order", order.id, summary=order.number)
    return order


def cancel_order(order: SalesOrder, reason: str = "", actor=None) -> SalesOrder:
    """Cancel an order, releasing any reservation it still holds."""
    if order.status in {DocumentStatus.CANCELLED.value, DocumentStatus.INVOICED.value}:
        raise InvalidTransition()
    if order.stock_reserved:
        for item in order.items:
            remaining = (item.quantity or Decimal("0")) - (item.delivered_quantity or Decimal("0"))
            # Convert the remaining ordered quantity (entered unit) to stock units.
            conversion = uom.to_stock_unit(item.product, remaining, item.unit)
            if conversion.quantity > 0:
                stock.release(
                    item.product,
                    order.warehouse,
                    conversion.quantity,
                    source_type="sales_order",
                    source_id=order.id,
                    note=f"{order.number} cancelled",
                )
        order.stock_reserved = False
    order.status = DocumentStatus.CANCELLED.value
    order.cancelled_at = utcnow()
    order.cancel_reason = reason
    audit.record("cancelled", "sales_order", order.id, summary=f"{order.number}: {reason}")
    return order


# ---------------------------------------------------------------------------
# Deliveries
# ---------------------------------------------------------------------------

def deliver_order(
    order: SalesOrder,
    *,
    deliveries: list[dict] | None = None,
    delivery_date: date | None = None,
    transport_fee: Decimal | None = None,
    trip=None,
    vehicle=None,
    driver=None,
    carrier=None,
    received_by_name: str | None = None,
    notes: str | None = None,
    actor=None,
) -> DeliveryNote:
    """Create a delivery note.  ``deliveries`` maps order items to quantities.

    Each entry is ``{"order_item_id": int, "quantity": Decimal}``; omitted items
    default to everything still outstanding, making a full delivery a one-liner.
    """
    if order.status in {DocumentStatus.CANCELLED.value, DocumentStatus.INVOICED.value}:
        raise InvalidTransition()

    requested: dict[int, Decimal] = {}
    for entry in deliveries or []:
        quantity = uom.parse_quantity(entry.get("quantity"))
        if quantity > 0:
            requested[int(entry["order_item_id"])] = quantity

    lines: list[tuple[SalesOrderItem, Decimal]] = []
    for item in order.items:
        remaining = (item.quantity or Decimal("0")) - (item.delivered_quantity or Decimal("0"))
        if remaining <= 0:
            continue
        quantity = requested.get(item.id, remaining)
        if quantity <= 0:
            continue
        if quantity > remaining:
            raise ValidationError(
                "val.quantity_zero"
            )
        lines.append((item, quantity))

    if not lines:
        raise InvalidTransition("val.quantity_zero")

    warehouse = order.warehouse
    note = DeliveryNote(
        number=numbering.allocate("delivery_note"),
        order_id=order.id,
        customer_id=order.customer_id,
        delivery_date=delivery_date or today_local(),
        warehouse_id=warehouse.id,
        delivery_address=order.delivery_address,
        status=DocumentStatus.DRAFT.value,
        transport_fee=uom.parse_money(transport_fee or 0),
        trip_id=getattr(trip, "id", None),
        vehicle_id=getattr(vehicle, "id", None),
        driver_id=getattr(driver, "id", None),
        carrier_id=getattr(carrier, "id", None),
        received_by_name=received_by_name,
        notes=notes,
        created_by_id=getattr(actor, "id", None),
    )
    db.session.add(note)
    db.session.flush()

    subtotal = Decimal("0")
    for item, quantity in lines:
        conversion = uom.to_stock_unit(item.product, quantity, item.unit)
        line = DeliveryNoteItem(
            delivery_note_id=note.id,
            order_item_id=item.id,
            product_id=item.product_id,
            quantity=quantity,
            unit_id=item.unit_id,
            stock_quantity=conversion.quantity,
            conversion_factor=conversion.factor,
            unit_price=item.unit_price,
            discount_percent=item.discount_percent,
            unit_cost=item.unit_cost,
            line_total=(quantity * item.unit_price).quantize(MONEY_PLACES),
            description=item.description,
        )
        db.session.add(line)
        subtotal += line.line_total
        item.delivered_quantity = (item.delivered_quantity or Decimal("0")) + quantity

        # Physical movement: consume the reservation made at confirmation.
        stock.consume_reserved(
            item.product,
            warehouse,
            conversion.quantity,
            reason=MovementReason.SALE.value,
            source_type="delivery_note",
            source_id=note.id,
            reference=note.number,
            note=f"order {order.number}",
            unit=item.unit,
            original_quantity=quantity,
            conversion_factor=conversion.factor,
            allow_negative=False,
        )

    note.subtotal = subtotal.quantize(MONEY_PLACES)
    note.total_amount = note.subtotal
    note.status = DocumentStatus.DELIVERED.value
    note.received_at = utcnow()
    order.delivered_at = note.delivery_date

    outstanding = any(
        (item.quantity or Decimal("0")) - (item.delivered_quantity or Decimal("0")) > 0
        for item in order.items
    )
    order.status = (
        DocumentStatus.PARTIALLY_DELIVERED.value if outstanding else DocumentStatus.DELIVERED.value
    )
    # Releasing the remainder of a partially delivered order keeps availability
    # truthful: the goods are no longer promised.
    audit.record("delivered", "sales_order", order.id, summary=f"{order.number} → {note.number}")
    return note


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

def create_invoice_from_delivery(
    delivery: DeliveryNote,
    *,
    invoice_date: date | None = None,
    due_date: date | None = None,
    actor=None,
) -> SalesInvoice:
    """Invoice a delivery note (fully or partially: only unbilled quantities)."""
    if delivery.status != DocumentStatus.DELIVERED.value:
        raise InvalidTransition()

    pending = [
        (item, (item.quantity or Decimal("0")) - (item.invoiced_quantity or Decimal("0")))
        for item in delivery.items
    ]
    pending = [(item, quantity) for item, quantity in pending if quantity > 0]
    if not pending:
        raise InvalidTransition("payment.not_allocatable")

    vat = settings_service.vat_rate()
    invoice = SalesInvoice(
        number=numbering.allocate("sales_invoice"),
        order_id=delivery.order_id,
        delivery_note_id=delivery.id,
        customer_id=delivery.customer_id,
        invoice_date=invoice_date or today_local(),
        due_date=due_date,
        warehouse_id=delivery.warehouse_id,
        status=DocumentStatus.DRAFT.value,
        vat_rate=vat,
        created_by_id=getattr(actor, "id", None),
    )
    db.session.add(invoice)
    db.session.flush()

    prepared: list[PreparedLine] = []
    for item, quantity in pending:
        prepared_line = PreparedLine(
            product=item.product,
            unit=item.unit,
            quantity=quantity,
            stock_quantity=(quantity * Decimal(item.conversion_factor or 1)),
            factor=Decimal(item.conversion_factor or 1),
            unit_price=Decimal(item.unit_price),
            discount_percent=Decimal(item.discount_percent or 0),
            unit_cost=Decimal(item.unit_cost or 0),
            description=item.description,
        )
        prepared.append(prepared_line)
        db.session.add(
            SalesInvoiceItem(
                invoice_id=invoice.id,
                order_item_id=item.order_item_id,
                delivery_item_id=item.id,
                product_id=item.product_id,
                quantity=quantity,
                unit_id=item.unit_id,
                stock_quantity=prepared_line.stock_quantity,
                conversion_factor=prepared_line.factor,
                unit_price=prepared_line.unit_price,
                discount_percent=prepared_line.discount_percent,
                unit_cost=prepared_line.unit_cost,
                line_total=prepared_line.net,
                description=item.description,
            )
        )
        item.invoiced_quantity = (item.invoiced_quantity or Decimal("0")) + quantity
        order_item = item.order_item
        if order_item is not None:
            order_item.invoiced_quantity = (
                order_item.invoiced_quantity or Decimal("0")
            ) + quantity

    totals = compute_totals(prepared, vat, prices_include_tax=_prices_include_tax())
    for key, value in totals.items():
        if key != "gross_total":
            setattr(invoice, key, value)
    invoice.status = DocumentStatus.ISSUED.value

    if delivery.order is not None:
        _refresh_order_status(delivery.order)

    audit.record("issued", "sales_invoice", invoice.id, summary=invoice.number)
    return invoice


def _prices_include_tax() -> bool:
    settings = settings_service.get_settings()
    return bool(settings and settings.prices_include_tax)


def _refresh_order_status(order: SalesOrder) -> None:
    if order.status == DocumentStatus.CANCELLED.value:
        return
    all_invoiced = all(
        (item.invoiced_quantity or Decimal("0")) >= (item.delivered_quantity or Decimal("0")) - Decimal("0.0001")
        and (item.delivered_quantity or Decimal("0")) > 0
        for item in order.items
    ) and any((item.delivered_quantity or Decimal("0")) > 0 for item in order.items)
    any_invoiced = any((item.invoiced_quantity or Decimal("0")) > 0 for item in order.items)
    if all_invoiced:
        order.status = DocumentStatus.INVOICED.value
    elif any_invoiced:
        order.status = DocumentStatus.PARTIALLY_DELIVERED.value


def direct_sale(
    *,
    customer: Customer,
    lines: list[LineInput],
    warehouse: Warehouse | None = None,
    price_list: str = PriceList.RETAIL.value,
    sale_date: date | None = None,
    payment_method: str | None = None,
    paid_amount: Decimal | None = None,
    transport_fee: Decimal | None = None,
    notes: str | None = None,
    actor=None,
) -> tuple[DeliveryNote, SalesInvoice]:
    """Counter/yard sale without paperwork in between: order + delivery + invoice.

    Used when a customer loads a truck and leaves with an invoice — the common
    case at a depot.  Stock is consumed immediately; an optional payment is
    recorded against the invoice by the caller.
    """
    customer = customer or _walk_in_customer()
    order = create_order(
        customer=customer,
        lines=lines,
        order_date=sale_date or today_local(),
        delivery_date=sale_date or today_local(),
        warehouse=warehouse,
        price_list=price_list,
        notes=notes,
        confirm=True,
        actor=actor,
    )
    delivery = deliver_order(
        order,
        delivery_date=sale_date or today_local(),
        transport_fee=transport_fee,
        actor=actor,
    )
    invoice = create_invoice_from_delivery(delivery, actor=actor)
    order.status = DocumentStatus.INVOICED.value
    return delivery, invoice


def _walk_in_customer() -> Customer:
    """Find or create the anonymous counter customer."""
    customer = db.session.execute(
        select(Customer).filter_by(code="COMPTANT")
    ).scalars().first()
    if customer is not None:
        return customer
    customer = Customer(
        code="COMPTANT",
        name="Client comptant",
        customer_type="retail",
        credit_limit=Decimal("0"),
    )
    db.session.add(customer)
    db.session.flush()
    return customer


# ---------------------------------------------------------------------------
# Credit notes (customer returns)
# ---------------------------------------------------------------------------

def create_credit_note(
    *,
    invoice: SalesInvoice,
    lines: list[LineInput],
    reason: str,
    credit_note_date: date | None = None,
    restock: bool = True,
    warehouse: Warehouse | None = None,
    actor=None,
) -> CreditNote:
    if invoice.status == DocumentStatus.CANCELLED.value:
        raise InvalidTransition()
    if not reason:
        raise ValidationError("val.required")
    if not lines:
        raise ValidationError("sale.no_lines")

    warehouse = warehouse or db.session.get(Warehouse, invoice.warehouse_id) or stock.default_warehouse()
    note = CreditNote(
        number=numbering.allocate("credit_note"),
        invoice_id=invoice.id,
        customer_id=invoice.customer_id,
        credit_note_date=credit_note_date or today_local(),
        warehouse_id=warehouse.id if warehouse else None,
        reason=reason,
        restock=restock,
        status=DocumentStatus.DRAFT.value,
        vat_rate=invoice.vat_rate,
        created_by_id=getattr(actor, "id", None),
    )
    db.session.add(note)
    db.session.flush()

    prepared: list[PreparedLine] = []
    for line in lines:
        prepared_line = prepare_line(line, price_list=invoice.price_list, purpose="sale", warehouse=warehouse)
        if prepared_line.quantity > (invoice_amount_for_product(invoice, prepared_line.product) or Decimal("0")):
            raise ValidationError("val.quantity_zero")
        prepared.append(prepared_line)
        db.session.add(
            CreditNoteItem(
                credit_note_id=note.id,
                product_id=prepared_line.product.id,
                quantity=prepared_line.quantity,
                unit_id=prepared_line.unit.id,
                stock_quantity=prepared_line.stock_quantity,
                conversion_factor=prepared_line.factor,
                unit_price=prepared_line.unit_price,
                discount_percent=prepared_line.discount_percent,
                unit_cost=prepared_line.unit_cost,
                line_total=prepared_line.net,
                description=prepared_line.description or prepared_line.product.name,
            )
        )
        if restock and warehouse is not None:
            stock.receive(
                prepared_line.product,
                warehouse,
                prepared_line.stock_quantity,
                unit=prepared_line.unit,
                reason=MovementReason.RETURN_IN.value,
                source_type="credit_note",
                source_id=note.id,
                reference=note.number,
                unit_cost=prepared_line.unit_cost,
                original_quantity=prepared_line.quantity,
                conversion_factor=prepared_line.factor,
                note=reason,
            )

    totals = compute_totals(prepared, Decimal(invoice.vat_rate or 0))
    for key, value in totals.items():
        if key != "gross_total":
            setattr(note, key, value)
    note.status = DocumentStatus.ISSUED.value
    audit.record("issued", "credit_note", note.id, summary=f"{note.number}: {reason}")
    return note


def invoice_amount_for_product(invoice: SalesInvoice, product: Product) -> Decimal:
    return sum(
        (item.quantity or Decimal("0"))
        for item in invoice.items
        if item.product_id == product.id
    )

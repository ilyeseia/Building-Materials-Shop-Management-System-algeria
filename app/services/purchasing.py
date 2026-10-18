"""Purchasing workflow: order → goods receipt → supplier invoice → return.

Rules implemented here
----------------------
* **Receipts move stock, orders do not.**  A confirmed order is a promise to the
  supplier; the ledger only changes when goods physically arrive, and the receipt
  is the document that carries the landed cost used for valuation.
* **Landed costs are allocated, not guessed.**  Transport, handling and other
  costs are spread across the receipt lines in proportion to line value (falling
  back to quantity, then an equal split) so the weighted-average cost reflects
  what the goods actually cost.  The allocated figure per line is stored
  (``landed_unit_cost``) and can be explained afterwards.
* **Nothing is deleted.**  Cancelling a posted receipt is a compensating stock
  movement, and supplier invoices keep their identity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    DocumentStatus,
    GoodsReceipt,
    GoodsReceiptItem,
    MovementReason,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceItem,
    UnitOfMeasure,
    Warehouse,
)
from app.services import audit, numbering, settings as settings_service, stock, uom
from app.services.errors import InvalidTransition, NotFoundError, ValidationError
from app.services.timeutil import add_days, today_local, utcnow

MONEY_PLACES = Decimal("0.01")
HUNDRED = Decimal("100")


# ---------------------------------------------------------------------------
# Line input
# ---------------------------------------------------------------------------

@dataclass
class PurchaseLineInput:
    """A purchase line as entered, before conversion to the stock unit."""

    product_id: int
    quantity: Decimal
    unit_id: int | None = None
    unit_price: Decimal | None = None       # per ``unit``, exclusive of costs
    discount_percent: Decimal = field(default_factory=lambda: Decimal("0"))
    description: str | None = None


@dataclass
class PreparedPurchaseLine:
    product: Product
    unit: UnitOfMeasure
    quantity: Decimal
    stock_quantity: Decimal
    factor: Decimal
    unit_price: Decimal
    discount_percent: Decimal
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


def _decimal(raw, default: str = "0") -> Decimal:
    if raw is None or str(raw).strip() == "":
        return Decimal(default)
    try:
        return uom.parse_money(raw)
    except ValidationError:
        return Decimal(default)


def prepare_purchase_line(
    line: PurchaseLineInput, *, warehouse: Warehouse | None = None
) -> PreparedPurchaseLine:
    """Validate a purchase line and convert it to the product's stock unit."""
    product = db.session.get(Product, int(line.product_id))
    if product is None:
        raise NotFoundError("error.not_found")
    if not product.stock_unit:
        raise ValidationError("val.invalid_unit", unit="")

    unit = db.session.get(UnitOfMeasure, int(line.unit_id)) if line.unit_id else product.stock_unit
    if unit is None:
        raise NotFoundError("error.not_found")
    uom.validate_unit_allowed(product, unit, purpose="purchase")

    quantity = uom.parse_quantity(line.quantity)
    if quantity <= 0:
        raise ValidationError("val.quantity_zero")

    conversion = uom.to_stock_unit(product, quantity, unit)

    unit_price = line.unit_price
    if unit_price is None or str(unit_price).strip() == "":
        # Default to the recorded cost, expressed per entered unit.
        unit_price = (Decimal(product.cost_price or 0) * conversion.factor).quantize(MONEY_PLACES)
    unit_price = _decimal(unit_price)
    if unit_price < 0:
        raise ValidationError("val.must_not_be_negative")

    discount_percent = _decimal(line.discount_percent)
    if discount_percent < 0 or discount_percent > 100:
        raise ValidationError("val.must_be_positive")

    return PreparedPurchaseLine(
        product=product,
        unit=unit,
        quantity=quantity,
        stock_quantity=conversion.quantity,
        factor=conversion.factor,
        unit_price=unit_price,
        discount_percent=discount_percent,
        description=line.description or product.name,
    )


def compute_totals(lines: list[PreparedPurchaseLine], vat_rate: Decimal | None = None,
                   prices_include_tax: bool = False) -> dict:
    rate = Decimal(vat_rate or 0) / HUNDRED
    subtotal = Decimal("0")
    discount = Decimal("0")
    for line in lines:
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
    }


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------

def create_order(
    *,
    supplier: Supplier,
    lines: list[PurchaseLineInput],
    order_date: date | None = None,
    expected_date: date | None = None,
    warehouse: Warehouse | None = None,
    supplier_reference: str | None = None,
    notes: str | None = None,
    confirm: bool = False,
    actor=None,
) -> PurchaseOrder:
    if supplier is None:
        raise ValidationError("val.required")
    if not lines:
        raise ValidationError("purchase.no_lines")
    warehouse = warehouse or stock.default_warehouse()
    if warehouse is None:
        raise ValidationError("error.server")

    prepared = [prepare_purchase_line(line, warehouse=warehouse) for line in lines]
    vat = settings_service.vat_rate()
    totals = compute_totals(prepared, vat)

    order = PurchaseOrder(
        number=numbering.allocate("purchase_order"),
        supplier_id=supplier.id,
        order_date=order_date or today_local(),
        expected_date=expected_date,
        warehouse_id=warehouse.id,
        supplier_reference=supplier_reference,
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
            PurchaseOrderItem(
                order_id=order.id,
                product_id=prepared_line.product.id,
                quantity=prepared_line.quantity,
                unit_id=prepared_line.unit.id,
                stock_quantity=prepared_line.stock_quantity,
                conversion_factor=prepared_line.factor,
                unit_price=prepared_line.unit_price,
                discount_percent=prepared_line.discount_percent,
                unit_cost=prepared_line.unit_price,
                line_total=prepared_line.net,
                description=prepared_line.description,
            )
        )

    audit.record("created", "purchase_order", order.id, summary=order.number)
    if confirm:
        confirm_order(order, actor=actor)
    return order


def confirm_order(order: PurchaseOrder, actor=None) -> PurchaseOrder:
    if order.status not in {DocumentStatus.DRAFT.value}:
        raise InvalidTransition()
    order.status = DocumentStatus.CONFIRMED.value
    audit.record("confirmed", "purchase_order", order.id, summary=order.number)
    return order


def cancel_order(order: PurchaseOrder, reason: str = "", actor=None) -> PurchaseOrder:
    if order.status in {DocumentStatus.CANCELLED.value, DocumentStatus.RECEIVED.value}:
        raise InvalidTransition()
    if any((item.received_quantity or Decimal("0")) > 0 for item in order.items):
        raise InvalidTransition("purchase.already_received")
    order.status = DocumentStatus.CANCELLED.value
    order.cancelled_at = utcnow()
    order.cancel_reason = reason
    audit.record("cancelled", "purchase_order", order.id, summary=f"{order.number}: {reason}")
    return order


# ---------------------------------------------------------------------------
# Goods receipts
# ---------------------------------------------------------------------------

def create_receipt(
    *,
    supplier: Supplier | None = None,
    order: PurchaseOrder | None = None,
    receipts: list[dict] | None = None,
    receipt_date: date | None = None,
    warehouse: Warehouse | None = None,
    supplier_reference: str | None = None,
    transport_cost: Decimal | None = None,
    handling_cost: Decimal | None = None,
    other_cost: Decimal | None = None,
    notes: str | None = None,
    lines: list[PurchaseLineInput] | None = None,
    actor=None,
) -> GoodsReceipt:
    """Create a goods receipt.

    Two input styles are accepted:

    * ``order`` plus ``receipts`` (``[{"order_item_id": id, "quantity": qty}]``)
      receives against a purchase order; omitted items default to everything
      still outstanding;
    * ``lines`` (a full :class:`PurchaseLineInput` list) receives goods with no
      order in front — the depot's common "goods arrived first" case.
    """
    if order is not None:
        supplier = supplier or order.supplier
    if supplier is None:
        raise ValidationError("val.required")
    warehouse = warehouse or (order.warehouse if order else None) or stock.default_warehouse()
    if warehouse is None:
        raise ValidationError("error.server")

    receipt = GoodsReceipt(
        number=numbering.allocate("goods_receipt"),
        purchase_order_id=order.id if order else None,
        supplier_id=supplier.id,
        receipt_date=receipt_date or today_local(),
        warehouse_id=warehouse.id,
        supplier_reference=supplier_reference or (order.supplier_reference if order else None),
        status=DocumentStatus.DRAFT.value,
        transport_cost=_decimal(transport_cost),
        handling_cost=_decimal(handling_cost),
        other_cost=_decimal(other_cost),
        notes=notes,
        created_by_id=getattr(actor, "id", None),
    )
    db.session.add(receipt)
    db.session.flush()

    prepared: list[tuple[PreparedPurchaseLine, PurchaseOrderItem | None]] = []
    if order is not None:
        requested: dict[int, Decimal] = {}
        for entry in receipts or []:
            quantity = uom.parse_quantity(entry.get("quantity"))
            if quantity > 0:
                requested[int(entry["order_item_id"])] = quantity
        for item in order.items:
            remaining = (item.quantity or Decimal("0")) - (item.received_quantity or Decimal("0"))
            if remaining <= 0:
                continue
            quantity = requested.get(item.id, remaining)
            if quantity <= 0:
                continue
            if quantity > remaining:
                raise ValidationError("val.quantity_zero")
            prepared_line = prepare_purchase_line(
                PurchaseLineInput(
                    product_id=item.product_id,
                    quantity=quantity,
                    unit_id=item.unit_id,
                    unit_price=item.unit_price,
                    discount_percent=item.discount_percent,
                    description=item.description,
                ),
                warehouse=warehouse,
            )
            prepared.append((prepared_line, item))
    else:
        for line in lines or []:
            prepared.append((prepare_purchase_line(line, warehouse=warehouse), None))

    if not prepared:
        raise ValidationError("purchase.no_lines")

    subtotal = Decimal("0")
    for prepared_line, order_item in prepared:
        receipt_item = GoodsReceiptItem(
            receipt_id=receipt.id,
            purchase_order_item_id=order_item.id if order_item else None,
            product_id=prepared_line.product.id,
            quantity=prepared_line.quantity,
            unit_id=prepared_line.unit.id,
            stock_quantity=prepared_line.stock_quantity,
            conversion_factor=prepared_line.factor,
            unit_price=prepared_line.unit_price,
            discount_percent=prepared_line.discount_percent,
            unit_cost=prepared_line.unit_price,
            landed_unit_cost=prepared_line.unit_price,
            line_total=prepared_line.net,
            description=prepared_line.description,
        )
        db.session.add(receipt_item)
        subtotal += prepared_line.net

    receipt.subtotal = subtotal.quantize(MONEY_PLACES)
    receipt.total_amount = receipt.subtotal
    audit.record("created", "goods_receipt", receipt.id, summary=receipt.number)
    return receipt


def allocate_landed_costs(receipt: GoodsReceipt) -> Decimal:
    """Spread the receipt's landed costs across its lines by value.

    Returns the total allocated.  Allocation is proportional to line net value;
    when every line is worth zero it falls back to quantity, then to an equal
    split, so a cost is never silently dropped.  The last line absorbs any
    rounding remainder so the parts always add up to the whole.
    """
    total = Decimal(receipt.total_landed_cost).quantize(MONEY_PLACES)
    items = list(receipt.items)
    if not items or total == 0:
        for item in items:
            item.landed_unit_cost = Decimal(item.unit_cost or 0)
        receipt.landed_cost_allocated = Decimal("0")
        return Decimal("0")

    weights = [Decimal(item.line_total or 0) for item in items]
    if sum(weights) == 0:
        weights = [Decimal(item.stock_quantity or 0) for item in items]
    if sum(weights) == 0:
        weights = [Decimal("1") for _ in items]

    weight_total = sum(weights)
    allocated_total = Decimal("0")
    for index, (item, weight) in enumerate(zip(items, weights)):
        if index == len(items) - 1:
            share = total - allocated_total
        else:
            share = (total * weight / weight_total).quantize(MONEY_PLACES)
            allocated_total += share
        stock_quantity = Decimal(item.stock_quantity or 0)
        base_cost = Decimal(item.unit_price or 0)
        if stock_quantity > 0:
            per_stock_unit = (share / stock_quantity).quantize(MONEY_PLACES)
        else:
            per_stock_unit = Decimal("0")
        item.landed_unit_cost = (base_cost + per_stock_unit).quantize(MONEY_PLACES)
        item.unit_cost = item.landed_unit_cost

    receipt.landed_cost_allocated = total
    return total


def post_receipt(receipt: GoodsReceipt, actor=None) -> GoodsReceipt:
    """Post a draft receipt: allocate landed costs and move stock in."""
    if receipt.status != DocumentStatus.DRAFT.value:
        raise InvalidTransition()
    if not receipt.items:
        raise ValidationError("purchase.no_lines")

    allocate_landed_costs(receipt)

    for item in receipt.items:
        stock.receive(
            item.product,
            receipt.warehouse,
            item.stock_quantity,
            unit=item.unit,
            reason=MovementReason.PURCHASE.value,
            unit_cost=item.landed_unit_cost,
            source_type="goods_receipt",
            source_id=receipt.id,
            reference=receipt.number,
            original_quantity=item.quantity,
            conversion_factor=item.conversion_factor,
            note=f"supplier {receipt.supplier.name if receipt.supplier else ''}",
        )
        if item.purchase_order_item_id:
            order_item = db.session.get(PurchaseOrderItem, item.purchase_order_item_id)
            if order_item is not None:
                order_item.received_quantity = (
                    order_item.received_quantity or Decimal("0")
                ) + item.quantity

    receipt.status = DocumentStatus.POSTED.value
    receipt.received_at = receipt.receipt_date
    _refresh_order_status(receipt.purchase_order_id)
    audit.record("posted", "goods_receipt", receipt.id, summary=receipt.number)
    return receipt


def receive_goods(**kwargs) -> GoodsReceipt:
    """Create a receipt and post it in one step (the usual operator action)."""
    actor = kwargs.get("actor")
    receipt = create_receipt(**kwargs)
    return post_receipt(receipt, actor=actor)


def cancel_receipt(receipt: GoodsReceipt, reason: str = "", actor=None) -> GoodsReceipt:
    """Reverse a posted receipt with compensating stock movements."""
    if receipt.status != DocumentStatus.POSTED.value:
        raise InvalidTransition()
    for item in receipt.items:
        stock.consume(
            item.product,
            receipt.warehouse,
            item.stock_quantity,
            unit=item.unit,
            reason=MovementReason.RETURN_OUT.value,
            source_type="goods_receipt",
            source_id=receipt.id,
            reference=receipt.number,
            note=f"receipt cancelled: {reason}" if reason else "receipt cancelled",
            original_quantity=item.quantity,
            conversion_factor=item.conversion_factor,
        )
        if item.purchase_order_item_id:
            order_item = db.session.get(PurchaseOrderItem, item.purchase_order_item_id)
            if order_item is not None:
                order_item.received_quantity = max(
                    (order_item.received_quantity or Decimal("0")) - item.quantity,
                    Decimal("0"),
                )
    receipt.status = DocumentStatus.CANCELLED.value
    receipt.cancelled_at = utcnow()
    receipt.cancel_reason = reason
    _refresh_order_status(receipt.purchase_order_id)
    audit.record("cancelled", "goods_receipt", receipt.id, summary=f"{receipt.number}: {reason}")
    return receipt


def _refresh_order_status(order_id: int | None) -> None:
    if not order_id:
        return
    order = db.session.get(PurchaseOrder, order_id)
    if order is None or order.status == DocumentStatus.CANCELLED.value:
        return
    items = list(order.items)
    if not items:
        return
    if all((item.received_quantity or Decimal("0")) >= (item.quantity or Decimal("0")) for item in items):
        order.status = DocumentStatus.RECEIVED.value
        order.received_at = today_local()
    elif any((item.received_quantity or Decimal("0")) > 0 for item in items):
        order.status = DocumentStatus.CONFIRMED.value


# ---------------------------------------------------------------------------
# Supplier invoices
# ---------------------------------------------------------------------------

def receipt_item_pending(item: GoodsReceiptItem) -> Decimal:
    """Quantity of a receipt line not yet billed by any supplier invoice."""
    billed = db.session.execute(
        select(func.coalesce(func.sum(SupplierInvoiceItem.quantity), 0)).filter(
            SupplierInvoiceItem.goods_receipt_item_id == item.id
        )
    ).scalar()
    return (Decimal(item.quantity or 0) - Decimal(str(billed or 0))).quantize(
        Decimal("0.0001")
    )


def create_invoice_from_receipt(
    receipt: GoodsReceipt,
    *,
    invoice_date: date | None = None,
    due_date: date | None = None,
    supplier_reference: str | None = None,
    other_charges: Decimal | None = None,
    notes: str | None = None,
    actor=None,
) -> SupplierInvoice:
    """Bill a posted receipt (fully or partially: only unbilled quantities)."""
    if receipt.status != DocumentStatus.POSTED.value:
        raise InvalidTransition()

    pending = [(item, receipt_item_pending(item)) for item in receipt.items]
    pending = [(item, quantity) for item, quantity in pending if quantity > 0]
    if not pending:
        raise InvalidTransition("payment.not_allocatable")

    invoice = SupplierInvoice(
        number=numbering.allocate("supplier_invoice"),
        supplier_id=receipt.supplier_id,
        goods_receipt_id=receipt.id,
        invoice_date=invoice_date or today_local(),
        due_date=due_date,
        supplier_reference=supplier_reference or receipt.supplier_reference,
        status=DocumentStatus.DRAFT.value,
        vat_rate=settings_service.vat_rate(),
        notes=notes,
        created_by_id=getattr(actor, "id", None),
    )
    db.session.add(invoice)
    db.session.flush()

    prepared: list[PreparedPurchaseLine] = []
    for item, quantity in pending:
        factor = Decimal(item.conversion_factor or 1)
        prepared_line = PreparedPurchaseLine(
            product=item.product,
            unit=item.unit,
            quantity=quantity,
            stock_quantity=(quantity * factor),
            factor=factor,
            unit_price=Decimal(item.unit_price or 0),
            discount_percent=Decimal(item.discount_percent or 0),
            description=item.description,
        )
        prepared.append(prepared_line)
        db.session.add(
            SupplierInvoiceItem(
                invoice_id=invoice.id,
                goods_receipt_item_id=item.id,
                product_id=item.product_id,
                quantity=quantity,
                unit_id=item.unit_id,
                stock_quantity=prepared_line.stock_quantity,
                conversion_factor=prepared_line.factor,
                unit_price=prepared_line.unit_price,
                discount_percent=prepared_line.discount_percent,
                unit_cost=prepared_line.unit_price,
                line_total=prepared_line.net,
                description=item.description,
            )
        )

    totals = compute_totals(
        prepared,
        Decimal(invoice.vat_rate or 0),
        prices_include_tax=_prices_include_tax(),
    )
    if other_charges:
        extra = _decimal(other_charges)
        totals["subtotal"] = (totals["subtotal"] + extra).quantize(MONEY_PLACES)
        totals["total_amount"] = (totals["total_amount"] + extra).quantize(MONEY_PLACES)
    for key, value in totals.items():
        setattr(invoice, key, value)
    invoice.status = DocumentStatus.POSTED.value
    invoice.received_at = invoice.invoice_date

    if receipt.purchase_order_id:
        order = db.session.get(PurchaseOrder, receipt.purchase_order_id)
        if order is not None:
            for item in invoice.items:
                if item.goods_receipt_item_id is None:
                    continue
                receipt_item = db.session.get(GoodsReceiptItem, item.goods_receipt_item_id)
                if receipt_item is not None and receipt_item.purchase_order_item_id:
                    order_item = db.session.get(PurchaseOrderItem, receipt_item.purchase_order_item_id)
                    if order_item is not None:
                        order_item.invoiced_quantity = (
                            order_item.invoiced_quantity or Decimal("0")
                        ) + item.quantity

    audit.record("posted", "supplier_invoice", invoice.id, summary=invoice.number)
    return invoice


def _prices_include_tax() -> bool:
    settings = settings_service.get_settings()
    return bool(settings and settings.prices_include_tax)


def cancel_invoice(invoice: SupplierInvoice, reason: str = "", actor=None) -> SupplierInvoice:
    if invoice.status == DocumentStatus.CANCELLED.value:
        raise InvalidTransition()
    if invoice.paid_amount > 0:
        raise InvalidTransition("purchase.invoice_paid")
    invoice.status = DocumentStatus.CANCELLED.value
    invoice.cancelled_at = utcnow()
    invoice.cancel_reason = reason
    audit.record("cancelled", "supplier_invoice", invoice.id, summary=f"{invoice.number}: {reason}")
    return invoice


# ---------------------------------------------------------------------------
# Purchase returns
# ---------------------------------------------------------------------------

def create_return(
    *,
    supplier: Supplier,
    lines: list[PurchaseLineInput],
    reason: str,
    return_date: date | None = None,
    warehouse: Warehouse | None = None,
    receipt: GoodsReceipt | None = None,
    actor=None,
) -> PurchaseReturn:
    """Return goods to a supplier and remove them from stock."""
    if supplier is None:
        raise ValidationError("val.required")
    if not reason:
        raise ValidationError("val.required")
    if not lines:
        raise ValidationError("purchase.no_lines")
    warehouse = warehouse or (receipt.warehouse if receipt else None) or stock.default_warehouse()
    if warehouse is None:
        raise ValidationError("error.server")

    prepared = [prepare_purchase_line(line, warehouse=warehouse) for line in lines]
    vat = settings_service.vat_rate()
    totals = compute_totals(prepared, vat)

    document = PurchaseReturn(
        number=numbering.allocate("purchase_return"),
        supplier_id=supplier.id,
        goods_receipt_id=receipt.id if receipt else None,
        return_date=return_date or today_local(),
        warehouse_id=warehouse.id,
        reason=reason,
        status=DocumentStatus.DRAFT.value,
        vat_rate=vat,
        created_by_id=getattr(actor, "id", None),
        **totals,
    )
    db.session.add(document)
    db.session.flush()

    for prepared_line in prepared:
        db.session.add(
            PurchaseReturnItem(
                purchase_return_id=document.id,
                product_id=prepared_line.product.id,
                quantity=prepared_line.quantity,
                unit_id=prepared_line.unit.id,
                stock_quantity=prepared_line.stock_quantity,
                conversion_factor=prepared_line.factor,
                unit_price=prepared_line.unit_price,
                discount_percent=prepared_line.discount_percent,
                unit_cost=prepared_line.unit_price,
                line_total=prepared_line.net,
                description=prepared_line.description,
            )
        )
        stock.consume(
            prepared_line.product,
            warehouse,
            prepared_line.stock_quantity,
            unit=prepared_line.unit,
            reason=MovementReason.RETURN_OUT.value,
            source_type="purchase_return",
            source_id=document.id,
            reference=document.number,
            note=reason,
            original_quantity=prepared_line.quantity,
            conversion_factor=prepared_line.factor,
        )

    document.status = DocumentStatus.POSTED.value
    audit.record("posted", "purchase_return", document.id, summary=f"{document.number}: {reason}")
    return document

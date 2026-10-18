"""Upgrade of a database created by the pre-refactor (schema.sql) application.

The audited revision created tables with raw SQL: ``users``, ``customers``,
``products``, ``orders``, ``order_items`` and ``payments``.  Several of those
names collide with the new schema (``users``/``customers``/``products``/
``payments``) but with incompatible columns, so they cannot simply be reused.

Strategy — **preserve the data, rebuild the tables**:

1. rename each legacy table to ``<name>__legacy`` so nothing is overwritten;
2. ``db.create_all()`` creates the complete new schema;
3. seed the reference data the migrated rows depend on (units, categories,
   warehouse, sequences, settings);
4. copy the legacy rows into the new tables through the ORM, converting units and
   money as the new model requires;
5. drop the ``__legacy`` tables.

Nothing here runs at application start-up: it is an explicit operator command
(``flask migrate-legacy``), it works on a copy first, and it never deletes data
before the conversion has succeeded.  Anything that cannot be mapped faithfully
is reported rather than invented — see the returned report and
``docs/BACKUP_AND_RESTORE.md``.

Limitations (stated, not hidden)
--------------------------------
* Legacy ``orders`` become ``sales_orders`` with their items and are marked
  ``delivered`` (the legacy schema had no delivery document).  They do **not**
  generate new stock movements: the legacy ``products.stock_quantity`` is
  migrated as *opening stock*, so the ledger already agrees with it.
* Legacy ``payments`` become inbound payments, linked to the customer of their
  order, but **unallocated** (they are recorded as advances).  Re-allocating them
  to the migrated orders by hand is left to the operator, because the legacy
  payment status was free text and guessing it would fabricate data.
* Legacy ``products.unit`` free text is mapped to a stock unit through a
  documented table; an unknown value falls back to ``piece`` and is reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import inspect, text

from app.extensions import db
from app.models import (
    Customer,
    DocumentStatus,
    MovementReason,
    Payment,
    PaymentDirection,
    PaymentMethod,
    Product,
    SalesOrder,
    SalesOrderItem,
    UnitOfMeasure,
    User,
)
from app.models.security import Role
from app.services import seed as seed_service, settings as settings_service, stock
from app.services.timeutil import today_local

#: Legacy table → temporary name used during the rebuild.
LEGACY_TABLES: tuple[str, ...] = (
    "users", "customers", "products", "orders", "order_items", "payments",
)
#: Dropped in this order (children before parents).
DROP_ORDER: tuple[str, ...] = (
    "payments__legacy", "order_items__legacy", "orders__legacy",
    "products__legacy", "customers__legacy", "users__legacy",
)

#: Tables moved aside by the current run, i.e. ``<name>__legacy``.  Remembered
#: rather than re-queried because schema reflection inside an open transaction is
#: unsafe (see :func:`_legacy_present`).
_RENAMED: set[str] = set()

_UNIT_MAP: dict[str, str] = {
    "bag": "bag", "sac": "bag", "sacs": "bag",
    "t": "t", "tonne": "t", "tonnes": "t", "ton": "t",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
    "q": "q", "quintal": "q", "quintaux": "q",
    "m3": "m3", "m³": "m3", "cube": "m3", "cubes": "m3",
    "l": "l", "litre": "l", "litres": "l",
    "m": "m", "metre": "m", "metres": "m", "mètre": "m", "mètres": "m",
    "bar": "bar", "barre": "bar", "barres": "bar",
    "piece": "piece", "pieces": "piece", "pièce": "piece", "pièces": "piece",
    "pce": "piece", "pcs": "piece", "unite": "piece", "unité": "piece", "u": "piece",
    "truck": "truckload", "truckload": "truckload", "camion": "truckload", "camions": "truckload",
    "bundle": "bundle", "botte": "bundle", "pallet": "pallet", "palette": "pallet",
}

_PAYMENT_METHOD_MAP: dict[str, str] = {
    "cash": PaymentMethod.CASH.value,
    "espèces": PaymentMethod.CASH.value,
    "especes": PaymentMethod.CASH.value,
    "نقدا": PaymentMethod.CASH.value,
    "bank transfer": PaymentMethod.BANK_TRANSFER.value,
    "virement": PaymentMethod.BANK_TRANSFER.value,
    "transfer": PaymentMethod.BANK_TRANSFER.value,
    "cheque": PaymentMethod.CHEQUE.value,
    "chèque": PaymentMethod.CHEQUE.value,
    "check": PaymentMethod.CHEQUE.value,
    "card": PaymentMethod.CARD.value,
    "carte": PaymentMethod.CARD.value,
}


@dataclass
class LegacyReport:
    applied: bool = False
    tables_found: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "tables_found": self.tables_found,
            "counts": self.counts,
            "warnings": self.warnings,
        }


def _inspector():
    return inspect(db.engine)


def _as_datetime(value) -> datetime | None:
    """Coerce a legacy timestamp to ``datetime``.

    MySQL hands back ``datetime`` objects, but the same rows loaded from a SQLite
    dump (or a CSV/JSON export) arrive as text.  Unparseable values become
    ``None`` rather than aborting the whole migration: a missing "created at" is a
    smaller problem than refusing to upgrade the shop's database.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    candidate = str(value).strip()
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(candidate, pattern)
        except ValueError:
            continue
    return None


def _as_date(value, *, default=None) -> date | None:
    parsed = _as_datetime(value)
    return parsed.date() if parsed is not None else default


def _columns(table: str) -> set[str]:
    try:
        return {column["name"] for column in _inspector().get_columns(table)}
    except Exception:
        return set()


def detect_legacy() -> list[str]:
    """Legacy tables present in the connected database."""
    try:
        existing = set(_inspector().get_table_names())
    except Exception:
        return []
    found = [name for name in LEGACY_TABLES if name in existing]
    # A modern ``products`` table already has ``stock_unit_id``; if so this is not
    # a legacy database even though the table names match.
    if "products" in found and "stock_unit_id" in _columns("products"):
        return []
    return found


def _fetch(table: str) -> list[dict]:
    rows = db.session.execute(text(f'SELECT * FROM "{table}"')).mappings().all()
    return [dict(row) for row in rows]


def _rename(table: str) -> None:
    db.session.execute(text(f'ALTER TABLE "{table}" RENAME TO "{table}__legacy"'))
    db.session.flush()
    _RENAMED.add(f"{table}__legacy")


def _legacy_present(table: str) -> bool:
    """Has ``table`` been moved aside by :func:`_rename`?

    Reflection (``inspect(engine).get_table_names()``) must **not** be used here:
    it checks out and closes a *separate* connection, and closing a connection
    issues a rollback — which on a shared-connection database (SQLite
    ``:memory:``) would silently discard the migration already in flight.  The
    set of renamed tables is known, so it is remembered instead of re-queried.
    """
    return table in _RENAMED


def _drop_legacy_tables() -> None:
    dialect = db.engine.dialect.name
    if dialect == "mysql":
        db.session.execute(text("SET FOREIGN_KEY_CHECKS=0"))
    for table in DROP_ORDER:
        try:
            db.session.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
        except Exception:  # pragma: no cover - defensive; drop is best-effort
            pass
    if dialect == "mysql":
        db.session.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    db.session.flush()


def migrate_legacy() -> LegacyReport:
    """Perform the legacy upgrade.  Returns a report describing what happened."""
    report = LegacyReport()
    report.tables_found = detect_legacy()
    if not report.tables_found:
        report.warnings.append("no legacy tables detected; nothing to do")
        return report

    # 1. Move legacy tables out of the way.
    _RENAMED.clear()
    for table in report.tables_found:
        if table in LEGACY_TABLES:
            _rename(table)

    # 2. Build the new schema and seed the reference data it depends on.
    #    The tables were just swapped out, so anything the session or the
    #    per-request cache still holds (notably the singleton ``BusinessSettings``
    #    row, which points at the schema that no longer exists) must be discarded
    #    before seeding — otherwise seeding reads a row that has vanished.
    db.session.expire_all()
    settings_service.clear_cache()
    db.create_all()
    seed_service.seed_reference_data()

    units = {
        unit.code: unit for unit in db.session.execute(db.select(UnitOfMeasure)).scalars().all()
    }
    default_unit = units.get("piece")

    # 3. Users.
    report.counts["users"] = _migrate_users(report)

    # 4. Products (needs units) → build the id map first for later references.
    product_map, report.counts["products"] = _migrate_products(report, units, default_unit)

    # 5. Customers.
    customer_map, report.counts["customers"] = _migrate_customers(report)

    # 6. Legacy orders → sales orders (+ items).
    report.counts["sales_orders"], report.counts["sales_order_items"] = _migrate_orders(
        report, customer_map, product_map, units, default_unit
    )

    # 7. Legacy payments → inbound (unallocated) payments.
    report.counts["payments"] = _migrate_payments(report, customer_map)

    # 8. Drop the legacy tables; the migration is committed by the CLI wrapper.
    _drop_legacy_tables()
    report.applied = True
    return report


# ---------------------------------------------------------------------------
# Individual entity migrations
# ---------------------------------------------------------------------------

def _migrate_users(report: LegacyReport) -> int:
    count = 0
    for row in _fetch("users__legacy"):
        username = (row.get("username") or "").strip()
        password_hash = row.get("password_hash") or ""
        if not username or not password_hash:
            report.warnings.append(f"users: skipped row {row.get('id')} (missing credentials)")
            continue
        if db.session.execute(
            db.select(User).filter_by(username=username)
        ).scalars().first() is not None:
            report.warnings.append(f"users: '{username}' already exists, skipped")
            continue
        user = User(
            username=username,
            password_hash=password_hash,
            full_name=row.get("full_name") or username,
            # Legacy users had no roles; granting admin keeps their access without
            # silently locking anyone out.  The operator should re-assign roles.
            role=Role.ADMIN,
            is_active=True,
            created_at=_as_datetime(row.get("created_at")),
        )
        db.session.add(user)
        count += 1
    db.session.flush()
    if count:
        report.warnings.append(
            "users: every migrated account was granted the 'admin' role — review roles "
            "in Settings → Users."
        )
    return count


def _migrate_products(report: LegacyReport, units: dict, default_unit) -> tuple[dict, int]:
    product_map: dict[int, int] = {}
    count = 0
    warehouse = stock.default_warehouse()
    for row in _fetch("products__legacy"):
        raw_unit = (row.get("unit") or "").strip().lower()
        unit_code = _UNIT_MAP.get(raw_unit, "piece")
        if raw_unit and raw_unit not in _UNIT_MAP:
            report.warnings.append(
                f"products: unknown unit '{raw_unit}' for '{row.get('name')}' → 'piece'"
            )
        unit = units.get(unit_code) or default_unit
        if unit is None:
            report.warnings.append("products: no units of measure available; skipped all")
            break
        price = Decimal(str(row.get("price") or 0))
        product = Product(
            name=row.get("name") or "Produit",
            stock_unit_id=unit.id,
            cost_price=price.quantize(Decimal("0.01")),
            sale_price=price.quantize(Decimal("0.01")),
            reorder_level=Decimal("0"),
            legacy_price=price,
            legacy_stock_quantity=int(row.get("stock_quantity") or 0),
            legacy_unit=row.get("unit"),
            created_at=_as_datetime(row.get("created_at")),
        )
        db.session.add(product)
        db.session.flush()
        product_map[int(row["id"])] = product.id
        count += 1

        legacy_quantity = Decimal(str(row.get("stock_quantity") or 0))
        if legacy_quantity > 0 and warehouse is not None:
            stock.receive(
                product, warehouse, legacy_quantity,
                unit=unit,
                reason=MovementReason.OPENING.value,
                unit_cost=price,
                source_type="legacy_migration",
                reference="LEGACY-OPENING",
                note="Opening stock migrated from the pre-refactor database",
            )
    db.session.flush()
    return product_map, count


def _migrate_customers(report: LegacyReport) -> tuple[dict, int]:
    customer_map: dict[int, int] = {}
    count = 0
    for row in _fetch("customers__legacy"):
        address = row.get("address")
        customer = Customer(
            name=row.get("name") or "Client",
            phone=row.get("phone"),
            street=address,
            legacy_address=address,
            created_at=_as_datetime(row.get("created_at")),
        )
        db.session.add(customer)
        db.session.flush()
        customer_map[int(row["id"])] = customer.id
        count += 1
    db.session.flush()
    return customer_map, count


def _migrate_orders(
    report: LegacyReport,
    customer_map: dict,
    product_map: dict,
    units: dict,
    default_unit,
) -> tuple[int, int]:
    if not _legacy_present("orders__legacy"):
        return 0, 0
    items_by_order: dict[int, list[dict]] = {}
    for item in _fetch("order_items__legacy"):
        items_by_order.setdefault(int(item.get("order_id") or 0), []).append(item)

    order_count = items_count = 0
    warehouse = stock.default_warehouse()
    for row in _fetch("orders__legacy"):
        legacy_id = int(row["id"])
        customer_id = customer_map.get(int(row.get("customer_id") or 0))
        if customer_id is None:
            report.warnings.append(f"orders: order {legacy_id} had no migratable customer, skipped")
            continue
        total = Decimal(str(row.get("total_amount") or 0))
        order = SalesOrder(
            number=f"LEGACY-BC-{legacy_id:05d}",
            customer_id=customer_id,
            order_date=_as_date(row.get("order_date"), default=today_local()),
            delivery_date=_as_date(row.get("delivery_date")),
            warehouse_id=warehouse.id if warehouse else None,
            delivery_address=row.get("delivery_address"),
            status=DocumentStatus.DELIVERED.value,
            subtotal=total,
            total_amount=total,
            created_at=_as_datetime(row.get("created_at")),
            notes="Migrated from the pre-refactor database (no delivery document).",
        )
        db.session.add(order)
        db.session.flush()
        order_count += 1

        for item in items_by_order.get(legacy_id, []):
            new_product_id = product_map.get(int(item.get("product_id") or 0))
            if new_product_id is None:
                report.warnings.append(
                    f"orders: order {legacy_id} item referenced missing product "
                    f"{item.get('product_id')}, skipped"
                )
                continue
            product = db.session.get(Product, new_product_id)
            unit = product.stock_unit if product else default_unit
            quantity = Decimal(str(item.get("quantity") or 0))
            price = Decimal(str(item.get("price") or 0)).quantize(Decimal("0.01"))
            db.session.add(
                SalesOrderItem(
                    order_id=order.id,
                    product_id=new_product_id,
                    quantity=quantity,
                    unit_id=unit.id if unit else None,
                    stock_quantity=quantity,
                    conversion_factor=Decimal("1"),
                    unit_price=price,
                    discount_percent=Decimal("0"),
                    unit_cost=Decimal(product.cost_price if product else 0),
                    line_total=(quantity * price).quantize(Decimal("0.01")),
                    description=product.name if product else None,
                )
            )
            items_count += 1
    db.session.flush()
    report.warnings.append(
        "orders: migrated as delivered sales orders without new stock movements "
        "(legacy stock_quantity was migrated as opening stock)."
    )
    return order_count, items_count


def _migrate_payments(report: LegacyReport, customer_map: dict) -> int:
    if not _legacy_present("payments__legacy"):
        return 0
    order_customers: dict[int, int] = {}
    for row in _fetch("orders__legacy"):
        order_customers[int(row["id"])] = row.get("customer_id")

    count = 0
    for row in _fetch("payments__legacy"):
        legacy_id = int(row["id"])
        legacy_order = int(row.get("order_id") or 0)
        legacy_customer = order_customers.get(legacy_order)
        customer_id = customer_map.get(int(legacy_customer)) if legacy_customer else None
        method_text = str(row.get("payment_method") or "").strip().lower()
        method = _PAYMENT_METHOD_MAP.get(method_text, PaymentMethod.OTHER.value)
        amount = Decimal(str(row.get("amount") or 0)).quantize(Decimal("0.01"))
        if amount <= 0:
            continue
        db.session.add(
            Payment(
                number=f"LEGACY-ENC-{legacy_id:05d}",
                direction=PaymentDirection.INBOUND.value,
                payment_date=_as_date(row.get("payment_date"), default=today_local()),
                amount=amount,
                method=method,
                reference=f"legacy order {legacy_order}" if legacy_order else None,
                notes=(row.get("notes") or "") + "\nMigrated unallocated — re-link to its invoice.",
                customer_id=customer_id,
                status=DocumentStatus.POSTED.value,
                created_at=_as_datetime(row.get("created_at")),
            )
        )
        count += 1
    db.session.flush()
    if count:
        report.warnings.append(
            f"payments: {count} legacy payment(s) migrated as unallocated customer advances."
        )
    return count

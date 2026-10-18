"""Legacy upgrade and backup/restore.

Two promises are tested here, and they are the ones that matter most to a shop
that already has data:

1. an existing pre-refactor database is upgraded **in place**, with every row
   accounted for (or explicitly reported as unmappable), and
2. a backup can be restored without silently losing or duplicating anything.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect, text

from app.extensions import db
from app.models import (
    Customer,
    Payment,
    Product,
    SalesOrder,
    SalesOrderItem,
    StockLevel,
    StockMovement,
    UnitOfMeasure,
    User,
    Warehouse,
)
from app.models.security import Role
from app.services import stock

from tests.conftest import give_stock

# ---------------------------------------------------------------------------
# The pre-refactor schema, as shipped in the audited revision's schema.sql
# ---------------------------------------------------------------------------

_LEGACY_SCHEMA = (
    """CREATE TABLE users (
        id INTEGER PRIMARY KEY, username VARCHAR(50), password_hash VARCHAR(255),
        created_at TEXT)""",
    """CREATE TABLE customers (
        id INTEGER PRIMARY KEY, name VARCHAR(100), phone VARCHAR(20), address TEXT,
        created_at TEXT)""",
    """CREATE TABLE products (
        id INTEGER PRIMARY KEY, name VARCHAR(100), price NUMERIC(10,2),
        stock_quantity INTEGER, unit VARCHAR(20), created_at TEXT)""",
    """CREATE TABLE orders (
        id INTEGER PRIMARY KEY, customer_id INTEGER, order_date TEXT, delivery_date TEXT,
        delivery_address TEXT, total_amount NUMERIC(10,2), payment_status VARCHAR(20),
        created_at TEXT)""",
    """CREATE TABLE order_items (
        id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER,
        quantity INTEGER, price NUMERIC(10,2))""",
    """CREATE TABLE payments (
        id INTEGER PRIMARY KEY, order_id INTEGER, payment_date TEXT,
        amount NUMERIC(10,2), payment_method VARCHAR(50), notes TEXT)""",
)

_LEGACY_ROWS = (
    "INSERT INTO users VALUES (1, 'admin', 'pbkdf2:sha256:600000$salt$deadbeef', '2024-05-01 08:30:00')",
    "INSERT INTO users VALUES (2, 'chef', 'scrypt:32768:8:1$salt$beefdead', '2024-05-02 09:00:00')",
    "INSERT INTO customers VALUES (1, 'ABC Construction', '0123456789', '123 Rue Principale, Sétif', '2024-04-01 10:00:00')",
    "INSERT INTO customers VALUES (2, 'XYZ Builders', '0987654321', 'Zone industrielle', '2024-04-02 10:00:00')",
    "INSERT INTO products VALUES (1, 'Ciment Portland', 350.00, 100, 'bag', '2024-04-01 10:00:00')",
    "INSERT INTO products VALUES (2, 'Sable de rivière', 1200.00, 20, 'truck', '2024-04-01 10:00:00')",
    "INSERT INTO products VALUES (3, 'Truelle', 500.00, 5, 'cuillère', '2024-04-01 10:00:00')",
    "INSERT INTO products VALUES (4, 'Produit sans unité', 100.00, 0, NULL, NULL)",
    "INSERT INTO orders VALUES (1, 1, '2024-05-01', '2024-05-03', 'Chantier Sétif', 35000.00, 'Partial', '2024-05-01 11:00:00')",
    "INSERT INTO order_items VALUES (1, 1, 1, 100, 350.00)",
    "INSERT INTO payments VALUES (1, 1, '2024-05-02', 20000.00, 'Cash', 'avance')",
)


def _build_legacy_database() -> None:
    db.drop_all()
    with db.engine.begin() as connection:
        for statement in _LEGACY_SCHEMA:
            connection.execute(text(statement))
        for statement in _LEGACY_ROWS:
            connection.execute(text(statement))


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------

def test_backup_writes_every_table_as_readable_json(app, runner, tmp_path, customer, product, warehouse):
    give_stock(product, warehouse, "10", unit_cost="1200")
    db.session.commit()

    path = tmp_path / "backup.json"
    result = runner.invoke(args=["backup", "--output", str(path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["meta"]["rows"] > 0
    assert {"products", "customers", "stock_levels", "stock_movements"} <= set(payload["tables"])
    assert len(payload["tables"]["products"]) == 1


def test_backup_encodes_money_and_dates_as_text_not_binary_floats(app, runner, tmp_path, product, warehouse):
    give_stock(product, warehouse, "10", unit_cost="1200")
    db.session.commit()

    path = tmp_path / "backup.json"
    runner.invoke(args=["backup", "--output", str(path)])
    raw = path.read_text(encoding="utf-8")

    assert '"__type__": "decimal"' in raw
    payload = json.loads(raw)
    price = payload["tables"]["products"][0]["sale_price"]
    # A Decimal is carried as a string: no binary-float rounding on the way out.
    assert price == {"__type__": "decimal", "__value__": "1800.00"}
    movement = payload["tables"]["stock_movements"][0]
    assert movement["created_at"]["__type__"] == "datetime"


def test_restore_replaces_the_database_without_duplicating_rows(app, runner, tmp_path, customer, product, warehouse):
    give_stock(product, warehouse, "10", unit_cost="1200")
    db.session.commit()

    path = tmp_path / "backup.json"
    runner.invoke(args=["backup", "--output", str(path)])

    # Wreck the current data the way a bad day would.
    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())
    db.session.commit()
    assert db.session.query(Product).count() == 0

    result = runner.invoke(args=["restore", str(path), "--yes"])

    assert result.exit_code == 0, result.output
    db.session.expire_all()
    assert db.session.query(Product).count() == 1
    assert db.session.query(Customer).count() == 1

    restored = db.session.query(Product).one()
    restored_warehouse = db.session.query(Warehouse).one()
    assert stock.on_hand(restored, restored_warehouse) == Decimal("10")


def test_restore_is_idempotent(app, runner, tmp_path, product, warehouse):
    give_stock(product, warehouse, "10", unit_cost="1200")
    db.session.commit()

    path = tmp_path / "backup.json"
    runner.invoke(args=["backup", "--output", str(path)])

    runner.invoke(args=["restore", str(path), "--yes"])
    runner.invoke(args=["restore", str(path), "--yes"])

    db.session.expire_all()
    assert db.session.query(Product).count() == 1
    assert db.session.query(StockLevel).count() == 1


def test_the_ledger_and_its_levels_survive_a_round_trip(app, runner, tmp_path, product, warehouse):
    give_stock(product, warehouse, "10", unit_cost="1200")
    db.session.commit()
    before_movements = db.session.query(StockMovement).count()

    path = tmp_path / "backup.json"
    runner.invoke(args=["backup", "--output", str(path)])
    runner.invoke(args=["restore", str(path), "--yes"])

    db.session.expire_all()
    assert db.session.query(StockMovement).count() == before_movements
    restored = db.session.query(Product).one()
    restored_warehouse = db.session.query(Warehouse).one()
    level = stock.get_level(restored, restored_warehouse)
    assert level.quantity == Decimal("10")
    assert level.avg_cost == Decimal("1200.00")


# ---------------------------------------------------------------------------
# Legacy detection
# ---------------------------------------------------------------------------

def test_a_modern_database_is_not_mistaken_for_a_legacy_one(app):
    from app.legacy import detect_legacy

    assert detect_legacy() == []


def test_the_modern_database_is_recognised_even_though_the_names_match(app):
    """``products`` exists in both schemas; ``stock_unit_id`` tells them apart."""
    from app.legacy import detect_legacy

    assert "stock_unit_id" in {column["name"] for column in inspect(db.engine).get_columns("products")}
    assert detect_legacy() == []


# ---------------------------------------------------------------------------
# The legacy upgrade
# ---------------------------------------------------------------------------

@pytest.fixture()
def legacy_report(app):
    _build_legacy_database()

    from app.legacy import migrate_legacy

    report = migrate_legacy()
    db.session.commit()
    return report


def test_all_six_legacy_tables_are_detected(app):
    _build_legacy_database()

    from app.legacy import detect_legacy

    assert set(detect_legacy()) == {
        "users", "customers", "products", "orders", "order_items", "payments",
    }


def test_every_migratable_row_arrives(legacy_report):
    assert legacy_report.applied is True
    assert legacy_report.tables_found  # non-empty
    assert legacy_report.counts == {
        "users": 2,
        "products": 4,
        "customers": 2,
        "sales_orders": 1,
        "sales_order_items": 1,
        "payments": 1,
    }
    assert db.session.query(User).count() == 2
    assert db.session.query(Customer).count() == 2
    assert db.session.query(Product).count() == 4
    assert db.session.query(SalesOrder).count() == 1
    assert db.session.query(SalesOrderItem).count() == 1
    assert db.session.query(Payment).count() == 1


def test_legacy_passwords_are_carried_over_untouched(legacy_report):
    user = db.session.query(User).filter_by(username="chef").one()

    # The hash is copied, never re-hashed into something the shop cannot use.
    assert user.password_hash == "scrypt:32768:8:1$salt$beefdead"
    assert user.role == Role.ADMIN
    assert user.created_at == datetime(2024, 5, 2, 9, 0, 0)
    assert any("admin" in warning for warning in legacy_report.warnings)


def test_legacy_units_are_mapped_and_unknown_ones_are_reported(legacy_report):
    units = {unit.code: unit for unit in db.session.query(UnitOfMeasure).all()}
    by_name = {product.name: product for product in db.session.query(Product).all()}

    assert by_name["Ciment Portland"].stock_unit_id == units["bag"].id
    assert by_name["Sable de rivière"].stock_unit_id == units["truckload"].id
    # 'cuillère' is not a unit we know; it becomes 'piece' *and* says so.
    assert by_name["Truelle"].stock_unit_id == units["piece"].id
    assert any("cuillère" in warning for warning in legacy_report.warnings)
    # A NULL unit is not a surprise and must not spam the report.
    assert not any("Produit sans unité" in warning for warning in legacy_report.warnings)
    assert by_name["Produit sans unité"].stock_unit_id == units["piece"].id


def test_legacy_prices_are_kept_as_decimal_twice(legacy_report):
    ciment = db.session.query(Product).filter_by(name="Ciment Portland").one()

    assert ciment.legacy_price == Decimal("350.00")
    assert ciment.legacy_stock_quantity == 100
    assert ciment.legacy_unit == "bag"
    # The deprecated mirror column is documentation of the legacy value; the
    # live price is the new (Decimal, price-list aware) one.
    assert ciment.cost_price == Decimal("350.00")


def test_legacy_stock_becomes_explicit_opening_stock(legacy_report):
    ciment = db.session.query(Product).filter_by(name="Ciment Portland").one()
    warehouse = db.session.query(Warehouse).one()

    assert stock.on_hand(ciment, warehouse) == Decimal("100")
    movement = (
        db.session.query(StockMovement)
        .filter_by(source_type="legacy_migration", product_id=ciment.id)
        .one()
    )
    assert movement.reason == "opening"
    assert movement.quantity == Decimal("100")
    assert "legacy" in (movement.reference or "").lower()

    # A product that had zero stock generates no phantom movement.
    truelle = db.session.query(Product).filter_by(name="Truelle").one()
    assert stock.on_hand(truelle, warehouse) == Decimal("5")


def test_legacy_addresses_keep_their_freetext(legacy_report):
    customer = db.session.query(Customer).filter_by(name="ABC Construction").one()

    assert customer.legacy_address == "123 Rue Principale, Sétif"
    assert customer.street == "123 Rue Principale, Sétif"
    assert customer.phone == "0123456789"


def test_legacy_orders_become_delivered_sales_orders(legacy_report):
    order = db.session.query(SalesOrder).one()
    item = order.items[0]

    assert order.number == "LEGACY-BC-00001"
    assert order.status == "delivered"
    assert order.total_amount == Decimal("35000.00")
    assert order.delivery_address == "Chantier Sétif"
    assert order.customer.name == "ABC Construction"
    assert item.quantity == Decimal("100")
    assert item.line_total == Decimal("35000.00")
    assert "no delivery document" in (order.notes or "")


def test_legacy_payments_become_unallocated_advances(legacy_report):
    payment = db.session.query(Payment).one()

    assert payment.number == "LEGACY-ENC-00001"
    assert payment.direction == "inbound"
    assert payment.amount == Decimal("20000.00")
    assert payment.method == "cash"
    assert payment.customer.name == "ABC Construction"
    # Guessing which invoice it settled would fabricate data, so it does not.
    assert payment.allocated_amount == Decimal("0.00")
    assert payment.unallocated_amount == Decimal("20000.00")
    assert "unallocated" in (payment.notes or "").lower()


def test_the_legacy_tables_are_gone_afterwards(legacy_report):
    names = set(inspect(db.engine).get_table_names())

    assert not [name for name in names if name.endswith("__legacy")]
    assert "products" in names


def test_the_migration_reports_what_it_could_not_do(legacy_report):
    """Honesty: anything a human has to finish is written down."""
    joined = " | ".join(legacy_report.warnings)

    assert "review roles" in joined
    assert "without new stock movements" in joined
    assert "unallocated" in joined


def test_running_the_migration_twice_changes_nothing_the_second_time(legacy_report):
    from app.legacy import migrate_legacy

    before = db.session.query(Product).count()
    second = migrate_legacy()
    db.session.commit()

    assert second.applied is False
    assert second.warnings == ["no legacy tables detected; nothing to do"]
    assert db.session.query(Product).count() == before


def test_the_migration_aborts_without_touching_a_modern_database(app):
    from app.legacy import migrate_legacy

    report = migrate_legacy()

    assert report.applied is False
    assert db.session.query(UnitOfMeasure).count() > 0     # seed data still there


# ---------------------------------------------------------------------------
# Timestamp coercion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        (datetime(2024, 5, 1, 8, 30), datetime(2024, 5, 1, 8, 30)),
        ("2024-05-01 08:30:00", datetime(2024, 5, 1, 8, 30)),
        ("2024-05-01T08:30:00", datetime(2024, 5, 1, 8, 30)),
        ("2024-05-01", datetime(2024, 5, 1)),
        ("01/05/2024", datetime(2024, 5, 1)),
        ("nonsense", None),
        (None, None),
        ("", None),
    ],
)
def test_legacy_timestamps_are_coerced_or_dropped_not_fatal(app, raw, expected):
    from app.legacy import _as_datetime

    assert _as_datetime(raw) == expected

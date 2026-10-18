"""Shared pytest fixtures.

Every fixture builds its data through the *service layer*, not by inserting rows
directly, so the tests exercise the same code paths the application uses.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import create_app
from app.extensions import db as _db
from app.models import (
    Category,
    Customer,
    CustomerType,
    Product,
    ProductUnit,
    Supplier,
    UnitOfMeasure,
    UnitKind,
    User,
    Warehouse,
)
from app.models.security import Role
from app.services import seed as seed_service, stock, uom


@pytest.fixture()
def app():
    """A testing application with an in-memory database and reference data."""
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        seed_service.seed_reference_data()
        _db.session.commit()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def warehouse(app) -> Warehouse:
    return stock.default_warehouse()


@pytest.fixture()
def units(app) -> dict[str, UnitOfMeasure]:
    return uom.default_stock_units()


@pytest.fixture()
def categories(app) -> dict[str, Category]:
    return {row.code: row for row in _db.session.query(Category).all()}


@pytest.fixture()
def make_product(app, units, categories):
    """Factory: create a product with an explicit stock unit and cost."""

    def factory(
        *,
        name: str = "Sable 0/4",
        sku: str = "SAND-1",
        unit_code: str = "m3",
        category_code: str = "SAND",
        cost: str = "1200",
        sale: str = "1800",
        reorder: str = "10",
        **extra,
    ) -> Product:
        product = Product(
            name=name,
            sku=sku,
            category_id=categories[category_code].id if category_code else None,
            stock_unit_id=units[unit_code].id,
            cost_price=Decimal(cost),
            sale_price=Decimal(sale),
            wholesale_price=(Decimal(sale) * Decimal("0.95")).quantize(Decimal("0.01")),
            contractor_price=(Decimal(sale) * Decimal("0.90")).quantize(Decimal("0.01")),
            reorder_level=Decimal(reorder),
            **extra,
        )
        _db.session.add(product)
        _db.session.flush()
        return product

    return factory


@pytest.fixture()
def product(make_product) -> Product:
    return make_product()


@pytest.fixture()
def cement(make_product, units) -> Product:
    return make_product(
        name="Ciment CPJ 42.5", sku="CIM-1", unit_code="bag",
        category_code="CEMENT", cost="560", sale="690", bag_weight_kg=Decimal("50"),
    )


@pytest.fixture()
def make_customer(app):
    def factory(name: str = "SARL Bâtir", **extra) -> Customer:
        customer = Customer(
            name=name,
            customer_type=extra.pop("customer_type", CustomerType.CONTRACTOR.value),
            wilaya_code=extra.pop("wilaya_code", 19),
            **extra,
        )
        _db.session.add(customer)
        _db.session.flush()
        return customer

    return factory


@pytest.fixture()
def customer(make_customer) -> Customer:
    return make_customer()


@pytest.fixture()
def make_supplier(app):
    def factory(name: str = "Cimenterie Sétif", **extra) -> Supplier:
        supplier = Supplier(name=name, wilaya_code=19, **extra)
        _db.session.add(supplier)
        _db.session.flush()
        return supplier

    return factory


@pytest.fixture()
def supplier(make_supplier) -> Supplier:
    return make_supplier()


@pytest.fixture()
def make_user(app):
    def factory(username: str = "tester", role: str = Role.ADMIN, password: str = "Tester-Pass-123", **extra) -> User:
        user = User(username=username, full_name=extra.pop("full_name", username), role=role, is_active=extra.pop("is_active", True), **extra)
        user.set_password(password)
        _db.session.add(user)
        _db.session.flush()
        return user

    return factory


@pytest.fixture()
def admin_user(make_user) -> User:
    user = make_user("admin", Role.ADMIN)
    _db.session.commit()
    return user


@pytest.fixture()
def viewer_user(make_user) -> User:
    user = make_user("viewer", Role.VIEWER)
    _db.session.commit()
    return user


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def login(client):
    """Log a user in by seeding the Flask-Login session (CSRF is off in tests)."""

    def do_login(user: User):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        return client

    return do_login


@pytest.fixture()
def auth_client(client, admin_user, login):
    return login(admin_user)


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


# ---------------------------------------------------------------------------
# Small helpers used by several tests
# ---------------------------------------------------------------------------

def give_stock(product: Product, warehouse: Warehouse, quantity: str, unit_cost: str | None = None):
    """Put opening stock on the shelf through the ledger."""
    from app.models import MovementReason

    return stock.receive(
        product, warehouse, Decimal(quantity),
        reason=MovementReason.OPENING.value,
        unit_cost=Decimal(unit_cost) if unit_cost else None,
        source_type="test", reference="TEST-OPENING",
    )


def allow_unit(product: Product, unit: UnitOfMeasure, *, sale: bool = True, purchase: bool = True) -> ProductUnit:
    link = ProductUnit(product_id=product.id, unit_id=unit.id, allow_sale=sale, allow_purchase=purchase)
    _db.session.add(link)
    _db.session.flush()
    return link


def today() -> date:
    from app.services.timeutil import today_local

    return today_local()


def days_ago(count: int) -> date:
    return today() - timedelta(days=count)


__all__ = ["give_stock", "allow_unit", "today", "days_ago", "UnitKind"]

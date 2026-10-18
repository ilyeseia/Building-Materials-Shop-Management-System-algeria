"""Reference-data seeding.

``seed_reference_data`` is idempotent: it creates the units of measure, product
categories, the default warehouse, the document sequences and the settings row
only when they are missing, so it is safe to run on an existing database and it
never overwrites data an administrator has edited.

``seed_demo_data`` is explicitly a *demo* helper for a fresh installation and for
tests; it is never run automatically at start-up.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.extensions import db
from app.models import (
    Brand,
    Category,
    MovementReason,
    Product,
    UnitKind,
    UnitOfMeasure,
    Warehouse,
)
from app.services import numbering, settings as settings_service, stock
from app.services.timeutil import today_local

#: (code, kind, factor, name_ar, name_fr, name_en, allows_fraction)
UNITS: tuple[tuple[str, str, str, str, str, str, bool], ...] = (
    # Mass — base kg
    ("kg", UnitKind.MASS.value, "1", "كغ", "kg", "kg", True),
    ("g", UnitKind.MASS.value, "0.001", "غ", "g", "g", True),
    ("q", UnitKind.MASS.value, "100", "قنطار", "quintal", "quintal", True),
    ("t", UnitKind.MASS.value, "1000", "طن", "tonne", "tonne", True),
    # Volume — base m³
    ("m3", UnitKind.VOLUME.value, "1", "م³", "m³", "m³", True),
    ("l", UnitKind.VOLUME.value, "0.001", "لتر", "litre", "litre", True),
    # Count — packaging; conversion is always product-specific
    ("piece", UnitKind.COUNT.value, "1", "وحدة", "pièce", "piece", True),
    ("bag", UnitKind.COUNT.value, "1", "كيس", "sac", "bag", False),
    ("pallet", UnitKind.COUNT.value, "1", "طبلية", "palette", "pallet", False),
    ("bundle", UnitKind.COUNT.value, "1", "حزمة", "botte", "bundle", False),
    ("truckload", UnitKind.COUNT.value, "1", "حمولة شاحنة", "camion", "truckload", False),
    # Length — base m
    ("m", UnitKind.LENGTH.value, "1", "م", "m", "m", True),
    ("bar", UnitKind.LENGTH.value, "1", "قضيب", "barre", "bar", False),
)

#: (code, name_ar, name_fr, name_en, sort_order)
CATEGORIES: tuple[tuple[str, str, str, str, int], ...] = (
    ("SAND", "رمل", "Sable", "Sand", 10),
    ("AGGREGATE", "حصى وأحجار", "Graviers et agrégats", "Gravel & aggregates", 20),
    ("CEMENT", "إسمنت", "Ciment", "Cement", 30),
    ("STEEL", "حديد التسليح", "Acier à béton", "Reinforcing steel", 40),
    ("GYPSUM", "جبس", "Plâtre", "Gypsum", 50),
    ("BRICKS", "طوب وآجر", "Briques", "Bricks", 60),
    ("CONCRETE", "خرسانة جاهزة", "Béton prêt", "Ready-mix", 70),
)

DEMO_PRODUCTS: tuple[dict, ...] = (
    {
        "sku": "SABLE-0", "name": "Sable 0/4", "category": "SAND", "unit": "m3",
        "cost": "1200", "sale": "1800", "reorder": "50",
        "spec_notes": "حبيبات صغيرة 0/4 مم — البيع بالمتر المكعب",
    },
    {
        "sku": "GRAV-3-8", "name": "Gravier 3/8", "category": "AGGREGATE", "unit": "m3",
        "cost": "1400", "sale": "2100", "reorder": "40",
    },
    {
        "sku": "CIM-42.5", "name": "Ciment CPJ 42.5 (sac 50 kg)", "category": "CEMENT", "unit": "bag",
        "cost": "560", "sale": "690", "reorder": "200", "bag_weight_kg": "50",
    },
    {
        "sku": "ACI-T12", "name": "Acier à béton T12", "category": "STEEL", "unit": "t",
        "cost": "12500", "sale": "14200", "reorder": "5",
        "steel_diameter_mm": "12", "steel_length_m": "12",
    },
    {
        "sku": "PLATRE-25", "name": "Plâtre (sac 25 kg)", "category": "GYPSUM", "unit": "bag",
        "cost": "320", "sale": "430", "reorder": "100", "bag_weight_kg": "25",
    },
)


def seed_reference_data(*, with_warehouse: bool = True) -> dict:
    """Create the reference rows the application needs.  Idempotent."""
    created = {"units": 0, "categories": 0, "warehouses": 0, "brands": 0}

    existing_units = {
        unit.code: unit for unit in db.session.execute(select(UnitOfMeasure)).scalars().all()
    }
    for code, kind, factor, name_ar, name_fr, name_en, allows_fraction in UNITS:
        if code in existing_units:
            continue
        db.session.add(
            UnitOfMeasure(
                code=code, kind=kind, factor=Decimal(factor),
                name_ar=name_ar, name_fr=name_fr, name_en=name_en,
                allows_fraction=allows_fraction, is_system=True,
            )
        )
        created["units"] += 1

    existing_categories = {
        row.code for row in db.session.execute(select(Category)).scalars().all()
    }
    for code, name_ar, name_fr, name_en, sort_order in CATEGORIES:
        if code in existing_categories:
            continue
        db.session.add(
            Category(code=code, name_ar=name_ar, name_fr=name_fr, name_en=name_en, sort_order=sort_order)
        )
        created["categories"] += 1

    db.session.flush()

    if with_warehouse:
        existing_warehouse = db.session.execute(
            select(Warehouse).order_by(Warehouse.id)
        ).scalars().first()
        if existing_warehouse is None:
            from config import DEFAULT_WILAYA_CODE

            db.session.add(
                Warehouse(
                    code="PRINC", name_ar="المخزن الرئيسي", name_fr="Dépôt principal",
                    name_en="Main warehouse", wilaya_code=DEFAULT_WILAYA_CODE, is_default=True,
                )
            )
            created["warehouses"] += 1
            db.session.flush()

    settings = settings_service.ensure_settings()
    if not settings.default_warehouse_id:
        warehouse = stock.default_warehouse()
        if warehouse is not None:
            settings.default_warehouse_id = warehouse.id

    numbering.ensure_sequences()
    db.session.flush()
    return created


def seed_demo_data(*, opening_stock: bool = True) -> dict:
    """Create a small demonstration catalogue.  For a fresh install/tests only."""
    seed_reference_data()
    unit_by_code = {
        unit.code: unit for unit in db.session.execute(select(UnitOfMeasure)).scalars().all()
    }
    category_by_code = {
        row.code: row for row in db.session.execute(select(Category)).scalars().all()
    }
    warehouse = stock.default_warehouse()

    created = {"products": 0}
    existing_skus = {
        row.sku for row in db.session.execute(select(Product)).scalars().all() if row.sku
    }
    for spec in DEMO_PRODUCTS:
        if spec["sku"] in existing_skus:
            continue
        stock_unit = unit_by_code.get(spec["unit"])
        category = category_by_code.get(spec["category"])
        if stock_unit is None or category is None:
            continue
        product = Product(
            sku=spec["sku"],
            name=spec["name"],
            category_id=category.id,
            stock_unit_id=stock_unit.id,
            cost_price=Decimal(spec["cost"]),
            sale_price=Decimal(spec["sale"]),
            wholesale_price=(Decimal(spec["sale"]) * Decimal("0.94")).quantize(Decimal("0.01")),
            contractor_price=(Decimal(spec["sale"]) * Decimal("0.90")).quantize(Decimal("0.01")),
            reorder_level=Decimal(spec.get("reorder", "0")),
            bag_weight_kg=Decimal(spec["bag_weight_kg"]) if spec.get("bag_weight_kg") else None,
            steel_diameter_mm=Decimal(spec["steel_diameter_mm"]) if spec.get("steel_diameter_mm") else None,
            steel_length_m=Decimal(spec["steel_length_m"]) if spec.get("steel_length_m") else None,
            spec_notes=spec.get("spec_notes"),
        )
        db.session.add(product)
        db.session.flush()
        created["products"] += 1

        if opening_stock and warehouse is not None:
            quantity = Decimal("100") if stock_unit.kind == UnitKind.COUNT.value else Decimal("30")
            stock.receive(
                product, warehouse, quantity,
                reason=MovementReason.OPENING.value,
                unit_cost=Decimal(spec["cost"]),
                source_type="seed", reference="OPENING",
                note="Opening stock (demo)",
            )

    db.session.flush()
    return created

"""Catalog: products, categories, units of measure, warehouses, conversions."""
from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.extensions import db
from app.forms import CategoryForm, ConversionForm, ProductForm, UnitForm, WarehouseForm
from app.i18n import localized_name, translate
from app.models import (
    Brand,
    Category,
    Product,
    ProductConversion,
    StockLevel,
    Supplier,
    UnitOfMeasure,
    Warehouse,
)
from app.blueprints.helpers import (
    choices_for,
    clean_str,
    fill_choices,
    handle_errors,
    page_args,
    parse_decimal,
    parse_int,
)
from app.security import requires
from app.services import audit, reporting, stock, uom
from app.services.timeutil import today_local

bp = Blueprint("catalog", __name__)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

@bp.route("/catalog/products")
@requires("catalog.view")
def products():
    page, per_page = page_args()
    search = (request.args.get("q") or "").strip()
    category_id = parse_int(request.args.get("category"))
    query = select(Product)
    if search:
        pattern = f"%{search}%"
        query = query.where(Product.name.ilike(pattern) | Product.sku.ilike(pattern))
    if category_id:
        query = query.where(Product.category_id == category_id)
    query = query.order_by(Product.name)
    result = reporting.paginate(query, page, per_page)

    from app.i18n import format_money, format_quantity_with_unit

    rows = []
    for product in result["items"]:
        on_hand = stock.total_on_hand(product.id)
        rows.append(
            {
                "sku": product.sku or "",
                "name": product.name,
                "category": localized_name(product.category) if product.category else "",
                "stock": format_quantity_with_unit(on_hand, product.stock_unit_code),
                "cost": format_money(product.cost_price),
                "sale": format_money(product.sale_price),
                "_url": url_for("catalog.product_detail", product_id=product.id),
            }
        )
    return render_template(
        "crud/list.html",
        title_key="nav.catalog",
        rows=rows,
        columns=[
            {"key": "sku", "label": "product.sku"},
            {"key": "name", "label": "common.name"},
            {"key": "category", "label": "product.category"},
            {"key": "stock", "label": "stock.on_hand"},
            {"key": "cost", "label": "product.cost_price"},
            {"key": "sale", "label": "product.sale_price"},
        ],
        result=result,
        search=search,
        searchable=True,
        create_url=url_for("catalog.product_new") if _can("catalog.manage") else None,
        search_url=url_for("catalog.products"),
        extra_filters=_category_filter(category_id),
    )


def _category_filter(selected: int | None):
    categories = db.session.execute(select(Category).order_by(Category.sort_order)).scalars().all()
    return {
        "name": "category",
        "label": "product.category",
        "selected": selected or 0,
        "options": [(0, "—")] + [(c.id, localized_name(c)) for c in categories],
    }


def _product_form_choices(form: ProductForm) -> None:
    fill_choices(
        form,
        category_id=choices_for(Category, order_by=Category.sort_order),
        brand_id=choices_for(Brand, order_by=Brand.name),
        default_supplier_id=choices_for(Supplier, order_by=Supplier.name),
        stock_unit_id=choices_for(UnitOfMeasure, order_by=UnitOfMeasure.code, blank=False),
    )


def _apply_product(form: ProductForm, product: Product) -> None:
    product.sku = form.sku.data or None
    product.barcode = form.barcode.data or None
    product.name = form.name.data
    product.description = form.description.data
    product.category_id = form.category_id.data or None
    product.brand_id = form.brand_id.data or None
    product.default_supplier_id = form.default_supplier_id.data or None
    if form.stock_unit_id.data:
        product.stock_unit_id = form.stock_unit_id.data
    product.cost_price = form.cost_price.data or Decimal("0")
    product.sale_price = form.sale_price.data or Decimal("0")
    product.wholesale_price = form.wholesale_price.data
    product.contractor_price = form.contractor_price.data
    product.reorder_level = form.reorder_level.data or Decimal("0")
    product.bag_weight_kg = form.bag_weight_kg.data
    product.steel_diameter_mm = form.steel_diameter_mm.data
    product.steel_length_m = form.steel_length_m.data
    product.steel_grade = form.steel_grade.data
    product.aggregate_grading = form.aggregate_grading.data
    product.spec_notes = form.spec_notes.data
    product.is_active = bool(form.is_active.data)


@bp.route("/catalog/products/new", methods=["GET", "POST"])
@requires("catalog.manage")
@handle_errors
def product_new():
    form = ProductForm()
    _product_form_choices(form)
    if form.validate_on_submit():
        if not form.stock_unit_id.data:
            flash(translate("val.invalid_unit", unit=""), "danger")
        else:
            product = Product(name=form.name.data, stock_unit_id=form.stock_unit_id.data)
            _apply_product(form, product)
            db.session.add(product)
            db.session.flush()
            product.sync_legacy_mirrors(on_hand=Decimal("0"))
            audit.record("created", "product", product.id, summary=product.name)
            db.session.commit()
            flash(translate("common.saved"), "success")
            return redirect(url_for("catalog.product_detail", product_id=product.id))
    return render_template("crud/form.html", form=form, title_key="product.new",
                           cancel_url=url_for("catalog.products"))


@bp.route("/catalog/products/<int:product_id>")
@requires("catalog.view")
def product_detail(product_id: int):
    product = db.session.get(Product, product_id)
    if product is None:
        return redirect(url_for("catalog.products"))
    levels = db.session.execute(
        select(StockLevel).where(StockLevel.product_id == product_id)
    ).scalars().all()
    conversions = db.session.execute(
        select(ProductConversion)
        .where(ProductConversion.product_id == product_id)
        .order_by(ProductConversion.effective_from.desc())
    ).scalars().all()
    return render_template(
        "catalog/product_detail.html",
        product=product,
        levels=levels,
        conversions=conversions,
        total_on_hand=stock.total_on_hand(product_id),
        available=stock.available(product),
        steel_weight=uom.theoretical_steel_weight(
            product.steel_diameter_mm, product.steel_length_m
        ) if product.steel_diameter_mm and product.steel_length_m else None,
    )


@bp.route("/catalog/products/<int:product_id>/edit", methods=["GET", "POST"])
@requires("catalog.manage")
@handle_errors
def product_edit(product_id: int):
    product = db.session.get(Product, product_id)
    if product is None:
        return redirect(url_for("catalog.products"))
    form = ProductForm(obj=product)
    _product_form_choices(form)
    if not form.is_submitted():
        form.category_id.data = product.category_id or 0
        form.brand_id.data = product.brand_id or 0
        form.default_supplier_id.data = product.default_supplier_id or 0
        form.stock_unit_id.data = product.stock_unit_id
    if form.validate_on_submit():
        _apply_product(form, product)
        product.sync_legacy_mirrors(on_hand=stock.total_on_hand(product.id))
        audit.record("updated", "product", product.id, summary=product.name)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("catalog.product_detail", product_id=product.id))
    return render_template("crud/form.html", form=form, title_key="product.edit",
                           cancel_url=url_for("catalog.product_detail", product_id=product.id))


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------

@bp.route("/catalog/products/<int:product_id>/conversions", methods=["GET", "POST"])
@requires("catalog.manage")
@handle_errors
def product_conversions(product_id: int):
    product = db.session.get(Product, product_id)
    if product is None:
        return redirect(url_for("catalog.products"))
    form = ConversionForm()
    fill_choices(
        form,
        from_unit_id=choices_for(UnitOfMeasure, order_by=UnitOfMeasure.code, blank=False),
        to_unit_id=choices_for(UnitOfMeasure, order_by=UnitOfMeasure.code, blank=False),
    )
    if form.validate_on_submit():
        if form.from_unit_id.data == form.to_unit_id.data:
            flash(translate("val.same_unit"), "danger")
        else:
            conversion = ProductConversion(
                product_id=product.id,
                from_unit_id=form.from_unit_id.data,
                to_unit_id=form.to_unit_id.data,
                factor=form.factor.data,
                conversion_type=form.conversion_type.data,
                effective_from=form.effective_from.data or today_local(),
                source=clean_str(form.source.data, 200),
                notes=form.notes.data,
            )
            db.session.add(conversion)
            audit.record("created", "product_conversion", product.id, summary=f"{product.name} {form.conversion_type.data}")
            db.session.commit()
            flash(translate("common.saved"), "success")
            return redirect(url_for("catalog.product_conversions", product_id=product.id))
    conversions = db.session.execute(
        select(ProductConversion).where(ProductConversion.product_id == product_id)
        .order_by(ProductConversion.effective_from.desc())
    ).scalars().all()
    return render_template(
        "catalog/conversions.html", product=product, form=form, conversions=conversions
    )


# ---------------------------------------------------------------------------
# Categories / units / warehouses
# ---------------------------------------------------------------------------

@bp.route("/catalog/categories", methods=["GET", "POST"])
@requires("catalog.view")
@handle_errors
def categories():
    form = CategoryForm()
    if _can("catalog.manage") and form.validate_on_submit():
        category = Category(
            code=form.code.data, name_ar=form.name_ar.data, name_fr=form.name_fr.data,
            name_en=form.name_en.data, sort_order=form.sort_order.data or 0,
        )
        db.session.add(category)
        audit.record("created", "category", None, summary=category.code)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("catalog.categories"))
    items = db.session.execute(select(Category).order_by(Category.sort_order)).scalars().all()
    return render_template("catalog/categories.html", form=form, items=items)


@bp.route("/catalog/categories/<int:category_id>/edit", methods=["POST"])
@requires("catalog.manage")
@handle_errors
def category_edit(category_id: int):
    category = db.session.get(Category, category_id)
    if category is None:
        return redirect(url_for("catalog.categories"))
    category.name_ar = request.form.get("name_ar") or category.name_ar
    category.name_fr = request.form.get("name_fr") or category.name_fr
    category.name_en = request.form.get("name_en") or category.name_en
    category.sort_order = parse_int(request.form.get("sort_order"), category.sort_order) or 0
    audit.record("updated", "category", category.id, summary=category.code)
    db.session.commit()
    flash(translate("common.saved"), "success")
    return redirect(url_for("catalog.categories"))


@bp.route("/catalog/units", methods=["GET", "POST"])
@requires("catalog.view")
@handle_errors
def units():
    form = UnitForm()
    if _can("catalog.manage") and form.validate_on_submit():
        unit = UnitOfMeasure(
            code=form.code.data, kind=form.kind.data, factor=form.factor.data,
            name_ar=form.name_ar.data, name_fr=form.name_fr.data, name_en=form.name_en.data,
            allows_fraction=bool(form.allows_fraction.data),
        )
        db.session.add(unit)
        audit.record("created", "unit", None, summary=unit.code)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("catalog.units"))
    items = db.session.execute(select(UnitOfMeasure).order_by(UnitOfMeasure.kind, UnitOfMeasure.code)).scalars().all()
    return render_template("catalog/units.html", form=form, items=items)


@bp.route("/catalog/warehouses", methods=["GET", "POST"])
@requires("catalog.view")
@handle_errors
def warehouses():
    form = WarehouseForm()
    if _can("catalog.manage") and form.validate_on_submit():
        warehouse = Warehouse(
            code=form.code.data, name_ar=form.name_ar.data, name_fr=form.name_fr.data,
            name_en=form.name_en.data, location=form.location.data,
            wilaya_code=form.wilaya_code.data or None, is_default=bool(form.is_default.data),
        )
        db.session.add(warehouse)
        db.session.flush()
        if warehouse.is_default:
            for other in db.session.execute(
                select(Warehouse).where(Warehouse.id != warehouse.id)
            ).scalars().all():
                other.is_default = False
        audit.record("created", "warehouse", warehouse.id, summary=warehouse.code)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("catalog.warehouses"))
    items = db.session.execute(select(Warehouse).order_by(Warehouse.code)).scalars().all()
    return render_template("catalog/warehouses.html", form=form, items=items)


def _can(permission: str) -> bool:
    from app.security import can

    return can(permission)

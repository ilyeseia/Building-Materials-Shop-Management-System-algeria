"""Shared helpers for blueprints: input parsing, line extraction, flashing errors."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user
from sqlalchemy import select

from app.extensions import db
from app.i18n import to_decimal, translate
from app.services.errors import BusinessError

# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

def parse_int(raw, default: int | None = None) -> int | None:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def parse_decimal(raw, default: Decimal | None = None) -> Decimal | None:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return to_decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        return default


def parse_date(raw, default: date | None = None) -> date | None:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if raw is None or str(raw).strip() == "":
        return default
    text = str(raw).strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return default


def clean_str(raw, limit: int | None = None) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def page_args(default_per_page: int = 25) -> tuple[int, int]:
    page = parse_int(request.args.get("page"), 1) or 1
    per_page = parse_int(request.args.get("per_page"), default_per_page) or default_per_page
    per_page = max(5, min(per_page, 200))
    return max(page, 1), per_page


def wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


# ---------------------------------------------------------------------------
# Document line extraction (arrays submitted by the line-entry form)
# ---------------------------------------------------------------------------

def form_lines(prefix: str = "line", *, allow_zero: bool = False,
               allow_negative: bool = False) -> list[dict]:
    """Collect repeating line arrays from the submitted form.

    The line-entry template submits parallel arrays
    (``product_id[]``, ``quantity[]``, ``unit_id[]``, …).  Blank product rows are
    ignored so a form can ship with spare rows.

    ``quantity`` is required to be strictly positive for commercial documents
    (an order line of zero is not a line).  Stock adjustments accept negative
    quantities — that is how a shortage is recorded — and physical counts accept
    zero, because a counted quantity of zero is a legitimate result.
    """
    products = request.form.getlist(f"{prefix}_product_id")
    quantities = request.form.getlist(f"{prefix}_quantity")
    units = request.form.getlist(f"{prefix}_unit_id")
    prices = request.form.getlist(f"{prefix}_unit_price")
    discounts = request.form.getlist(f"{prefix}_discount_percent")
    descriptions = request.form.getlist(f"{prefix}_description")
    costs = request.form.getlist(f"{prefix}_unit_cost")

    lines: list[dict] = []
    for index, product_id in enumerate(products):
        product_id = parse_int(product_id)
        if not product_id:
            continue
        quantity = parse_decimal(quantities[index] if index < len(quantities) else None)
        if quantity is None:
            continue
        if quantity < 0:
            if not allow_negative:
                continue
        elif quantity == 0 and not (allow_zero or allow_negative):
            continue
        lines.append(
            {
                "product_id": product_id,
                "quantity": quantity,
                "unit_id": parse_int(units[index]) if index < len(units) else None,
                "unit_price": parse_decimal(prices[index]) if index < len(prices) else None,
                "discount_percent": parse_decimal(discounts[index]) if index < len(discounts) else Decimal("0"),
                "description": clean_str(descriptions[index], 255) if index < len(descriptions) else None,
                "unit_cost": parse_decimal(costs[index]) if index < len(costs) else None,
            }
        )
    return lines


# ---------------------------------------------------------------------------
# Error handling for service calls
# ---------------------------------------------------------------------------

def flash_business_error(error: BusinessError) -> None:
    flash(error.message(), "danger")


def handle_errors(view):
    """Turn a :class:`BusinessError` into a flash + redirect instead of a 500.

    The service layer raises domain errors; letting them escape would show a
    generic error page.  Rolling back first guarantees a half-applied change
    never survives.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except BusinessError as error:
            db.session.rollback()
            current_app_log(error)
            flash(error.message(), "danger")
            return redirect(request.referrer or url_for("dashboard.index"))

    return wrapper


def current_app_log(error: BusinessError) -> None:
    try:
        from flask import current_app

        current_app.logger.info("Business rule refused: %s %s", error.message_key, error.params)
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Common queries
# ---------------------------------------------------------------------------

def get_or_404(model, object_id):
    from app.models import __name__ as _models  # noqa: F401 - ensures models imported

    instance = db.session.get(model, object_id)
    if instance is None:
        abort(404)
    return instance


def active_only(model, query=None):
    query = query if query is not None else select(model)
    if hasattr(model, "is_active"):
        query = query.where(model.is_active.is_(True))
    return query


def current_user_id() -> int | None:
    return current_user.id if getattr(current_user, "is_authenticated", False) else None


# ---------------------------------------------------------------------------
# Select-field choices
# ---------------------------------------------------------------------------

def choices_for(model, *, label=None, blank: bool = True, blank_label: str = "—",
                order_by=None, where=None) -> list[tuple[int, str]]:
    """Build ``[(id, label)]`` for a ``SelectField`` from a model query."""
    from app.i18n import localized_name

    query = select(model)
    if where is not None:
        query = query.where(where)
    if order_by is not None:
        query = query.order_by(order_by)
    rows = db.session.execute(query).scalars().all()
    items: list[tuple[int, str]] = [(0, blank_label)] if blank else []
    for row in rows:
        if callable(label):
            text = label(row)
        elif hasattr(row, "name_ar"):
            text = localized_name(row)
        else:
            text = getattr(row, "name", None) or str(row)
        items.append((row.id, text))
    return items


def fill_choices(form, **mapping) -> None:
    """Assign choice lists to form fields, ignoring fields the form lacks."""
    for field_name, choices in mapping.items():
        field = getattr(form, field_name, None)
        if field is not None:
            field.choices = choices

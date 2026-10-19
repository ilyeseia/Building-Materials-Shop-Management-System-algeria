"""Market reference price queries and comparisons.

The functions here are deliberately read-only.  Creating an observation is a
normal catalog action in the blueprint; nothing in this module can change a
selling price, a stock quantity or a financial document.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import current_app
from sqlalchemy import func, select

from app.extensions import db
from app.models import MarketPrice, Product
from app.services.timeutil import today_local

MONEY_PLACES = Decimal("0.01")


def stale_after_days() -> int:
    try:
        return max(int(current_app.config.get("MARKET_PRICE_STALE_DAYS", 30)), 1)
    except (TypeError, ValueError):
        return 30


def latest_for_product(product_id: int) -> MarketPrice | None:
    return db.session.execute(
        select(MarketPrice)
        .where(MarketPrice.product_id == product_id)
        .order_by(MarketPrice.observed_on.desc(), MarketPrice.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_for_products(product_ids: list[int]) -> dict[int, MarketPrice]:
    """Return one latest observation per product with a single query."""
    ids = [int(value) for value in product_ids if value is not None]
    if not ids:
        return {}
    ranked = (
        select(
            MarketPrice.id.label("market_id"),
            func.row_number()
            .over(
                partition_by=MarketPrice.product_id,
                order_by=(MarketPrice.observed_on.desc(), MarketPrice.id.desc()),
            )
            .label("row_number"),
        )
        .where(MarketPrice.product_id.in_(ids))
        .subquery()
    )
    rows = db.session.execute(
        select(MarketPrice)
        .join(ranked, MarketPrice.id == ranked.c.market_id)
        .where(ranked.c.row_number == 1)
    ).scalars().all()
    return {row.product_id: row for row in rows}


def history(product_id: int, *, limit: int = 100) -> list[MarketPrice]:
    return db.session.execute(
        select(MarketPrice)
        .where(MarketPrice.product_id == product_id)
        .order_by(MarketPrice.observed_on.desc(), MarketPrice.id.desc())
        .limit(max(int(limit), 1))
    ).scalars().all()


def freshness(observation: MarketPrice, *, on_date: date | None = None) -> str:
    """Return ``verified``, ``unverified`` or ``needs_update``.

    "Needs update" is derived from the observation date; it is never stored as
    a fact in the database because it changes with the calendar.
    """
    today = on_date or today_local()
    age = (today - observation.observed_on).days
    if age > stale_after_days():
        return "needs_update"
    return observation.verification_status or "unverified"


def age_days(observation: MarketPrice, *, on_date: date | None = None) -> int:
    return ((on_date or today_local()) - observation.observed_on).days


def compare(product: Product, observation: MarketPrice | None) -> dict:
    """Compare the shop's selling price with one market observation.

    The comparison is only meaningful when both amounts refer to the same
    unit.  A market quote in tonnes and a shop price per cubic metre are
    reported side by side, but no percentage is invented across units.
    """
    if observation is None:
        return {
            "comparable": False,
            "reason": "no_observation",
            "difference": None,
            "percent": None,
            "direction": None,
        }
    if observation.unit_id != product.stock_unit_id:
        return {
            "comparable": False,
            "reason": "unit_mismatch",
            "difference": None,
            "percent": None,
            "direction": None,
        }

    selling = Decimal(product.sale_price or 0)
    observed = Decimal(observation.price or 0)
    difference = (observed - selling).quantize(MONEY_PLACES)
    percent = None
    if selling > 0:
        percent = ((observed - selling) / selling * 100).quantize(MONEY_PLACES)
    direction = "equal"
    if difference > 0:
        direction = "above"
    elif difference < 0:
        direction = "below"
    return {
        "comparable": True,
        "reason": "same_unit",
        "difference": difference,
        "percent": percent,
        "direction": direction,
    }

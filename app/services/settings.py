"""Business settings accessor.

The settings row is a singleton (``id = 1``).  Reads are cached per request so a
page render does not query the row dozens of times; writes invalidate the cache
and clear the request cache explicitly.
"""
from __future__ import annotations

import logging

from flask import g, has_app_context
from sqlalchemy import select

from app.extensions import db
from app.models import BusinessSettings

logger = logging.getLogger(__name__)

_CACHE_KEY = "_business_settings_cache"


def get_settings() -> BusinessSettings | None:
    """Return the settings row, or ``None`` when the database is not ready.

    Never raises: it is called from template filters and request hooks, including
    during first-run situations where the tables do not exist yet.

    A failed read is isolated in a **savepoint**.  It must never roll back the
    caller's transaction: this accessor is called from inside write paths (stock
    movements, numbering, the legacy migration), and discarding that work because
    a *read* failed would be a data-loss bug, not a graceful degradation.
    """
    if has_app_context() and _CACHE_KEY in g:
        return getattr(g, _CACHE_KEY)
    settings = None
    try:
        with db.session.begin_nested():
            settings = db.session.get(BusinessSettings, 1)
            if settings is None:
                settings = db.session.execute(
                    select(BusinessSettings).order_by(BusinessSettings.id).limit(1)
                ).scalars().first()
    except Exception:
        logger.debug("business settings are not readable yet", exc_info=True)
        settings = None
    if has_app_context():
        setattr(g, _CACHE_KEY, settings)
    return settings


def clear_cache() -> None:
    if has_app_context() and hasattr(g, _CACHE_KEY):
        delattr(g, _CACHE_KEY)


def ensure_settings() -> BusinessSettings:
    """Create the singleton row with configuration defaults when missing."""
    settings = get_settings()
    if settings is not None:
        return settings
    from config import Config

    settings = BusinessSettings(
        business_name="",
        currency=Config.DEFAULT_CURRENCY,
        currency_symbol=Config.DEFAULT_CURRENCY_SYMBOL,
        timezone=Config.TIMEZONE,
        default_language=Config.BABEL_DEFAULT_LOCALE,
        vat_rate=Config.DEFAULT_VAT_RATE,
        enable_weighbridge=Config.FEATURE_WEIGHBRIDGE,
        enable_fleet=Config.FEATURE_FLEET,
        enable_accounting=Config.FEATURE_ACCOUNTING,
        enable_ai=Config.FEATURE_AI,
    )
    db.session.add(settings)
    db.session.flush()
    clear_cache()
    return settings


def update_settings(changes: dict) -> BusinessSettings:
    """Apply a whitelist of changes and invalidate caches."""
    settings = ensure_settings()
    allowed = {
        "business_name", "legal_name", "tax_id", "registry_no", "activity",
        "phone", "email", "street", "commune", "locality", "wilaya_code",
        "bank_details", "invoice_footer", "currency", "currency_symbol",
        "timezone", "default_language", "decimal_separator",
        "thousands_separator", "vat_rate", "prices_include_tax",
        "valuation_method", "enable_weighbridge", "enable_fleet", "enable_ai",
        "enable_accounting", "default_warehouse_id", "weigh_tolerance_percent",
        "default_payment_terms_days", "allow_negative_stock",
    }
    for key, value in changes.items():
        if key in allowed and value is not None:
            setattr(settings, key, value)
    db.session.flush()
    clear_cache()
    return settings


def vat_rate() -> "object":
    """Configured default tax rate as a percentage (Decimal)."""
    from decimal import Decimal

    settings = get_settings()
    if settings is None or settings.vat_rate is None:
        return Decimal("0")
    return Decimal(str(settings.vat_rate))


def module_enabled(name: str) -> bool:
    settings = get_settings()
    if settings is None:
        from config import Config

        return {
            "weighbridge": Config.FEATURE_WEIGHBRIDGE,
            "fleet": Config.FEATURE_FLEET,
            "accounting": Config.FEATURE_ACCOUNTING,
        }.get(name, True)
    return {
        "weighbridge": bool(settings.enable_weighbridge),
        "fleet": bool(settings.enable_fleet),
        "accounting": bool(settings.enable_accounting),
    }.get(name, True)

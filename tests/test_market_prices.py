"""Market reference prices stay separate from the shop's selling prices."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import AuditEvent, MarketPrice, MarketVerificationStatus
from app.services import market
from tests.conftest import days_ago, today


def _observe(
    product,
    unit,
    price: str,
    *,
    source: str = "Bourse locale",
    observed_on: date | None = None,
    status: str = MarketVerificationStatus.UNVERIFIED.value,
    **extra,
) -> MarketPrice:
    observation = MarketPrice(
        product_id=product.id,
        unit_id=unit.id,
        price=Decimal(price),
        currency="DZD",
        source_name=source,
        observed_on=observed_on or today(),
        verification_status=status,
        **extra,
    )
    db.session.add(observation)
    db.session.commit()
    return observation


def test_an_observation_does_not_overwrite_the_selling_price(product, units):
    original_sale = product.sale_price
    observation = _observe(product, units["m3"], "2100")

    assert product.sale_price == original_sale
    assert market.latest_for_product(product.id).id == observation.id
    comparison = market.compare(product, observation)
    assert comparison["comparable"] is True
    assert comparison["difference"] == Decimal("300.00")
    assert comparison["percent"] == Decimal("16.67")


def test_a_different_unit_is_not_compared_or_given_a_percentage(product, units):
    observation = _observe(product, units["t"], "2500")

    comparison = market.compare(product, observation)

    assert comparison["comparable"] is False
    assert comparison["reason"] == "unit_mismatch"
    assert comparison["percent"] is None
    assert comparison["difference"] is None


def test_stale_observation_needs_update_even_when_once_verified(product, units):
    observation = _observe(
        product,
        units["m3"],
        "2000",
        observed_on=days_ago(31),
        status=MarketVerificationStatus.VERIFIED.value,
    )

    assert market.freshness(observation) == "needs_update"
    assert market.age_days(observation) == 31


def test_latest_for_products_returns_one_row_per_product_in_one_query(
    product, make_product, units
):
    second = make_product(name="Gravier 8/15", sku="GRAV-1")
    _observe(product, units["m3"], "1900", observed_on=days_ago(10))
    newest = _observe(product, units["m3"], "2050", observed_on=days_ago(1))
    other = _observe(second, units["m3"], "2300", observed_on=days_ago(2))

    latest = market.latest_for_products([product.id, second.id])

    assert latest[product.id].id == newest.id
    assert latest[second.id].id == other.id


def test_a_viewer_can_read_market_prices_but_not_submit_them(
    client, viewer_user, login, product, units
):
    login(viewer_user)
    assert client.get("/catalog/market-prices").status_code == 200
    denied = client.post(
        "/catalog/market-prices/new",
        data={
            "product_id": product.id,
            "unit_id": units["m3"].id,
            "price": "2100",
            "source_name": "Bourse locale",
            "observed_on": today().isoformat(),
        },
    )
    assert denied.status_code == 403


def test_an_authorised_user_records_an_observation_with_an_audit_trail(
    auth_client, product, units
):
    response = auth_client.post(
        "/catalog/market-prices/new",
        data={
            "product_id": product.id,
            "unit_id": units["m3"].id,
            "price": "2100",
            "currency": "DZD",
            "source_name": "Bourse locale",
            "source_reference": "Relevé du 19/10",
            "wilaya_code": 19,
            "location": "Sétif",
            "observed_on": today().isoformat(),
            "verification_status": "verified",
            "notes": "Observation de contrôle",
        },
    )

    assert response.status_code == 302
    observations = db.session.execute(select(MarketPrice)).scalars().all()
    assert len(observations) == 1
    assert observations[0].verification_status == "verified"
    assert observations[0].price == Decimal("2100.00")
    assert observations[0].product_id == product.id

    audits = db.session.execute(
        select(AuditEvent).where(
            AuditEvent.entity_type == "market_price",
            AuditEvent.action == "recorded",
        )
    ).scalars().all()
    assert len(audits) == 1


def test_duplicate_observation_is_refused(client, admin_user, login, product, units):
    login(admin_user)
    payload = {
        "product_id": product.id,
        "unit_id": units["m3"].id,
        "price": "2100",
        "currency": "DZD",
        "source_name": "Bourse locale",
        "observed_on": today().isoformat(),
        "verification_status": "unverified",
    }
    first = client.post("/catalog/market-prices/new", data=payload)
    second = client.post("/catalog/market-prices/new", data=payload)

    assert first.status_code == 302
    assert second.status_code == 200  # form is shown again with a flash
    assert db.session.execute(
        select(MarketPrice).where(MarketPrice.product_id == product.id)
    ).scalars().all().__len__() == 1


def test_product_detail_shows_the_latest_market_reference(
    client, admin_user, login, product, units
):
    _observe(
        product,
        units["m3"],
        "2100",
        source="Bourse locale",
        observed_on=today(),
    )
    login(admin_user)

    response = client.get(f"/catalog/products/{product.id}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Bourse locale" in body
    assert "2 100,00" in body

"""Reproducible read-path benchmark for a *disposable* database.

This is deliberately not a load test against production.  It seeds a scratch
database (name must contain ``bench``, ``perf`` or ``scratch``) and measures page
latency and SQL statement counts through the real Flask application and the real
service layer.

Examples
--------
SQLite scratch file::

    BENCHMARK_CONFIRM=1 python scripts/benchmark.py \
        --database-url sqlite:///instance/benchmark.db --reset \
        --products 500 --customers 1000 --invoices 2000 --iterations 15

MySQL scratch database (run from the app container; use a database that was
created for this purpose and drop it afterwards)::

    BENCHMARK_CONFIRM=1 BENCHMARK_DATABASE_URL=mysql+pymysql://.../bmshop_perf_... \
        python scripts/benchmark.py --reset --iterations 20

The script refuses to run without ``BENCHMARK_CONFIRM=1`` and a scratch-looking
database name.  It never runs ``drop_all`` unless ``--reset`` is explicit.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Running ``python scripts/benchmark.py`` puts ``scripts/`` on sys.path; the
# application package lives one level up.  Import-time only, before any app import.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("BENCHMARK_DATABASE_URL"))
    parser.add_argument("--products", type=int, default=500)
    parser.add_argument("--customers", type=int, default=1000)
    parser.add_argument("--invoices", type=int, default=2000)
    parser.add_argument("--iterations", type=int, default=15)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--reset", action="store_true", help="drop and recreate the scratch schema")
    parser.add_argument("--output", default="")
    parser.add_argument("--skip-seed", action="store_true")
    return parser.parse_args()


def _fail(message: str) -> None:
    print(f"[benchmark] ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def _is_scratch(name: str) -> bool:
    return bool(re.search(r"(bench|perf|scratch)", name or "", re.IGNORECASE))


def _rows_products(count: int, unit_id: int, category_id: int, now: datetime) -> list[dict]:
    rows = []
    for index in range(count):
        cost = Decimal("1000") + Decimal(index % 500)
        sale = cost + Decimal("350")
        rows.append(
            {
                "id": index + 1,
                "sku": f"BENCH-{index + 1:05d}",
                "barcode": f"61300000{index + 1:05d}",
                "name": f"Produit de construction {index + 1:05d}",
                "description": "Synthetic benchmark product",
                "category_id": category_id,
                "stock_unit_id": unit_id,
                "cost_price": cost,
                "sale_price": sale,
                "wholesale_price": sale - Decimal("20"),
                "contractor_price": sale - Decimal("40"),
                "reorder_level": Decimal("10"),
                "is_active": True,
                "legacy_price": sale,
                "legacy_stock_quantity": 1000,
                "legacy_unit": "m3",
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def _rows_customers(count: int, now: datetime) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append(
            {
                "id": index + 1,
                "code": f"C-{index + 1:05d}",
                "name": f"Client de construction {index + 1:05d}",
                "customer_type": "contractor",
                "wilaya_code": 16,
                "payment_terms_days": 30,
                "opening_balance": Decimal("0"),
                "credit_limit": Decimal("500000"),
                "is_blocked": False,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def seed(app, args) -> tuple[int, int, int]:
    from sqlalchemy import func, insert

    from app.extensions import db
    from app.models import (
        Customer,
        Product,
        Role,
        SalesInvoice,
        SalesInvoiceItem,
        StockLevel,
        User,
        Warehouse,
    )
    from app.models import Payment, PaymentAllocation
    from app.services import seed as seed_service
    from app.services import settings as settings_service
    from app.services import stock as stock_service
    from app.services.timeutil import utcnow

    seed_service.seed_reference_data()
    settings_service.ensure_settings()
    db.session.commit()

    admin = db.session.query(User).filter_by(username="benchmark").first()
    if admin is None:
        admin = User(username="benchmark", full_name="Benchmark", role=Role.ADMIN)
        admin.set_password("Benchmark-Pass-123")
        db.session.add(admin)
        db.session.commit()

    if db.session.query(Product).count():
        print("[benchmark] existing products found; skipping seed")
        return (
            db.session.query(Product).count(),
            db.session.query(Customer).count(),
            db.session.query(SalesInvoice).count(),
        )

    warehouse = stock_service.default_warehouse() or db.session.query(Warehouse).first()
    if warehouse is None:
        _fail("no warehouse in the scratch database")
    from app.models import Category, UnitOfMeasure

    unit = db.session.query(UnitOfMeasure).filter_by(code="m3").first() or db.session.query(UnitOfMeasure).first()
    category = db.session.query(Category).first()
    if unit is None or category is None:
        _fail("reference data missing")

    now = utcnow()
    today = now.date()
    products = _rows_products(args.products, unit.id, category.id, now)
    db.session.execute(insert(Product), products)
    stock_rows = [
        {
            "product_id": row["id"],
            "warehouse_id": warehouse.id,
            "quantity": Decimal("1000"),
            "reserved": Decimal("25"),
            "avg_cost": row["cost_price"],
            "created_at": now,
            "updated_at": now,
        }
        for row in products
    ]
    db.session.execute(insert(StockLevel), stock_rows)

    customers = _rows_customers(args.customers, now)
    db.session.execute(insert(Customer), customers)
    db.session.commit()

    invoices = []
    items = []
    payments = []
    allocations = []
    item_id = 0
    payment_id = 0
    invoice_id = 0
    for index in range(args.invoices):
        invoice_id = index + 1
        invoice_date = today - timedelta(days=index % 90)
        customer_id = (index % len(customers)) + 1
        total = Decimal("0")
        line_count = 1 + (index % 3)
        for line in range(line_count):
            item_id += 1
            product = products[(index * 3 + line) % len(products)]
            quantity = Decimal(1 + (line % 5))
            unit_price = product["sale_price"]
            line_total = (quantity * unit_price).quantize(Decimal("0.01"))
            total += line_total
            items.append(
                {
                    "id": item_id,
                    "invoice_id": invoice_id,
                    "product_id": product["id"],
                    "quantity": quantity,
                    "unit_id": unit.id,
                    "stock_quantity": quantity,
                    "conversion_factor": Decimal("1"),
                    "unit_price": unit_price,
                    "discount_percent": Decimal("0"),
                    "unit_cost": product["cost_price"],
                    "line_total": line_total,
                }
            )
        invoices.append(
            {
                "id": invoice_id,
                "number": f"BENCH-INV-{invoice_id:07d}",
                "status": "posted",
                "price_list": "retail",
                "subtotal": total,
                "discount_amount": Decimal("0"),
                "tax_amount": Decimal("0"),
                "total_amount": total,
                "vat_rate": Decimal("19"),
                "customer_id": customer_id,
                "invoice_date": invoice_date,
                "due_date": invoice_date + timedelta(days=30),
                "warehouse_id": warehouse.id,
                "created_at": now,
                "updated_at": now,
            }
        )
        if index % 2 == 0:
            payment_id += 1
            amount = (total / 2).quantize(Decimal("0.01"))
            payments.append(
                {
                    "id": payment_id,
                    "number": f"BENCH-PAY-{payment_id:07d}",
                    "direction": "inbound",
                    "payment_date": invoice_date,
                    "amount": amount,
                    "method": "cash",
                    "customer_id": customer_id,
                    "status": "posted",
                    "created_at": now,
                    "updated_at": now,
                }
            )
            allocations.append(
                {
                    "payment_id": payment_id,
                    "document_type": "sales_invoice",
                    "document_id": invoice_id,
                    "amount": amount,
                    "created_at": now,
                    "updated_at": now,
                }
            )

    db.session.execute(insert(SalesInvoice), invoices)
    db.session.execute(insert(SalesInvoiceItem), items)
    db.session.execute(insert(Payment), payments)
    db.session.execute(insert(PaymentAllocation), allocations)
    db.session.commit()
    return len(products), len(customers), len(invoices)


def benchmark(app, admin_id: int, iterations: int, warmup: int) -> dict:
    from sqlalchemy import event

    from app.extensions import db

    counter = {"queries": 0}

    @event.listens_for(db.engine, "before_cursor_execute")
    def _count_queries(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        counter["queries"] += 1

    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(admin_id)
        session["_fresh"] = True
        session["language"] = "en"

    paths = [
        "/",
        "/catalog/products",
        "/inventory",
        "/sales/invoices",
        "/finance/payments",
        "/reports/receivables",
        "/reports/stock",
        "/ai",
    ]
    results: dict[str, dict] = {}
    for path in paths:
        for _ in range(warmup):
            client.get(path)
        timings: list[float] = []
        query_counts: list[int] = []
        statuses: list[int] = []
        sizes: list[int] = []
        for _ in range(iterations):
            counter["queries"] = 0
            started = time.perf_counter()
            response = client.get(path)
            elapsed_ms = (time.perf_counter() - started) * 1000
            timings.append(elapsed_ms)
            query_counts.append(counter["queries"])
            statuses.append(response.status_code)
            sizes.append(len(response.data))
        results[path] = {
            "requests": iterations,
            "status": sorted(set(statuses)),
            "errors": sum(1 for status in statuses if status >= 400),
            "p50_ms": round(statistics.median(timings), 1),
            "p95_ms": round(sorted(timings)[max(0, int(len(timings) * 0.95) - 1)], 1),
            "mean_ms": round(statistics.mean(timings), 1),
            "min_ms": round(min(timings), 1),
            "max_ms": round(max(timings), 1),
            "queries_p50": int(statistics.median(query_counts)),
            "response_kib_p50": round(statistics.median(sizes) / 1024, 1),
        }

    # Service-level measurement of the local AI assistant (no provider network).
    from app.models import User
    from app.services import ai as ai_service

    admin = db.session.get(User, admin_id)
    ai_timings = []
    for _ in range(max(3, iterations // 2)):
        with app.test_request_context("/ai"):
            started = time.perf_counter()
            ai_service.ask("اعرض ملخص النشاط", admin)
            ai_timings.append((time.perf_counter() - started) * 1000)
    results["service:ai_local_overview"] = {
        "requests": len(ai_timings),
        "status": [200],
        "errors": 0,
        "p50_ms": round(statistics.median(ai_timings), 1),
        "p95_ms": round(sorted(ai_timings)[max(0, int(len(ai_timings) * 0.95) - 1)], 1),
        "mean_ms": round(statistics.mean(ai_timings), 1),
        "min_ms": round(min(ai_timings), 1),
        "max_ms": round(max(ai_timings), 1),
        "queries_p50": None,
        "response_kib_p50": None,
    }
    return results


def main() -> int:
    args = parse_args()
    if args.database_url:
        os.environ["BENCHMARK_DATABASE_URL"] = args.database_url
    if os.environ.get("BENCHMARK_CONFIRM") != "1":
        _fail("set BENCHMARK_CONFIRM=1 to acknowledge that this is a disposable database")

    from config import get_config

    config = get_config("benchmark")
    from sqlalchemy.engine import make_url

    database = make_url(config.SQLALCHEMY_DATABASE_URI).database or ""
    if not _is_scratch(database):
        _fail(
            f"database '{database}' does not look disposable; its name must contain "
            "bench, perf or scratch"
        )

    from app import create_app
    from app.extensions import db

    app = create_app("benchmark")
    with app.app_context():
        if args.reset:
            db.drop_all()
        db.create_all()
        if not args.skip_seed:
            print("[benchmark] seeding synthetic data (this can take a minute) ...")
            product_count, customer_count, invoice_count = seed(app, args)
        else:
            from app.models import Customer, Product, SalesInvoice

            product_count = db.session.query(Product).count()
            customer_count = db.session.query(Customer).count()
            invoice_count = db.session.query(SalesInvoice).count()

        from app.models import User

        admin = db.session.query(User).filter_by(username="benchmark").first()
        if admin is None:
            from app.models import Role

            admin = User(username="benchmark", role=Role.ADMIN)
            admin.set_password("Benchmark-Pass-123")
            db.session.add(admin)
            db.session.commit()

        print(f"[benchmark] data: {product_count} products, {customer_count} customers, {invoice_count} invoices")
        results = benchmark(app, admin.id, max(3, args.iterations), max(0, args.warmup))

        from app.services import system_status

        summary = system_status.get_system_status()
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": app.config.get("APP_VERSION"),
            "commit": app.config.get("BUILD_COMMIT") or "unknown",
            "python": summary["app"]["python"],
            "database": {
                "dialect": summary["database"]["dialect"],
                "server_version": summary["database"]["server_version"],
                "name": database,
                "size_mb": summary["database"]["size_mb"],
                "table_count": summary["database"]["table_count"],
            },
            "volumes": {
                "products": product_count,
                "customers": customer_count,
                "invoices": invoice_count,
            },
            "iterations": max(3, args.iterations),
            "results": results,
            "notes": [
                "Synthetic data in a disposable database; not a production load test.",
                "Latency is from Flask's test client in a single process, so it excludes real network and proxy time.",
                "p95 uses the nearest-rank value for the recorded sample.",
                "SQL query counts come from SQLAlchemy before_cursor_execute events.",
                "External AI providers are not called; the local deterministic assistant is measured.",
            ],
        }
        output = Path(args.output) if args.output else Path("docs") / f"PERFORMANCE_REPORT_{datetime.now(timezone.utc):%Y-%m-%d}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

        print("")
        print(f"{'path':34} {'p50':>8} {'p95':>8} {'queries':>8} {'errors':>7}")
        for path, row in results.items():
            print(
                f"{path:34} {row['p50_ms']:>7.1f}ms {row['p95_ms']:>7.1f}ms "
                f"{str(row['queries_p50']):>8} {row['errors']:>7}"
            )
        print("")
        print(f"[benchmark] report written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

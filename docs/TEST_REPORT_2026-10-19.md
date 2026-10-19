# Test and verification report - 2026-10-19

**Revision:** `7211b7a`  
**Application suite:** **203 passed, 0 failed, 0 skipped** in 73.27 s  
**Environment:** Windows x64, Python 3.14.3, pytest 9.1.1, SQLite test database  
**Production:** MySQL 8.0.46, schema revision `a1b2c3d4e5f6`, 51 objects / 179 foreign keys

> The older file `docs/TEST_REPORT.md` describes the 184-test baseline.  This
> file is the authoritative report for the current revision.

## 1. Automated suite

| Test file | Focus |
|---|---|
| `tests/test_sales.py` | Quotation, order, delivery, invoice, payment allocation, credit notes |
| `tests/test_purchasing.py` | Purchase orders, receipts, landed cost, supplier invoices and payments |
| `tests/test_stock_and_uom.py` | Ledger, reservations, transfers, counts, explicit unit conversion, release regression |
| `tests/test_weighbridge.py` | Gross/tare/net, corrections, ticket workflow |
| `tests/test_fleet.py` | Vehicles, drivers, carriers, trips, expenses, cost per trip |
| `tests/test_reporting.py` | Stock, sales, margin, ageing and transport reports |
| `tests/test_security.py` | Authentication, RBAC, CSRF, headers, rate limiting, audit |
| `tests/test_i18n.py` | Arabic/French/English catalogue parity, RTL, money/quantity formatting |
| `tests/test_legacy_and_backup.py` | Legacy migration, backup/restore services, CLI |
| `tests/test_cli.py` | User creation, password reset, database checks |
| `tests/test_health.py` | Health endpoint and database probe |
| `tests/test_market_prices.py` | Market observations, same/different unit, stale status, RBAC, audit, detail rendering |

The suite result was produced with:

```bash
.venv\Scripts\python -m pytest -q
```

## 2. MySQL-specific verification

The normal suite uses SQLite.  The following checks used disposable MySQL
databases and were run on the deployment host against the real image:

### 2.1 Migration from empty schema

```text
INFO  Running upgrade  -> 5790880d9443, 0001_initial_algerian_schema
INFO  Running upgrade 5790880d9443 -> a1b2c3d4e5f6, market reference prices
MYSQL INTEGRATION PASS
```

The integration run created a product with a selling price of 1 800.00 DZD and
two market observations at 2 000.00 and 2 050.00 DZD.  All four catalog pages
returned `200`, the create form returned `302`, and the selling price remained
**1 800.00**.  This proves the separation rule on MySQL, not only in tests.

### 2.2 Payment allocation concurrency

Two operating-system processes attempted to allocate 1 000.00 DZD each to the
same 1 000.00 DZD invoice.  The winner held the transaction for two seconds
before commit.

```text
worker 1: ALLOCATED (exit 0)
worker 2: REFUSED payment.exceeds_balance (exit 2)
invoice paid=1000.00 balance=0.00
PASS
```

### 2.3 Production data audit

| Check | Result |
|---|---|
| Negative quantity / negative reservation / reserved > quantity | 0 rows |
| Orphan stock levels or movements | 0 rows |
| Overallocated payments | 0 rows |
| Invoice balance below zero | 0 rows |
| Unbalanced invoice totals | 0 rows |
| Concurrent payment over-settlement | prevented and verified |

The production database is nearly empty (1 product, 7 categories, 0 stock
movements, 0 customers, 0 invoices, 0 payments, 25 audit events, 1 user), so
these results do **not** represent a performance or capacity test.

## 3. Backup and restore

| Drill | Result |
|---|---|
| Production backup `bmshop-20261019-131841` | checksum valid, gzip valid, completion marker present |
| Isolated restore (`verify-backup.sh`) | 51/51 tables, revision `a1b2c3d4e5f6`, 179 FKs, all tables `CHECK TABLE` OK, zero stock/payment invariant violations, scratch DB dropped |
| Disposable destructive restore (`restore.sh`) | safety backup taken, 51/51 tables, `mysqlcheck` OK, report written, scratch DB dropped |
| Failure path without target privileges | aborted before import; target unchanged |

## 4. Deployment

| Check | Result |
|---|---|
| `docker compose build app` | PASS |
| `flask db upgrade` on production | PASS, `5790880d9443 -> a1b2c3d4e5f6` |
| app and db container health | healthy |
| `GET /healthz` | 200 |
| `GET /login` | 200 |
| Unauthenticated catalog routes | 302 to sign-in |
| Existing business rows | preserved |

## 5. Not tested / blocked

* Authenticated browser acceptance (dashboard, forms, both themes) is **BLOCKED**
  because the current production administrator password is unknown.
* Real mobile/tablet browser testing is **NOT TESTED** in this iteration.
* Multi-user load, p50/p95 latency, slow-query analysis and capacity are **NOT
  TESTED** because production has no representative data.
* AI workspace, licensing and activation are **not implemented**, so they have no
  test results.
* No real weighing scale, PostgreSQL, MySQL 8.4+, MariaDB, point-in-time
  recovery or multi-host deployment was tested.

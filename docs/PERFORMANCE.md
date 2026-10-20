# Performance report and method

Status: **PARTIAL — measured on synthetic SQLite and MySQL scratch databases;
not a concurrent load test or a production capacity test.**

This report records a real measurement, including the defect it found. It does
not claim production throughput from a test client.

---

## 1. What was measured

Tool: `scripts/benchmark.py` (committed). It seeds a **disposable** database whose
name contains `bench`, `perf` or `scratch`, refuses to run without
`BENCHMARK_CONFIRM=1`, and measures through the real Flask application, service
layer, templates and SQLAlchemy session.

Command used:

```powershell
$db = (Join-Path (Get-Location) 'instance\benchmark.db') -replace '\\','/'
$env:BENCHMARK_CONFIRM='1'
.\.venv\Scripts\python.exe scripts\benchmark.py `
  --database-url "sqlite:///$db" --reset `
  --products 300 --customers 500 --invoices 1000 --iterations 8
```

Environment: Windows, Python 3.14.3, Flask 3.1.x, SQLAlchemy 2.1.x, SQLite file
scratch database. Volumes: 300 products, 500 customers, 1,000 posted invoices,
1,500 invoice lines, 500 payments/allocations, 300 stock levels.

The report JSON is committed next to this file:
`docs/PERFORMANCE_REPORT_2026-10-19.json`.

---

## 2. Defect found: per-invoice N+1

The first run showed:

| Page | p50 | p95 | SQL statements |
|---|---:|---:|---:|
| `/` dashboard | 992.8 ms | 993.5 ms | **1,014** |
| `/reports/receivables` | 1,010.9 ms | 1,017.1 ms | **1,003** |
| `/sales/invoices` | 127.2 ms | 127.5 ms | **139** |
| `service:ai_local_overview` | 978.6 ms | 978.6 ms | — |

Root cause: `SalesInvoice.paid_amount` and `credited_amount` are properties that
issue a `SUM` query for **each invoice**. The dashboard, receivables ageing and
sales list called them inside loops. With 1,000 invoices that is roughly 1,000
extra round trips, and the local AI overview inherited the same problem through
the dashboard KPIs.

## 3. Fix

* `app/services/reporting.py` now aggregates allocations once with
  `_paid_by_document()` and credit notes once with `_credited_by_invoice()`.
* `receivable_total`, `receivables_aging`, `payable_total` and `payables_aging`
  use those maps instead of per-invoice properties.
* `invoice_settlements()` batches balance/status for the sales list.
* No calculation changed: the same posted-payment and non-cancelled credit-note
  rules are applied. Only the query pattern changed.
* Regression tests fail if the dashboard or the batch helper starts issuing a
  query per invoice (`tests/test_reporting.py`).

## 4. After the fix

Same database, same volumes, 8 iterations:

| Page | p50 | p95 | SQL statements | Change |
|---|---:|---:|---:|---:|
| `/` dashboard | 100.8 ms | 109.1 ms | 16 | 1,014 → 16 queries |
| `/reports/receivables` | 127.1 ms | 133.8 ms | 4 | 1,003 → 4 |
| `/sales/invoices` | 16.5 ms | 16.8 ms | 4 | 139 → 4 |
| `/catalog/products` | 18.6 ms | 23.2 ms | 5 | — |
| `/inventory` | 18.3 ms | 21.8 ms | 3 | — |
| `/finance/payments` | 13.6 ms | 13.8 ms | 2 | — |
| `/reports/stock` | 88.9 ms | 92.5 ms | 3 | — |
| `/ai` (page) | 6.8 ms | 6.9 ms | 1 | — |
| `service:ai_local_overview` | 93.6 ms | 95.4 ms | — | 978.6 → 93.6 ms |

The remaining dashboard time is mostly aggregation over 1,000 invoices and the
low-stock query; it is a candidate for further indexing, not a correctness issue.

## 5. MySQL scratch run

The same workload was executed against a disposable **MySQL 8.0.46** database on
the production host.  The database was created only for this test, migrated
through `b2c3d4e5f6a7`, then reset and seeded by the benchmark.  It was dropped
after the run.  The report is
`docs/PERFORMANCE_REPORT_MYSQL_2026-10-19.json`.

Environment: MySQL 8.0.46 in Docker, Python 3.13.16, 300 products / 500
customers / 1,000 invoices, 8 iterations, 2 warm-up iterations, database size
3.88 MB, 52 tables.  The image was built from commit `a1d9239`.  The JSON's
`commit` field says `unknown` because the one-off `docker compose run` did not
receive the build-argument environment; the image labels carry the commit.

| Page | p50 | p95 | SQL statements |
|---|---:|---:|---:|
| `/` dashboard | 130.6 ms | 133.8 ms | 16 |
| `/catalog/products` | 22.8 ms | 23.4 ms | 5 |
| `/inventory` | 21.4 ms | 21.8 ms | 3 |
| `/sales/invoices` | 24.8 ms | 28.4 ms | 4 |
| `/finance/payments` | 17.3 ms | 18.4 ms | 2 |
| `/reports/receivables` | 147.3 ms | 160.4 ms | 4 |
| `/reports/stock` | 49.9 ms | 51.5 ms | 3 |
| `/ai` (page) | 5.0 ms | 5.0 ms | 1 |
| `service:ai_local_overview` | 124.9 ms | 126.4 ms | — |

The query counts are identical to SQLite, which confirms the N+1 fix is not a
SQLite-only effect.  MySQL latency is higher for the dashboard and receivables
because the aggregation runs in the database over 1,000 rows and their lines;
the absolute values still exclude network, TLS, proxy and browser time.

---

## 6. How to reproduce

SQLite scratch:

```bash
BENCHMARK_CONFIRM=1 python scripts/benchmark.py \
  --database-url "sqlite:///$(pwd)/instance/benchmark.db" --reset \
  --products 300 --customers 500 --invoices 1000 --iterations 20
```

MySQL scratch (do **not** point this at the production database):

```bash
# 1. create an empty database named e.g. bmshop_perf_20261019 and grant the app user
# 2. from the app container, with a DATABASE_URL for that scratch database:
BENCHMARK_CONFIRM=1 BENCHMARK_DATABASE_URL="mysql+pymysql://..." \
  python /app/scripts/benchmark.py --reset --iterations 20
# 3. drop the scratch database after recording the result
```

The script refuses a database name without `bench`/`perf`/`scratch` and refuses
to run without `BENCHMARK_CONFIRM=1`.

---

## 7. Honest limitations

* **Scratch databases, not production capacity.** Both SQLite and MySQL 8.0.46
  scratch runs measured the same query counts and similar relative behaviour,
  but neither is the customer's production data volume.  MySQL latency is now
  measured; capacity still needs a concurrent test.
* **Test client, not network.** The numbers exclude TLS, the reverse proxy and
  real browser rendering. They measure server-side application work.
* **Synthetic data.** Names, prices and distribution are generated; they are not
  the customer's real sales mix.
* **Single process.** This is not a concurrent load test. Concurrency safety was
  tested separately for payments and stock, not throughput.
* **No slow-query log.** MySQL `slow_query_log` and `performance_schema` were not
  configured; p95 per page is the current evidence.
* **Only read paths.** Write-path throughput (invoice posting, stock movements)
  is not benchmarked here.

Until a concurrent load test is run on the target host, capacity planning
remains **PARTIAL**.

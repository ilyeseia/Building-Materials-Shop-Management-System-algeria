# Test and verification report - 2026-10-19 (updated 2026-10-20)

**Revision:** commit `cc5ed50` (deployed to production and pushed to `algeria`)  
**Application suite:** **229 passed, 0 failed, 0 skipped** in 80.09 s  
**Environment:** Windows x64, Python 3.14.3, pytest 9.1.1, SQLite test database  
**Production deployment:** MySQL 8.0.46, revision `b2c3d4e5f6a7` (head),
52 tables / 180 foreign keys.  Pre-update backup `bmshop-20261020-070510` and
post-deployment backup `bmshop-20261020-073306` were both verified in isolation.

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
| `tests/test_ai.py` | Read-only tools, permission enforcement, audit, provider failure fallback, injection boundary, truncation |
| `tests/test_licensing.py` | Community/valid/expired/invalid license, HMAC signature, explicit non-enforcement |
| `tests/test_system_status.py` | Status page permissions/secrets, backup marker, `doctor`/`version`/`license-status` CLI |
| `tests/test_reporting.py` (new cases) | Query-count regression for dashboard and invoice settlement batch |

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

### 2.4 AI workspace (SQLite suite; provider mocked)

| Check | Result |
|---|---|
| `/ai` requires `ai.view`; a driver role receives 403 | PASS |
| Admin page renders with CSRF form and read-only tool list | PASS |
| Local assistant executes permitted tools and returns an answer | PASS |
| `execute_tool` re-checks permissions server-side; a cashier cannot read stock | PASS |
| Unknown/write tool name (`delete_product`) is refused before any handler | PASS |
| Question and answer stored in `ai_interactions`; `ai_query` audit event written | PASS |
| No product row changes during an AI question | PASS |
| External provider failure falls back locally with status `unavailable` | PASS |
| Provider tool result is passed with `tool` role, never as a system message | PASS |
| Empty question refused; oversized question truncated, not rejected | PASS |
| No database rollback on a read-only tool failure | PASS |

No test reaches the network.  The provider tests monkeypatch the HTTP client with
a fake response; they do not prove an external provider account works.  That
requires a customer key and is **NOT TESTED** here.

### 2.5 Licensing and system status

| Check | Result |
|---|---|
| Missing license reports `community`, never enforces | PASS |
| Valid unsigned license reports `unverified`, never enforces | PASS |
| Expired/malformed/tampered license reports `expired`/`invalid`, never enforces | PASS |
| HMAC-signed license verifies; a changed licensee fails the signature | PASS |
| `/settings/system` requires `settings.manage`; viewer receives 403 | PASS |
| Status page renders version and checks but not `SECRET_KEY` | PASS |
| `flask doctor` reports pass/warn/fail; `version` and `license-status` exit 0 | PASS |

### 2.6 Measured performance (synthetic SQLite scratch)

A disposable database was seeded with 300 products, 500 customers and 1,000
posted invoices (1,500 lines, 500 payments).  The benchmark found and then
verified the removal of a per-invoice N+1:

| Page | Before | After |
|---|---|---|
| Dashboard | 1,014 queries, 993 ms p50 | **16 queries, 101 ms p50** |
| Receivables | 1,003 queries, 1,011 ms p50 | **4 queries, 127 ms p50** |
| Sales invoice list | 139 queries, 127 ms p50 | **4 queries, 17 ms p50** |
| Local AI overview | 979 ms p50 | **94 ms p50** |

The same volumes were also measured on a disposable **MySQL 8.0.46** database on
the production host: dashboard **16 queries / 130.6 ms p50 / 133.8 ms p95**,
receivables **4 / 147.3 / 160.4 ms**, sales list **4 / 24.8 / 28.4 ms**, local AI
overview **124.9 ms p50**.  The query counts match SQLite, confirming the fix is
not SQLite-only.  Report: `docs/PERFORMANCE_REPORT_MYSQL_2026-10-19.json`.

Details, method and caveats are in `docs/PERFORMANCE.md`.  This is **not** a
concurrent-load result; capacity remains **PARTIAL / NOT TESTED** for concurrency.

## 3. Backup and restore

| Drill | Result |
|---|---|
| Production backup `bmshop-20261019-131841` | checksum valid, gzip valid, completion marker present |
| Isolated restore (`verify-backup.sh`) | 51/51 tables, revision `a1b2c3d4e5f6`, 179 FKs, all tables `CHECK TABLE` OK, zero stock/payment invariant violations, scratch DB dropped |
| Disposable destructive restore (`restore.sh`) | safety backup taken, 51/51 tables, `mysqlcheck` OK, report written, scratch DB dropped |
| Failure path without target privileges | aborted before import; target unchanged |

## 4. Production deployment (verified 2026-10-20)

| Check | Result |
|---|---|
| `docker compose build app` | PASS (`1.1.0 / cc5ed50`) |
| Pre-update backup | `bmshop-20261020-070510` verified (51/51 tables, revision `a1b2c3d4e5f6`, `CHECK TABLE` pass) |
| `flask db upgrade` on production | PASS, `a1b2c3d4e5f6 -> b2c3d4e5f6a7` |
| Downgrade/re-upgrade safety | Tested on disposable MySQL scratch DB `bmshop_scratch_a1d9239` before dropping |
| App container recreation | Non-destructive, `flask version` reports `1.1.0 (cc5ed50)` |
| `GET /healthz` | 200 `{"database": true, "status": "ok"}` |
| `GET /login` | 200 |
| Unauthenticated `/ai` | 302 to `/login?next=%2Fai` |
| Authenticated pages pass | 14 pages GET 200 (`/`, `/catalog/products`, `/catalog/market-prices`, `/inventory`, `/sales/invoices`, `/purchasing/orders`, `/finance/payments`, `/reports/receivables`, `/reports/stock`, `/ai`, `/settings`, `/settings/system`, `/users`, `/audit`) |
| AI workspace execution | POST `/ai/ask` handled locally (`local`), answer returned, `ai_interactions` row #2 appended, `ai_query` audit event written |
| Post-deployment backup | `bmshop-20261020-073306` verified in isolation (52/52 tables, revision `b2c3d4e5f6a7`, 180 FKs, zero stock/payment violations) |
| Playwright viewports (1440x900, 834x1112, 390x844) | Zero horizontal overflow on `/ai`, `/settings/system` and `/`; mobile drawer opens; dark/light theme persists; AR/FR/EN switch verified |
| Existing business rows | Preserved (products=2, users=2, audit=53) |

## 5. Not tested / blocked

* **Branded-Chrome public TLS path:** the public URL via Tailscale remains
  blocked for branded Chrome and server-side `curl` due to an SSL/connection
  handshake failure, while local `curl` and older Chromium 1179 succeed.
  No production proxy config was changed.
* **External AI provider:** no external API key exists.  All tests use the local
  assistant or a mocked client; real external LLM behavior is **NOT TESTED**.
* **Off-site backups and PITR:** `scripts/backup-offsite.sh` is guarded and
  verified to report "skipped" when unconfigured, but no remote credentials
  exist and no point-in-time recovery has been tested.
* **Concurrent multi-user load:** absolute single-process performance is
  measured, but high-concurrency write load is **NOT TESTED**.

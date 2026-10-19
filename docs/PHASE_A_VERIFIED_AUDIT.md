# Phase A - Verified Production Audit and Phase B/D Delivery

**Audit date:** 2026-10-19  
**Deployed revision:** `7211b7a`  
**Production URL:** `https://building-materials-shop-management-system.tail7d68dd.ts.net/`  
**Database:** MySQL 8.0.46, `Africa/Algiers`, DZD  
**Test suite at this revision:** **203 passed** in 73.27 s (Windows x64, Python 3.14.3, SQLite test database)

> Evidence rule: statements below are based on the deployed revision, a live
> read-only inspection, a scratch MySQL database, or the actual test suite.
> Nothing is inferred from old README claims. Statuses use PASS, PARTIAL, FAIL,
> NOT TESTED and BLOCKED. Production credentials are never recorded here.

---

## 1. Executive result

The application is healthy and the upgrade was deployed non-destructively. The
production database was backed up first, the schema moved from
`5790880d9443` to `a1b2c3d4e5f6`, and all existing business rows were kept.

This iteration closed four concrete reliability gaps found in Phase A:

| Gap found in Phase A | Change delivered | Verification |
|---|---|---|
| Two cashiers could over-settle one invoice because allocation checked an unlocked balance | Invoice and payment rows are now loaded with `SELECT ... FOR UPDATE`; allocation rows are read with a locking read to defeat MySQL REPEATABLE READ snapshots | Real two-process MySQL drill: one allocation succeeded, the other was refused with `payment.exceeds_balance`; invoice paid 1 000.00, balance 0.00 |
| Releasing more than the reserved stock clamped the reservation to zero but wrote the full requested delta to the ledger | `stock.release` now refuses the operation and rolls back with the reservation untouched | Unit regression test; ledger/cache no longer diverge |
| Restore had no mandatory checksum, no pre-restore safety copy and no post-restore verification | Checkpointed restore with mandatory checksum, abort-on-safety-backup-failure, table-count comparison, `mysqlcheck`, `alembic_version` check, negative-stock check and a written report | Full restore into a disposable MySQL database: 51/51 tables, revision `a1b2c3d4e5f6`, 1 product, 25 audit events; scratch database dropped |
| Product list performed one stock query per product | Added `stock.on_hand_by_product()` and `market.latest_for_products()` batch queries | Route renders on MySQL; queries are batched |

It also delivered the market-reference-price module, kept strictly separate from
selling prices.

---

## 2. Production baseline (verified)

Read-only inspection of the live database and service:

| Check | Result |
|---|---|
| Container health | app **healthy**, db **healthy** |
| `GET /healthz` | **200** |
| `GET /login` | **200** |
| Unauthenticated `/catalog/products` and `/catalog/market-prices` | **302** to `/login?next=...` (access control working) |
| Schema revision | `a1b2c3d4e5f6` |
| Tables | **51** |
| Foreign keys | **179** |
| Products | 1 |
| Categories | 7 |
| Stock levels / movements | 0 / 0 |
| Customers / suppliers | 0 / 0 |
| Sales orders / invoices | 0 / 0 |
| Supplier invoices | 0 |
| Payments / allocations | 0 / 0 |
| Audit events | 25 |
| Users | 1 |

The near-empty production database means **no representative performance or
capacity conclusion can be drawn from it**. Integrity checks pass, but they are
not a load test.

---

## 3. Reliability evidence

### 3.1 Payment allocation concurrency (PASS)

Executed against a disposable MySQL database with two separate OS processes and
the real service layer:

```text
worker 1: ALLOCATED (exit 0)
worker 2: REFUSED payment.exceeds_balance (exit 2)
invoice paid=1000.00 balance=0.00
PASS
```

The winner sleeps two seconds before commit, so the second process blocks on
the invoice row lock and then sees the committed allocation. The invoice cannot
be paid twice. The check is reproducible with:

```bash
DATABASE_URL=... python scripts/concurrency-check.py
```

Run it only on a disposable database.

### 3.2 Stock release (PASS at unit level)

`app/services/stock.py` no longer clamps silently. A release above the
reservation raises `stock.release_exceeds_reserved`; the transaction rolls back
and both `reserved` and `available` stay correct. Covered by
`tests/test_stock_and_uom.py::test_release_above_the_reservation_is_refused_without_clamping`.

### 3.3 Backup verification drill (PASS)

The deployed `scripts/verify-backup.sh` restored the latest production backup
into `bmshop_verify_*`:

| Check | Result |
|---|---|
| SHA-256 checksum | PASS |
| gzip + mysqldump completion marker | PASS |
| Tables in dump / restored | 51 / 51 |
| `alembic_version` | `a1b2c3d4e5f6` |
| `CHECK TABLE` on every restored table | all OK |
| Foreign keys | 179 |
| Negative / over-reserved stock | 0 |
| Orphan stock levels | 0 |
| Overallocated payments | 0 |
| Scratch database cleanup | PASS (dropped) |
| Live database writes | none |

### 3.4 Destructive restore drill (PASS, disposable target)

The deployed `scripts/restore.sh` was run against a disposable database using
the latest backup:

* mandatory checksum verification passed;
* a safety backup of the target was taken first;
* table count 51 before/after matched;
* `mysqlcheck` passed;
* revision and negative-stock checks passed;
* a restore report was written;
* `RESTORE_FILES=0` skipped file replacement for the database-only drill;
* the scratch database was dropped afterwards.

The first attempt deliberately failed because the target database lacked the
application user's privileges; the script **aborted before importing** and did
not modify the target. This is the intended safety behaviour.

### 3.5 Market prices on MySQL (PASS)

A disposable MySQL database was migrated from an empty schema through both
migrations, seeded, and exercised through the real Flask test client:

```text
GET /catalog/products                 -> 200
GET /catalog/products/1               -> 200
GET /catalog/market-prices            -> 200
GET /catalog/market-prices/new        -> 200
POST /catalog/market-prices/new       -> 302
market_prices rows: 2
product sale price after observation: 1800.00
MYSQL INTEGRATION PASS
```

The observed market prices (2 000.00 and 2 050.00) did **not** change the
selling price (1 800.00). This is the core business rule of the module.

---

## 4. What changed

### Market reference prices

* `app/models/market.py`: append-only `market_prices` observations, with
  product, explicit unit, price, currency, source, source reference, wilaya,
  location, observation date, verification status and notes.
* `app/services/market.py`: latest-per-product batch query, history, age,
  freshness and same-unit comparison. A percentage is never computed across
  different units.
* `app/blueprints/catalog.py`: list, create and edit screens, search/status
  filters, audit events.
* Product list and product detail show the latest observation, its date,
  status and same-unit difference from the selling price.
* `migrations/versions/a1b2c3d4e5f6_market_prices.py`: new table and indexes.
* Arabic, French and English strings, including RTL labels.

### Financial and stock hardening

* `app/services/payments.py`: row locking and locking reads; invoice-first lock
  order; revalidation after acquiring locks.
* `app/services/stock.py`: batch on-hand query; release refuses over-release.
* `scripts/backup.sh`: temporary files, gzip validity, dump completion marker,
  checksums, manifest, `latest.json`, retention, suffix validation.
* `scripts/restore.sh`: mandatory checksum, safety backup, table count,
  `mysqlcheck`, revision and stock checks, restore report.
* `scripts/verify-backup.sh`: isolated restore drill in a scratch database.
* `scripts/concurrency-check.py`: reproducible MySQL concurrency proof.

### Tests

* 203 passing, including 8 new market-price tests and 1 stock regression test.
* MySQL-specific paths were exercised separately because the normal suite uses
  SQLite.

---

## 5. Risk register after this iteration

| Severity | Risk | Status |
|---|---|---|
| Critical | Concurrent payment could over-settle an invoice | **Resolved and verified on MySQL** |
| Critical | Over-release wrote a false stock ledger delta | **Resolved, unit-tested** |
| High | Restore could overwrite data without a recovery copy or integrity check | **Resolved and drill-tested** |
| High | No automatic backup schedule or off-site copy | **PARTIAL** - documented cron + off-site commands; not configured on the host |
| High | Production admin password is unknown, blocking authenticated browser verification | **BLOCKED** - audit log shows a password change on 2026-10-19 10:12:48; the stored helper password is stale |
| High | No representative performance baseline or load test | **NOT TESTED** - production has almost no business data |
| High | AI workspace is not implemented | **FAIL (not implemented)** |
| High | Licensing, activation and commercial packaging are not implemented | **FAIL (not implemented)** |
| Medium | No version/status administration screen | **FAIL (not implemented)** |
| Medium | Observability is limited to container logs, `/healthz` and the audit log | **PARTIAL** |
| Medium | Backups are not encrypted by the script | **PARTIAL** - documented; relies on encrypted storage off-site |
| Medium | No point-in-time recovery (binary logs) | **FAIL (not implemented)** |
| Medium | Single-host deployment, no HA or managed database | **PARTIAL** - documented limitation |
| Low | Weighbridge integration is manual only | **PARTIAL** - adapter boundary, no serial/TCP driver |
| Low | No batch/lot tracking | **FAIL (not implemented)** |
| Low | No e-invoicing / tax-authority integration | **FAIL (not implemented)** |

---

## 6. Master-prompt phase status

| Phase | Scope | Status |
|---|---|---|
| A | Verified audit, data/security/deployment baseline | **PASS** |
| B | Critical reliability: concurrency, stock ledger, restore safety | **PASS** for the four fixes above; **PARTIAL** for scheduling/off-site automation |
| C | Measured performance and capacity tuning | **NOT TESTED** - no representative data; only one N+1 query removed and verified by inspection |
| D | Market reference prices, separate from selling prices | **PASS** on SQLite suite and disposable MySQL |
| E | AI workspace with read-only, permission-enforced, audited tools | **FAIL (not implemented)** |
| F | Commercial productization, licensing, version/status administration | **FAIL (not implemented)**; deployment and backup documentation improved |
| G | Automatic/off-site backups, observability, update/rollback | **PARTIAL** - verified backup/restore and write-up; no scheduler, off-site job, notification or PITR |
| H | Final end-to-end, browser, mobile, accessibility and production verification | **PARTIAL / BLOCKED** - public pages verified; authenticated dashboard/list/form and theme verification blocked by the unknown admin password |

---

## 7. Remaining work, in priority order

1. **Unblock production acceptance:** obtain the current admin password or
   explicit approval to reset it, then verify dashboard, product list/form,
   market-price screens, Arabic RTL, French/English and both themes at desktop,
   tablet and mobile widths.
2. **Configure scheduled backups and an off-site destination** with a tested
   restore from the off-site copy; add failure notification.
3. **Performance baseline:** seed a disposable MySQL database with realistic
   volumes (thousands of products, tens of thousands of movements/invoices),
   measure p50/p95 page latency and slow queries, then optimise the measured
   bottlenecks.
4. **AI workspace:** provider-agnostic, read-only tools, explicit permission
   checks, prompt-injection boundaries, audit of every query/answer, timeout and
   provider-failure fallback. Do not let provider failure affect sales/stock.
5. **Commercial productization:** version/build display, licensing with a
   non-destructive evaluation mode, installation/onboarding wizard, release
   packaging, checksums and rollback rehearsal.
6. **Observability:** structured request/error logs, backup status, disk/DB
   health, and an administrator status screen.
7. **Point-in-time recovery** and off-site encryption.

---

## 8. Evidence map

| Claim | Evidence |
|---|---|
| Production healthy, revision, table/FK counts | live `docker compose ps`, `/healthz`, `/login`, SQL inspection on 2026-10-19 |
| 203 tests | `.venv\Scripts\python -m pytest -q` at `7211b7a` |
| Payment concurrency | two-process MySQL drill via `scripts/concurrency-check.py` |
| Backup restore verification | `scripts/verify-backup.sh` on `bmshop-20261019-131841` |
| Destructive restore safety | disposable `bmshop_restore_test` run of `scripts/restore.sh` |
| Market prices MySQL | disposable `bmshop_integration_test` run of Flask test client |
| Stock release regression | `tests/test_stock_and_uom.py` |
| Deployment steps | `docs/DEPLOYMENT.md` sections 7-8 |

No production business records were deleted or modified by these drills.

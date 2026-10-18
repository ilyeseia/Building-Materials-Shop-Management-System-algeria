# PROJECT AUDIT — Building-Materials-Shop-Management-System

**Audit date:** 2026-10-18
**Auditor role:** principal architect / ERP consultant / security reviewer (automated agent)
**Repository:** `C:\Users\seia\Desktop\esp-claw\Building-Materials-Shop-Management-System`
**Audited revision:** `bbb8798` (`Update config.py`, 2025-08-14) — branch `main`, working tree clean at audit time.

> Evidence rule used throughout: every claim below is tied to a file and line in the audited revision.
> Claims that could not be verified in source are labelled **[NOT VERIFIED]**.
> Nothing in this document is inferred from the README alone.

---

## 1. Inventory of the audited revision (verified)

| Path | Size | Notes |
|---|---|---|
| `app.py` | 101,884 B / 2,087 lines | Entire application: i18n dicts, models, routes, PDF, startup |
| `config.py` | 1,185 B | 3 config classes (development/production/testing) |
| `schema.sql` | 3,397 B | DDL for 6 tables + sample rows |
| `templates/base.html` | 61,850 B | Layout, ~1,200 lines of inline CSS/JS |
| `templates/orders.html` | 35,833 B | |
| `templates/customers.html` | 22,395 B | |
| `templates/dashboard.html` | 22,275 B | |
| `templates/products.html` | 21,647 B | |
| `templates/payments.html` | 18,897 B | |
| `templates/login.html` | 17,432 B | |
| `templates/reports.html` | 16,517 B | |
| `requirements.txt` | 190 B | 11 pinned packages |
| `start.bat` | 877 B | Windows launcher |
| `README.md` | 11,098 B | Feature claims (several unverified — see §6) |
| `LICENSE` | 1,084 B | MIT |

**Absent (verified by recursive listing):** test suite, `tests/`, `docs/`, database migrations,
`.gitignore`, `.env.example`, `Dockerfile`, `docker-compose.yml`, `wsgi.py`, CI configuration,
static asset directory (all CSS/JS is inline in `base.html`, all CDN assets are remote).

**Stack (from `requirements.txt`):** Flask 2.3.3, Flask-SQLAlchemy 3.0.5, Flask-Login 0.6.3,
Flask-WTF 1.1.1, WTForms 3.0.1, PyMySQL 1.1.0, cryptography 41.0.7, reportlab 4.0.4,
python-dateutil 2.8.2, Werkzeug 2.3.7.

**Architecture:** single-module Flask app. No application factory, no blueprints, no service
layer. `db = SQLAlchemy(app)` is created at import time (`app.py:24`).

---

## 2. Database schema (as implemented vs. `schema.sql`)

`db.create_all()` at startup (`app.py:2084`) creates tables from the SQLAlchemy models, **not**
from `schema.sql`. The two are not identical, and `schema.sql` is effectively dead code that
still contains a broken admin row.

| Table | Model file:line | Notable observations |
|---|---|---|
| `users` | `app.py:1082-1087` | Only `username` + `password_hash`. **No roles/permissions.** |
| `customers` | `app.py:1089-1099` | No wilaya/commune, no credit limit, no opening balance, no tax id. |
| `products` | `app.py:1101-1112` | `stock_quantity` is **INTEGER** (`app.py:1106`) — cannot hold tonnes/m³. No category, brand, SKU, barcode, supplier, cost price, or stock unit. |
| `orders` | `app.py:1114-1128` | No order number, no state machine, no warehouse, no salesperson. |
| `order_items` | `app.py:1130-1137` | `quantity` INTEGER — same fractional-quantity defect. |
| `payments` | `app.py:1139-1147` | Only customer receipts. **No supplier payments, no allocations, no AP at all.** |

**Missing entities** (required by the target scope): categories, brands, suppliers, units of
measure, conversion rules, warehouses, stock movements/ledger, stock transfers, physical counts,
quotations, delivery notes, credit notes/returns, purchase orders, goods receipts, supplier
invoices, vehicles, drivers, carriers, trips, trip expenses, weighbridge devices, weighing
tickets, roles/permissions, audit events, settings, accounting records.

**Indexes:** MySQL creates indexes for the four foreign keys. `orders.order_date`,
`orders.delivery_date`, `payments.payment_date` are **not** indexed, yet they are the columns used
by every dashboard and report filter (`app.py:1283-1297`, `1821-1828`, `1801-1803`).

**`schema.sql` defects:**
- `schema.sql:76-77` inserts a **literal placeholder** password hash
  (`'pbkdf2:sha256:600000$admin123$hash_placeholder'`). If that file is executed as the README
  instructs (`README.md:83`), the resulting `admin` row can never authenticate.
- `schema.sql:80-94` duplicates the sample data that `insert_sample_data()` also inserts
  (`app.py:1235-1268`).
- `schema.sql` has no `DROP`/migration path and is not used by the application.

**No migration tool.** Schema changes are applied by `db.create_all()` (additive only) or by the
destructive `/recreate-db` route (see §4). There is no versioned, reversible migration.

---

## 3. Business logic defects (data integrity)

### 3.1 Money is computed with binary floating point — everywhere
`DECIMAL(10,2)` is used in the schema, but every aggregation converts to `float`:

- `app.py:1285` `monthly_sales = sum(order.total_amount ...)`
- `app.py:1291-1293` pending amount
- `app.py:1592` order total
- `app.py:1635` `paid_amount`
- `app.py:1770` payment-status recalculation
- `app.py:1940-1945` invoice totals

This violates the "no binary floating point for financial calculations" requirement and produces
cent-level drift in aging/settlement logic.

### 3.2 Stock cannot represent commercial quantities
`products.stock_quantity` and `order_items.quantity` are `INTEGER`
(`schema.sql:31,56`; `app.py:1106,1135`). Selling 2.5 t of steel or 12.5 m³ of sand is
impossible. There is **no unit-of-measure model and no conversion mechanism** anywhere in the
revision; `products.unit` is a free-text string (`app.py:1107`) with no validation.

### 3.3 Oversell race condition
`app.py:1577-1581` reads `product.stock_quantity`, compares, then decrements — a classic
read-check-write with **no row lock and no atomic UPDATE**. Two concurrent orders for the same
product can both pass the check and both decrement, producing negative stock. No `SELECT … FOR
UPDATE`, no `with_for_update()`, no database-level constraint.

### 3.4 Customer deletion silently corrupts inventory
`app.py:1405-1427`: deleting a customer deletes all of their orders and payments **without
restoring the stock** that those orders consumed. Compare `app.py:1701-1706`, where order
deletion *does* restore stock. The same action therefore has two different inventory effects
depending on the path taken.

### 3.5 Product deletion destroys order history
`app.py:1512-1529`: deleting a product deletes its `order_items` rows. The parent order keeps its
`total_amount`, so historical invoices/orders no longer reconcile with their line items.

### 3.6 Fabricated financial records
`app.py:1662-1664`: setting an order to `Partial` **invents a payment of 50 % of the order total**
when no amount is supplied. A ledger entry is created for money that was never received.

### 3.7 Stock movements are unauditable
Stock changes happen as direct in-place mutation (`app.py:1581`, `1706`). There is no stock
ledger, no source-document link, no user, no timestamp, no reason. The audit requirement ("every
stock movement must be traceable to its source document") is unmet.

### 3.8 Client-controlled state and totals
- `payment_status` is accepted verbatim from the request body on create (`app.py:1562`) and
  update (`app.py:1652-1654`); a client can mark an order `Paid` without money moving.
- `total_amount` is trusted from the computed value but order **items cannot be edited at all**
  (`api_order` PUT handles only `payment_status`, `app.py:1648-1690`), while `orders.html` presents
  an "Edit Order" flow — a UI/backend mismatch: editing an order's items does nothing.
- `Check_customer_orders` / `check_product_orders` endpoints exist and are surfaced in the UI, but
  the delete path bypasses the confirmation they enable.

### 3.9 No pagination, no limits
`ITEMS_PER_PAGE` is defined (`config.py`) and **never used**. Every list route calls `.all()`
(`app.py:1335, 1440, 1542-1544, 1627, 1730-1731`). The dashboard loads an entire month of orders
plus every unpaid order and all of its payments into Python to compute four numbers
(`app.py:1282-1293`) — this degrades linearly with business history.

---

## 4. Security review

| # | Finding | Evidence | Severity |
|---|---|---|---|
| S1 | Unauthenticated admin creation. `GET /create-admin` creates `admin`/`admin123` for anyone able to reach the host. | `app.py:2060-2067` | **Critical** |
| S2 | Unauthenticated database creation endpoint. | `app.py:2050-2057` | High |
| S3 | Destructive `GET /recreate-db` calls `db.drop_all()` — wipes all business data, no confirmation, no CSRF, reachable by any logged-in user. | `app.py:2017-2036` | **Critical** |
| S4 | No CSRF protection at all. `Flask-WTF` is installed but never imported as `CSRFProtect`; no template contains a CSRF token (verified: 0 matches for `csrf` across `templates/`). All state changes are cookie-authenticated POST/PUT/DELETE. | `requirements.txt`, `templates/*` | **High** |
| S5 | Default credentials `admin`/`admin123` auto-created on every startup and documented in the README and `start.bat`. | `app.py:1214-1233`, `2085`; `start.bat:24`; `README.md:163` | **High** |
| S6 | Hard-coded fallback session secret `'your-secret-key-change-this-in-production'`. If `SECRET_KEY` is unset, all sessions are forgeable by anyone who reads the public repo. | `config.py:5` | **High** |
| S7 | Session cookie flags unset: `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE`, `SESSION_COOKIE_HTTPONLY` not configured. | `config.py:1-27` | Medium |
| S8 | Flask **debug mode + `host='0.0.0.0'`** in the run block. The Werkzeug debugger PIN is the only barrier to RCE on a network-exposed deployment. | `app.py:2087` | **Critical** if exposed |
| S9 | No rate limiting on `/login` (`app.py:1310-1323`) — unlimited credential guessing; no lockout, no delay, no logging. | `app.py:1310` | Medium |
| S10 | No authorization model. `users` has only a username; every authenticated user can read/modify/delete every customer, product, order and payment. | `app.py:1082-1087` | **High** |
| S11 | No audit logging of any kind (no login events, no deletions, no price/stock changes). | whole file | Medium |
| S12 | Unhandled exceptions leak internals to clients: `return jsonify({'error': str(e)}), 500` in ~20 handlers, and debug mode returns the full traceback. | `app.py:1363, 1403, 1434, 1468, 1510, 1536, 1625, 1690, 1724, 1784, 2036, 2047, 2057, 2067` | Medium |
| S13 | `request.get_json()` result used without a `None` guard → `TypeError` → HTTP 500 instead of 400 for a malformed request body. | `app.py:1342-1345` (and the same pattern in every POST handler) | Low |
| S14 | `request.form['username']` / `['password']` → `KeyError` → 500 when fields are absent. | `app.py:1313-1314` | Low |
| S15 | `Werkzeug 2.3.7` / `Flask 2.3.3` / `cryptography 41.0.7` are multiple minor releases behind; no automated dependency scanning exists. | `requirements.txt` | Medium (see §8) |
| S16 | `f"CREATE DATABASE IF NOT EXISTS \`{database_name}\` …"` interpolates a value taken from configuration into SQL. Not attacker-controlled in practice, but it is string-built DDL. | `app.py:1196` | Low |
| S17 | No file upload exists, so upload risk is currently nil — but `MAX_CONTENT_LENGTH` is set with no consumer. | `config.py:15` | Info |

**Positive findings (verified):** passwords are hashed with `generate_password_hash`
(`app.py:1225`, Werkzeug PBKDF2-SHA256) and verified with `check_password_hash`
(`app.py:1317`). ORM query builders are used throughout, and the one `.ilike(f'%{query}%')` search
(`app.py:1989, 1998, 2007`) binds through SQLAlchemy parameters, so it is **not** SQL-injectable.

---

## 5. Localization and Algerian adaptation review

- **Languages implemented:** `en`, `hi`, `ur` (`app.py:30-34`). Default `en` (`app.py:36`).
- **Arabic: absent. French: absent.** The target languages are not present in the codebase at all.
- **RTL:** implemented as one CSS rule `html[lang="ur"] { direction: rtl; }` plus a hand-written
  list of ~30 selector overrides (`base.html:1122-1170`, comment: *"RTL Support for Urdu - Text
  Only"*). The `<html>` element has **no `dir` attribute** (`base.html:2`), so direction is applied
  by CSS only and LTR-specific layout code (sidebar, flex ordering) is not mirrored — the comment
  "Text Only" is an admission that layout mirroring was never done.
- **Translation mechanism:** two 1,000-line Python dicts inline in `app.py`. Untranslated
  hard-coded strings remain in templates, e.g. the settings modal title `Settings`
  (`base.html:1427`) and footer button `Close` (`base.html:1455`).
- **Currency:** hard-coded US dollars. `$` appears in generated payment notes
  (`app.py:1602, 1605, 1668, 1671`) and in the PDF invoice (`app.py:1940-1945`). No DZD/DA
  support, no locale-aware number formatting.
- **Time zone:** never set; `datetime.utcnow` is used for timestamps (`app.py:1087, 1096, 1108`).
  No `Africa/Algiers` handling; `date.today()` is used for business dates (`app.py:1283`).
- **Phone/address:** `customers.phone` is `VARCHAR(20)` free text (`app.py:1093`); no wilaya,
  commune, locality. No Algerian number validation or formatting.
- **PDF invoice is fabricated and non-Arabic:** the header is hard-coded as
  `"BUILDING MATERIALS SHOP"`, `"123 Construction Street, City, Country"`,
  `"Phone: +1234567890 | Email: info@shop.com"` (`app.py:1916-1918`). Fonts are stock Helvetica
  (`app.py:1911, 1952`), which **cannot render Arabic glyphs**, so no Arabic invoice is possible
  with the current implementation.
- **Algerian regulatory parameters:** none present. No tax configuration, no commercial-register
  (RC) / NIF / NIS fields, no invoice numbering sequence. Nothing to verify against law because
  nothing is implemented.

---

## 6. Documentation accuracy (README vs. source)

| README claim | Verified reality |
|---|---|
| "Data Validation: Comprehensive input validation and sanitization" (`README.md:43`) | Only key-presence checks; no types, ranges, formats, lengths or normalization. |
| "Session Management: Configurable session timeouts" (`README.md:42`) | `PERMANENT_SESSION_LIFETIME` is set (`config.py:10`) but `session.permanent` is never set to `True`, so the value never applies. |
| "Inventory Reports / Customer Reports" (`README.md:23-24`) | Only two report endpoints exist: `/api/reports/sales` and `/api/reports/export-csv`. There is **no** inventory-report or customer-report endpoint in source. |
| "Order Management: Create, **edit**, and track orders" (`README.md:16`) | `PUT /api/orders/<id>` accepts `payment_status` only; items, dates and customer cannot be edited. |
| "Professional PDF invoice generation" (`README.md:18`) | Functional, but contains fabricated business identity and USD symbols; cannot render Arabic. |
| Docker Deployment section (`README.md:249-258`) | Provides a Dockerfile that is **not in the repository** and a `gunicorn` command although `gunicorn` is not in `requirements.txt`. |
| "Create any missing tables" on startup (`README.md:118`) | True (`db.create_all()`), but this is not a migration system and cannot alter existing columns. |
| Multi-language incl. RTL (`README.md:27-31`) | en/hi/ur only; RTL is text-only CSS for Urdu. |

---

## 7. Feature matrix (implemented / partial / missing)

Legend: **I** = implemented and verified in source · **P** = partially implemented · **M** = missing.

| Domain | Capability | State | Evidence |
|---|---|---|---|
| Auth | Login/logout, hashed passwords | I | `app.py:1310-1329`, `1225` |
| Auth | Roles / permissions | M | `app.py:1082-1087` |
| Auth | CSRF, rate limit, secure cookies | M | §4 S4, S7, S9 |
| Catalog | Products with price & stock | I | `app.py:1101-1112` |
| Catalog | Categories/brands/SKU/barcode/cost | M | model has none |
| Catalog | Suppliers | M | no model |
| UOM | Units, conversion rules, decimals | M | `unit` is free text |
| Inventory | Stock on hand | P | integer only, no ledger |
| Inventory | Warehouse/location, transfers, counts, reservations | M | none |
| Sales | Order → items → total | I | `app.py:1547-1625` |
| Sales | Edit order contents | M | `app.py:1648-1690` |
| Sales | Quotations, deliveries, credit notes/returns | M | none |
| Sales | Printable invoice (PDF) | P | USD + fake identity, no Arabic |
| Sales | Cash/credit price lists, discounts | M | none |
| Payments | Customer receipts, status recalc | I | `app.py:1734-1784` |
| Payments | Allocation, AP/supplier payments, aging | M | none |
| Purchasing | PO → receipt → supplier invoice → payment | M | none |
| Weighbridge | Tickets, gross/tare/net, scale adapter | M | none |
| Fleet | Vehicles, drivers, carriers, trips, costs | M | none |
| Reporting | Sales report + CSV export | I | `app.py:1815-1884` |
| Reporting | SQL aggregation / pagination / margins / aging | M | in-Python sums, `.all()` |
| Dashboard | 4 KPI cards + pending deliveries | P | correct but O(all rows) |
| i18n | ar / fr / en, RTL, DZD, Africa/Algiers | M | hi/ur only |
| Ops | Migrations, tests, backups, deploy docs | M | none present |

---

## 8. Dependency risk (Phase 1 note)

`requirements.txt` pins versions that are 2–3 years old relative to the audit date
(Flask 2.3.3, Werkzeug 2.3.7, cryptography 41.0.7, reportlab 4.0.4, PyMySQL 1.1.0). Two concrete
consequences seen during this audit:

1. The pins are **not installable on the interpreter available on this machine (Python 3.14.3)**;
   an in-project `.venv` with a current, still-pinned stack was created instead (see
   `docs/DEPLOYMENT.md`).
2. `gunicorn` is referenced by the README but absent from `requirements.txt`.

No CVE database scan was performed in this session; a version-matched advisory audit remains an
open item recorded in `docs/IMPLEMENTATION_ROADMAP.md` rather than being claimed here.

---

## 9. Ranked findings summary

**Critical**
1. `GET /create-admin` unauthenticated → remote admin account creation (S1).
2. `GET /recreate-db` → `db.drop_all()` from a URL (S3).
3. `app.run(debug=True, host='0.0.0.0')` exposure (S8).
4. Money computed in `float` (§3.1).
5. Customer deletion corrupts stock (§3.4).

**High**
6. No CSRF (S4). 7. Default `admin/admin123` auto-created (S5). 8. Hard-coded fallback secret (S6).
9. No authorization model (S10). 10. Integer-only quantities (§3.2). 11. Oversell race (§3.3).
12. Product deletion destroys order history (§3.5).

**Medium**
13. Fabricated partial payment (§3.6). 14. Unauditable stock (§3.7). 15. No migrations (§2).
16. No pagination / O(history) dashboard (§3.9). 17. No localization for Algerian operation (§5).
18. Unhandled-exception leakage (S12).

**Low / informational**
19. `request.get_json()`/`request.form[]` crash paths (S13, S14). 20. `schema.sql` broken and dead
(§2). 21. Untranslated template strings (§5). 22. Dead `ITEMS_PER_PAGE` config (§3.9).

---

## 10. Audit limitations (honest limits of this document)

- **No runtime execution of the audited revision was possible** on this machine: `requirements.txt`
  cannot be installed on Python 3.14.3 (see §8). All findings above are **static source evidence**.
  No claim is made about runtime behaviour beyond what the code demonstrably does.
- **No MySQL instance was reachable**, so the live schema was not dumped or compared row-by-row;
  the schema analysis is from `schema.sql` and the SQLAlchemy models.
- **No production data was inspected**, and none was modified.
- **No CVE/advisory scan** was completed, so dependency risk is stated qualitatively.
- **Algerian tax/accounting/invoicing rules were not verified against official sources** in this
  session, so none are implemented; regulatory placeholders are labelled as such in
  `docs/ALGERIA_LOCALIZATION.md`.
- The repository has a shallow single-author history (mostly README edits); no evidence of prior
  tests, deployments or issues could be recovered from Git history.

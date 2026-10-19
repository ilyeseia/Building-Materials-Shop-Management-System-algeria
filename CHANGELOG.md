# Changelog

All notable changes to this project are recorded here.  The project was audited
and substantially rewritten in 2026; the entries below describe that work
honestly, including what is **not** finished.

Format based on Keep a Changelog.  Versions are not yet tagged.

## [Unreleased] — rebuilding the application

### Context — what the audited revision was

The pre-existing application (`Building-Materials-Shop-Management-System`) was
reviewed and the findings are in [`docs/PROJECT_AUDIT.md`](docs/PROJECT_AUDIT.md).
In summary it:

* shipped only Urdu/Hindi strings, with no Arabic or French and no RTL support;
* hard-coded USD and used binary `float` arithmetic for money;
* stored quantities as integers, with no unit, no conversion and no weight;
* had no CSRF protection, no RBAC, no rate limiting, no migrations and no tests;
* exposed an unauthenticated `/create-admin` route and a destructive
  `GET /recreate-db` route;
* ran `app.run(debug=True, host='0.0.0.0')`;
* printed a fabricated business identity on PDFs and auto-created a 50 % payment
  on any "Partial" status.

The rebuild keeps the same entry points (`python app.py`, `wsgi:app`) so an
existing deployment is not surprised, but replaces the internals.

### Added

* **Application factory and blueprints** (`app/__init__.py`, `app/blueprints/*`):
  `auth`, `dashboard`, `parties`, `catalog`, `inventory`, `sales`, `purchasing`,
  `finance`, `weighbridge`, `fleet`, `reports`, `settings`, `users`, `audit`.
* **Configuration** (`config.py`) driven entirely by environment variables, with
  development / testing / production classes.  Production refuses to start
  without `SECRET_KEY` and `DATABASE_URL`.
* **Domain model** (`app/models/*`, 49 tables) covering catalog, inventory
  ledger, sales, purchasing, payments, weighbridge, fleet, users/roles, audit,
  numbering and business settings.
* **Service layer** (`app/services/*`): `stock`, `sales`, `purchasing`,
  `payments`, `weighing`, `transport`, `reporting`, `uom`, `numbering`,
  `settings`, `audit`, `pdf`, `seed`, `timeutil`.
* **Trilingual interface** — Arabic (default, RTL), French, English — with a
  parity-checked catalogue of 862 keys per language (`app/translations/*`,
  `app/i18n.py`).  Money/quantity/date formatting is locale-aware.
* **Algerian localisation**: DZD, `Africa/Algiers`, the 58 wilayas, commune and
  street address fields, tolerated local phone formats.
* **Decimal money and explicit units**: every monetary value is `Decimal`;
  every quantity carries a unit; conversions are declared, dated and sourced
  (`docs/INVENTORY_AND_UOM.md`).
* **Inventory ledger** with on-hand / reserved / available balances, append-only
  `StockMovement` rows, adjustments, transfers and counts.
* **Sales cycle**: quotation → order → delivery → invoice → payment, plus direct
  sale and credit notes.
* **Purchasing cycle**: purchase order → goods receipt → supplier invoice →
  payment, plus returns and landed-cost allocation.
* **Weighbridge** module with manual weighing, corrections and an explicit
  device-adapter boundary (no scale protocol is implemented).
* **Fleet / transport** with trips, vehicles, drivers, carriers and separated
  costing (`docs/WEIGHBRIDGE_AND_TRANSPORT.md`).
* **Receivables / payables with ageing buckets** and paid / partial / unpaid
  settlement states.
* **Reporting** via SQL aggregation with pagination.
* **Security**: PBKDF2 password hashing, password policy, CSRF on every form,
  HttpOnly / SameSite / Secure cookies, strict CSP with no CDN, security
  headers, login rate limiting, server-side permission checks on every route,
  append-only audit log, production-safe error pages (`docs/SECURITY.md`).
* **CLI** (`app/cli.py`): `init-db`, `seed`, `create-user`, `reset-password`,
  `backup`, `restore`, `check`, `migrate-legacy`, `ensure-sequences`.
* **Localisation-safe forms**: `BaseForm` translates field and choice labels;
  validator messages are mapped to catalogue keys.
* Support files: `.gitignore`, `.env.example`, `requirements.txt` (modern pins),
  rewritten `start.bat` (no fabricated default credentials).
* **Docker deployment** (`Dockerfile`, `docker-compose.yml`, `.dockerignore`,
  `.env.example`, `scripts/entrypoint.sh`, `scripts/docker-init.sh`,
  `scripts/backup.sh`, `scripts/restore.sh`, `docs/DEPLOYMENT.md`): multi-stage
  build on `python:3.13-slim`, non-root runtime (UID/GID 10001) whose code tree
  is root-owned and read-only, Gunicorn instead of the development server, a
  container `HEALTHCHECK` on `/healthz`, MySQL 8 on an internal-only network
  (the database publishes no port), named volumes for all business data,
  required secrets (`${VAR:?}`), resource limits, `cap_drop: ALL` and
  `no-new-privileges`, and four optional `tsdproxy.*` labels that publish the
  application over a tailnet / Tailscale Funnel with HTTPS.
* **`GET /healthz`**: `200` only when the process *and* the database answer,
  `503` otherwise; used as the Docker health check (`app/__init__.py`,
  `tests/test_health.py`).
* **`tests/test_cli.py`**: creating a user through the CLI, including the
  refused-password path.

### Changed

* Money and quantities are `Numeric`/`Decimal` columns, not `REAL`/`float` and
  not integers.
* Oversell prevention is a conditional SQL `UPDATE ... WHERE quantity -
  reserved >= :qty` with a `rowcount` check, so it is correct under concurrency
  on both MySQL and SQLite.
* Reservations are recorded on the same append-only ledger
  (`reserved_delta`, `quantity = 0`) rather than in a side table.
* The application never recreates the database at start-up and never seeds
  silently on boot.
* The legacy database is upgraded additively by `flask migrate-legacy`
  (`app/legacy.py`): colliding tables are renamed to `<name>__legacy`, the new
  schema is created, rows are copied through the ORM, then the shadow tables are
  dropped.  **The MySQL path has not been exercised on a real MySQL server.**

### Deprecated

* `products.price`, `products.stock_quantity`, `products.unit` and
  `customers.address` are kept as nullable mirror columns
  (`legacy_price`, `legacy_stock_quantity`, `legacy_unit`, `legacy_address`),
  written loosely for backward compatibility and scheduled for removal once
  every consumer is migrated.
* The pre-refactor templates in `./templates/` and `schema.sql` are no longer
  used by the application.  They are retained pending explicit approval to
  delete them.  See `docs/PROJECT_AUDIT.md`.

### Removed

* Unauthenticated `/create-admin` and destructive `GET /recreate-db` routes.
* `debug=True` on `0.0.0.0`; the development server now binds to `127.0.0.1`.
* Fabricated business identity on printed documents; the header now comes from
  `BusinessSettings`, and empty fields stay empty.
* The automatic 50 % payment created on a "Partial" status.
* `Product.track_batches` — there is no batch model, so the flag promised a
  capability the system does not have.  Batch/lot tracking is recorded in the
  roadmap instead (see `docs/IMPLEMENTATION_ROADMAP.md`).
* Circular foreign keys (`Quotation.converted_order_id`,
  `SalesInvoice.return_document_id`, `DeliveryNote.weighing_ticket_id`,
  `GoodsReceipt.weighing_ticket_id`); the link is derivable from the child row.

### Fixed

* `flask create-user` and `flask reset-password` crashed with
  `RuntimeError: Working outside of request context` when a password was
  refused: the policy message was translated with the request-bound locale.
  CLI output is now translated with an explicit language, so the operator sees
  `Password rejected: <reason>` and a non-zero exit code — never a traceback.
* Three password-policy messages had no entry in the catalogues
  (`auth.password_too_weak`, `auth.password_needs_digit`,
  `auth.password_needs_letter`), so the interface showed the raw key instead of
  a sentence.  All three now exist in Arabic, French and English, and
  `tests/test_i18n.py` drives the policy to prove that every key it can return
  is translated.

### Not done yet

* Batch/lot tracking, real scale integration, and accounting export.

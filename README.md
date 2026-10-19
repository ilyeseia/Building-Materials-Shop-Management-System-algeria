# Building Materials Shop Management System — Algeria

A management platform for building-materials traders in Algeria: catalogue, stock ledger,
quotations, orders, deliveries, invoices, payments, purchasing, weighbridge tickets, transport
and reporting — with an Arabic-first (RTL) interface, Algerian VAT and the DZD.

![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)
![Flask](https://img.shields.io/badge/Flask-3.x-green.svg)
![MySQL](https://img.shields.io/badge/MySQL-8.0-orange.svg)
![SQLite](https://img.shields.io/badge/SQLite-development-lightgrey.svg)
![Docker](https://img.shields.io/badge/Docker%20Compose-ready-blue.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

The application is an application-factory Flask project (`wsgi:app`) with a service layer, a
versioned migration chain and a test suite. It is a rebuild of an earlier single-file project:
what was wrong with the original and how it was corrected is documented in
[`docs/PROJECT_AUDIT.md`](docs/PROJECT_AUDIT.md).  The current production
baseline, reliability evidence and remaining risks are in
[`docs/PHASE_A_VERIFIED_AUDIT.md`](docs/PHASE_A_VERIFIED_AUDIT.md); current
test evidence is in [`docs/TEST_REPORT_2026-10-19.md`](docs/TEST_REPORT_2026-10-19.md).

---

## What it manages

| Area | Contents |
|---|---|
| Catalogue | Products, categories, brands, units of measure, warehouses, per-product unit conversions, market reference-price observations |
| Inventory | Append-only stock ledger, adjustments, transfers, counts, reservations, low-stock view |
| Sales | Quotation → order → delivery note → invoice → payment; credit notes; print/PDF documents |
| Purchasing | Purchase order → goods receipt (landed cost) → supplier invoice → payment |
| Weighbridge | Weight tickets (gross/tare/net), attached to receipts and deliveries; manual entry |
| Transport | Vehicles, drivers, carriers, trips, trip expenses and cost per trip |
| Finance | Customer and supplier payments, allocation to invoices, receivables/payables ageing |
| Reporting | Stock, sales, purchases, margins, ageing, weighbridge and transport reports |
| Administration | Users, roles and permissions, business settings, audit log, backups |

Market reference prices are recorded separately from the shop's selling prices.  An observation
carries its source, date, place, unit and verification status.  It **never** overwrites
`products.sale_price` automatically, and a percentage difference is shown only when the
observation and the product use the same unit.  Observations older than
`MARKET_PRICE_STALE_DAYS` (default 30) are flagged as needing an update.

## Algerian localisation

* Arabic is the default interface language (right-to-left), with French and English available;
  no untranslated or mixed strings.
* DZD amounts formatted with the Algerian convention; `Decimal` everywhere, never floats.
* Business time zone `Africa/Algiers`; wilaya and commune data for customer and supplier addresses.
* Algerian mobile and landline validation.
* VAT is a percentage (`19` means 19 %), used by invoices, quotations and purchase documents.
* Every product declares an explicit stock unit, and every conversion between units is declared
  (`m³ → kg`, `t → q`, `sacs → kg`, …) instead of being guessed.

---

## Quick start (Docker — recommended)

```bash
git clone https://github.com/ilyeseia/Building-Materials-Shop-Management-System-algeria.git
cd Building-Materials-Shop-Management-System-algeria

cp .env.example .env
chmod 600 .env
# fill in: SECRET_KEY, MYSQL_ROOT_PASSWORD, MYSQL_PASSWORD (+ BIND_ADDRESS, HOST_PORT if needed)
#   openssl rand -hex 32     -> SECRET_KEY
#   openssl rand -hex 24     -> the two database passwords

docker compose config --quiet          # refuses to start while a required value is empty
docker compose up -d --build           # application + MySQL 8, both health-checked
docker compose exec app /app/scripts/docker-init.sh   # migrations + reference data
docker compose exec app flask create-user --username admin --role admin
```

The interface is then on `http://<BIND_ADDRESS>:<HOST_PORT>` (default `http://localhost:8080`).

**Health**: `GET /healthz` answers `200 {"status":"ok","database":true}` only when the process
*and* the database are both responding, and `503` otherwise. Docker uses it as the container
health check.

**Data**: `db-data` (MySQL), `app-instance` (generated documents) and `app-backups` (dumps) are
named volumes. `docker compose down` keeps them, `docker compose down -v` **deletes the business
data** - never use `-v` on an installation you care about.

**Backups**: `docker compose exec app /app/scripts/backup.sh` takes a consistent dump, validates
the gzip and completion marker, writes checksums/manifest/`latest.json`, and prunes only backups
older than `BACKUP_RETENTION_DAYS`.  Restore drills are non-destructive:

```bash
./scripts/verify-backup.sh                  # latest backup
./scripts/verify-backup.sh <backup-name>    # specific backup
```

The script imports the dump into an isolated `bmshop_verify_*` database, runs `mysqlcheck`,
foreign-key and stock/payment integrity checks, then drops the scratch database.  It never writes
to the live database.  See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#8-backup-and-restore).

Full instructions, configuration reference, HTTPS/reverse-proxy setup, backup/restore drill,
upgrade procedure and troubleshooting are in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

### Publishing on a private network (optional)

`docker-compose.yml` carries four `tsdproxy.*` labels that publish the application through
[tsdproxy](https://github.com/almeidapaulopt/tsdproxy) on the host's tailnet, with HTTPS:

```yaml
labels:
  tsdproxy.enable: "true"
  tsdproxy.name: "Building-Materials-Shop-Management-System"
  tsdproxy.container_port: "8000"   # the port inside the container (Gunicorn)
  tsdproxy.funnel: "true"           # also publish on the public internet
```

`tsdproxy.container_port` is the **container** port (`8000`), not `HOST_PORT`. With
`tsdproxy.funnel` set to `"true"` the site is reachable from the public internet through
`https://<name>.<tailnet>.ts.net` — protected by this application's own sign-in, session
handling and login throttle, but public nevertheless. Set it to `"false"` to keep the service
private to the tailnet, or remove the labels to leave it unpublished.

---

## Running without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip

cp .env.example .env
export $(grep -v '^#' .env | xargs)              # Windows: set the variables in the shell

flask --app wsgi db upgrade                      # or: flask --app wsgi init-db  (first time)
flask --app wsgi create-user --username admin --role admin

python app.py                                    # development server, 127.0.0.1:5000

gunicorn --workers 3 --bind 127.0.0.1:8000 wsgi:app       # production (Linux)
waitress-serve --listen=127.0.0.1:8000 wsgi:app           # production (Windows)
```

Without `DATABASE_URL` the development configuration uses SQLite in `instance/`; the production
configuration **refuses to boot** without `SECRET_KEY` and `DATABASE_URL` instead of falling back
to a default.

---

## Tests

```bash
pytest -q                 # 203 tests
pytest -q tests/test_health.py tests/test_cli.py
```

The suite covers authentication and authorisation, catalogue and units, the stock ledger and
oversell protection, the full sales and purchasing chains, payments and ageing, the weighbridge
and transport modules, reports, security headers and CSRF, the health endpoint, and the legacy
database upgrade path. Evidence, including what is *not* covered, is in
[`docs/TEST_REPORT_2026-10-19.md`](docs/TEST_REPORT_2026-10-19.md).  The older
`docs/TEST_REPORT.md` describes the previous 184-test baseline.

---

## Verified deployments

| Environment | What was verified | Result |
|---|---|---|
| Windows x64, Python 3.14.3 | `pytest -q` (whole suite) at revision `7211b7a` | **203 passed** |
| Ubuntu 24.04.3, Docker 29.2.0, Compose v5.0.2 | `docker compose config --quiet`, `docker compose build` | ok |
| MySQL 8.0.46 container | `flask db upgrade` + `flask init-db`, **51 tables**, 179 foreign keys | ok |
| Disposable MySQL | Full migration from empty schema through `a1b2c3d4e5f6`, catalog pages 200, market POST 302, selling price unchanged | ok |
| Disposable MySQL | Two-process payment allocation: one success, one `payment.exceeds_balance` refusal | ok |
| Running stack | `GET /healthz` (200 + `database:true`), `GET /login` (200), `GET /` (302 → sign-in) | ok |
| Running stack | Sign-in end-to-end through the real form (CSRF token, session, redirect), then 17 module pages (prior revision) | **ok, all 200** |
| Running stack | Data survives `restart`, image rebuild and container recreation | ok |
| Running stack | Backup + isolated verification + disposable destructive restore drill | ok |
| Production | Revision `7211b7a` deployed non-destructively; revision `a1b2c3d4e5f6`; existing rows preserved | ok |
| Built image | Production refuses to boot without `SECRET_KEY`/`DATABASE_URL`; no secret in the image | ok |
| tsdproxy (tailnet + Funnel) | HTTPS publication, `GET /healthz` = 200 from the tailnet and from the public internet | ok |

Additions and corrections since the initial rebuild are recorded in
[`CHANGELOG.md`](CHANGELOG.md).

---

## Project structure

```
app/
  __init__.py          application factory, request hooks, security headers, /healthz
  models/              SQLAlchemy models (catalogue, stock, sales, purchasing, finance, …)
  services/            business rules (stock ledger, money, UOM conversion, numbering, reports)
  blueprints/          HTTP layer, one module per functional area, authorisation per route
  templates/           Jinja templates (RTL-aware, print layouts, no CDN assets)
  translations/        Arabic, French and English catalogues
  static/              application CSS and JavaScript
migrations/            Alembic chain (versioned, reversible, preserves user data)
scripts/               entrypoint.sh, docker-init.sh, backup.sh, restore.sh
tests/                 pytest suite and fixtures
docs/                  audit, deployment guide, test report
config.py              environment-driven configuration (development/testing/production)
wsgi.py                WSGI entry point (`wsgi:app`)     app.py: development entry point
docker-compose.yml     application + MySQL, health checks, volumes, network, tsdproxy labels
Dockerfile             multi-stage build, non-root runtime, Gunicorn
```

---

## Security

Server-side authorisation on every route, CSRF protection on all state-changing requests,
login throttling, secure cookie options, security headers (CSP without any CDN, `X-Frame-Options`,
`Referrer-Policy`, HSTS when the request is secure), an audit log for sign-in and data changes,
no committed secrets (`.env` is git-ignored and never copied into the image), production errors
that never leak tracebacks, non-root containers with dropped capabilities, and a database that
publishes no port. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#11-verification-performed) for the
checks that were actually executed.

---

## Known limitations

* Weighing-device integration is manual: the adapter boundary exists, but no serial/TCP scale
  driver is implemented.
* Single-host Docker deployment; no Kubernetes manifests and no managed-database variant.
* The Compose file pins `mysql:8.0`; MySQL 8.4+, MariaDB and PostgreSQL are not tested.
* `scripts/restore.sh` restores from a logical dump — it is not point-in-time recovery.
* The MySQL client tools inside the image are MariaDB's and connect with TLS disabled inside the
  private Docker network (the application driver connects the same way).

---

## License and credits

MIT — see [`LICENSE`](LICENSE). The original application this project was rebuilt from is by
[sam-eer31](https://github.com/sam-eer31); the Algerian platform, the data model, the stock
ledger, the security work, the Docker deployment and the test suite are this project's.

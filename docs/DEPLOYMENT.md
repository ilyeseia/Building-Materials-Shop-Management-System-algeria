# Deployment guide

How to run the Building Materials Shop Management System in production, on
Docker Compose (recommended) or natively.  Everything below describes the
current code base: a Flask application factory (`wsgi:app`) served by Gunicorn,
with MySQL 8 as the production database.

> The values in "Verified" tables come from an actual deployment that was
> executed and observed, not from the design on paper.  Anything not verified is
> marked as such.

---

## 1. What is deployed

```
                     host port (BIND_ADDRESS:HOST_PORT, default 127.0.0.1:8080)
                                        |
                                        v
  +---------------------------------------------------------------+     private
  |  app container (non-root, no capabilities, read-only code)     |     docker
  |    gunicorn wsgi:app  ->  Flask (blueprints + services)        |     network
  |    /app/instance  <- volume "app-instance"                     |
  |    /app/backups   <- volume "app-backups"                      |
  +----------------------------+----------------------------------+
                               | mysql+pymysql://...@db:3306/...
                               v
  +---------------------------------------------------------------+
  |  db container (MySQL 8, no published port)                     |
  |    /var/lib/mysql <- volume "db-data"                          |
  +---------------------------------------------------------------+
```

* The database is reachable **only** from the application container.
* Three named volumes hold all state: `db-data`, `app-instance`, `app-backups`.
* Nothing in the image contains a secret: configuration arrives at run time
  through the environment.

---

## 2. Prerequisites

| Requirement | Notes |
|---|---|
| Docker Engine 24+ with Compose v2+ | Verified on Docker 29.2.0 / Compose v5.0.2 (Ubuntu 24.04). |
| 1 CPU and ~1 GB RAM for the stack | Compose sets these limits; the app itself uses far less. |
| One free host port | `HOST_PORT`, default `8080`. Choose another if it is taken. |
| A copy of the repository | `git clone https://github.com/ilyeseia/Building-Materials-Shop-Management-System-algeria.git` |

For a native installation (no Docker) you need Python 3.12+ and a MySQL 8 server
instead; see section 10.

---

## 3. Environment variables

All configuration lives in `.env` (copy of `.env.example`, git-ignored).

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `production` | Selects the configuration class. `production` refuses to start without `SECRET_KEY` and `DATABASE_URL`. |
| `APP_NAME` | `Gestion de matériaux de construction` | Shown in the interface and on printed documents. |
| `BIND_ADDRESS` | `127.0.0.1` | Host interface the app port is published on. `127.0.0.1` = this machine only, `0.0.0.0` = LAN/VPN, or a specific interface address. |
| `HOST_PORT` | `8080` | Host port for the application. |
| `SECRET_KEY` | *(required)* | Session signing key. `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SESSION_COOKIE_SECURE` | `0` | Set to `1` **only** when the site is served over HTTPS. |
| `SESSION_LIFETIME_MINUTES` | `480` | Idle session lifetime. |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_SECONDS` | `5` / `300` | Brute-force throttling per user+address. |
| `MYSQL_ROOT_PASSWORD` | *(required)* | Database root password (health checks, maintenance). |
| `MYSQL_DATABASE` | `building_materials` | Database name. |
| `MYSQL_USER` / `MYSQL_PASSWORD` | `building_materials` / *(required)* | Least-privilege account used by the application. |
| `DEFAULT_LANGUAGE` | `ar` | Interface language: `ar` (default, RTL), `fr`, `en`. |
| `TIMEZONE` | `Africa/Algiers` | Business time zone for dates, numbering and reports. |
| `CURRENCY` / `CURRENCY_SYMBOL` | `DZD` / `DA` | Display currency. |
| `DEFAULT_VAT_RATE` | `19` | Percentage, not a fraction: `19` = 19 %. |
| `FEATURE_WEIGHBRIDGE` / `FEATURE_FLEET` / `FEATURE_ACCOUNTING` | `1` / `1` / `0` | Module switches. |
| `GUNICORN_WORKERS` | `3` | Worker processes. `2 x CPU + 1` is a good starting point. |
| `GUNICORN_LOG_LEVEL` | `info` | Gunicorn log level. |
| `FORWARDED_ALLOW_IPS` | *(unset → `127.0.0.1`)* | Proxy addresses trusted for `X-Forwarded-*`. Set to `*` only when the app port is reachable exclusively through the proxy. |
| `SEED_DEMO_DATA` | `0` | `1` also inserts the demo catalogue during first-run initialisation. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / *(unset)* | Optional automated administrator creation; leaving the password unset is safer. |
| `BACKUP_RETENTION_DAYS` | `30` | How long `scripts/backup.sh` keeps old dumps. |

Generate secrets with hexadecimal output; they are embedded in the MySQL
connection URL, where characters such as `@`, `:` or `/` would need escaping:

```bash
openssl rand -hex 24        # MYSQL_ROOT_PASSWORD, MYSQL_PASSWORD
openssl rand -hex 32        # SECRET_KEY
```

Docker Compose refuses to start (with a clear message) while a required value is
missing or empty.

---

## 4. First deployment (Docker)

```bash
# 1. Get the code
git clone https://github.com/ilyeseia/Building-Materials-Shop-Management-System-algeria.git
cd Building-Materials-Shop-Management-System-algeria

# 2. Create and edit the environment file
cp .env.example .env
chmod 600 .env
$EDITOR .env                # SECRET_KEY, MYSQL_ROOT_PASSWORD, MYSQL_PASSWORD,
                            # BIND_ADDRESS, HOST_PORT

# 3. Validate the composition before building anything
docker compose config --quiet && echo "compose ok"

# 4. Build and start (the database health check is waited for automatically)
docker compose up -d --build

# 5. Check the state - both services must report "healthy"
docker compose ps

# 6. Create the schema, seed reference data, apply migrations (idempotent)
docker compose exec app /app/scripts/docker-init.sh

# 7. Create the administrator (interactive password prompt)
docker compose exec app flask create-user --username admin --role admin
```

Access the interface at `http://<BIND_ADDRESS>:<HOST_PORT>` (for example
`http://localhost:8080`, or `http://<server-ip>:8090` when the port was
changed).  `/healthz` answers `{"status": "ok", "database": true}` with HTTP 200
when the process and the database are both responding, and HTTP 503 otherwise.

### What first-run initialisation does and does not do

* **Does**: wait for the database; `flask db upgrade` (versioned migrations);
  `flask init-db` (create missing tables, seed units, categories, warehouse);
  optionally create the administrator.
* **Does not**: drop, truncate or recreate anything; overwrite an existing
  administrator; insert demo data unless `SEED_DEMO_DATA=1`; touch the database
  on ordinary container restarts (initialisation is an explicit operator step).

---

## 5. HTTPS and reverse proxies

The container speaks plain HTTP on its internal port; terminate TLS in front of
it.  Two things must match the proxy:

1. `SESSION_COOKIE_SECURE=1` in `.env` (the session cookie is then only sent
   over HTTPS).
2. `FORWARDED_ALLOW_IPS` must contain the proxy's address, otherwise Gunicorn
   ignores `X-Forwarded-Proto` and the application cannot tell the request was
   secure (secure cookies and HSTS would not be applied).  When the application
   port is reachable **only** through the proxy, `FORWARDED_ALLOW_IPS=*` is
   acceptable.

Minimal nginx server block:

```nginx
server {
    listen 443 ssl;
    server_name shop.example.dz;

    ssl_certificate     /etc/ssl/shop/fullchain.pem;
    ssl_certificate_key /etc/ssl/shop/privkey.pem;

    client_max_body_size 10m;          # >= MAX_CONTENT_LENGTH (8 MB)

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

The application already sends `X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy`, `Permissions-Policy`, a strict `Content-Security-Policy`
(no CDN assets) and, on secure requests, HSTS.

In this repository's reference deployment the existing
`nginx-proxy-manager` on the host is used for this purpose.

### Publishing through tsdproxy (tailnet / Funnel)

`docker-compose.yml` labels the application so that a
[tsdproxy](https://github.com/almeidapaulopt/tsdproxy) instance already running
on the host publishes it with HTTPS:

| Label | Value | Meaning |
|---|---|---|
| `tsdproxy.enable` | `"true"` | tsdproxy manages this container |
| `tsdproxy.name` | `Building-Materials-Shop-Management-System` | proxy/node name it creates |
| `tsdproxy.container_port` | `"8000"` | the port **inside** the container (Gunicorn) — not `HOST_PORT` |
| `tsdproxy.funnel` | `"true"` | also publish on the public internet (Tailscale Funnel) |

In the reference deployment the application is then reachable at
`https://building-materials-shop-management-system.<tailnet>.ts.net`, both from
the tailnet and — because Funnel is enabled — from the public internet; that was
verified with `GET /healthz` from a machine outside the tailnet.

`tsdproxy.container_port` is the container port. If it is left wrong or absent,
tsdproxy falls back to auto-detection (it reaches the published host port
through the Docker gateway and logs
`Successfully connected using docker network gateway and exposed port`); that
happened here too, and it works — but the label is the documented path.

**Funnel is a public exposure.** An application published this way is visible to
the whole internet, protected only by its own sign-in, session handling and
login throttling:

* set `tsdproxy.funnel: "false"` to keep it private to the tailnet, or delete the
  four labels to leave the service unpublished;
* keep a strong administrator password, and on a public deployment set
  `SESSION_COOKIE_SECURE=1` and make sure `FORWARDED_ALLOW_IPS` covers the proxy
  so that secure cookies and HSTS behave correctly;
* Funnel requires TLS on ports 443/8443/10000 — tsdproxy/tsnet obtains the
  certificate — and the tailnet policy must allow Funnel for the node.

---

## 6. Persistent data

| Volume | Container path | Contents | Lost if deleted |
|---|---|---|---|
| `db-data` | `/var/lib/mysql` | All business records | everything |
| `app-instance` | `/app/instance` | Generated documents, runtime data | documents |
| `app-backups` | `/app/backups` | Backups written by `scripts/backup.sh` | backups |

```bash
docker volume ls | grep building-materials
```

> **`docker compose down`** removes the containers but keeps the volumes.
> **`docker compose down -v`** also deletes the volumes and therefore the
> business data. Never use `-v` on a live installation.

---

## 7. Routine operations

| Goal | Command | Data safe? |
|---|---|---|
| Status and health | `docker compose ps` | yes |
| Follow the logs | `docker compose logs -f app` | yes |
| Stop / start | `docker compose stop` / `docker compose start` | yes |
| Restart | `docker compose restart` | yes |
| Rebuild after a code change | `docker compose up -d --build` | yes |
| Update from Git | `git pull && docker compose up -d --build` then `docker compose exec app flask db upgrade` | yes |
| Health check by hand | `curl -fsS http://127.0.0.1:8080/healthz` | yes |
| Backup | `docker compose exec app /app/scripts/backup.sh` | yes |
| Restore | see section 8 | **destructive** |
| Remove containers **and data** | `docker compose down -v` | **destroys data** |

---

## 8. Backup and restore

```bash
# Backup (safe while the shop is open: single-transaction dump)
docker compose exec app /app/scripts/backup.sh
docker compose exec app ls -lh /app/backups

# Restore (destructive - the script requires typing YES then RESTORE)
docker compose stop app
docker compose run --rm --no-deps app /app/scripts/restore.sh <backup-name>
docker compose up -d app
```

Each backup produces a gzipped MySQL dump, a tarball of `/app/instance`, a
`.sha256` file (verified automatically before a restore) and a manifest that
records what was taken and when.

Copy backups off the machine regularly - a backup that only exists on the server
it protects is not a backup:

```bash
docker compose exec app tar -czf - -C /app/backups . > backups-$(date +%F).tar.gz
```

---

## 9. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `docker compose up` refuses with "SECRET_KEY must be set" | empty required variable | fill `.env` (section 3) |
| `port is already allocated` | `HOST_PORT` in use | pick another port in `.env`, `docker compose up -d` |
| App restarts in a loop, logs show `Access denied for user` | wrong `MYSQL_PASSWORD`, or credentials changed after the volume was created | restore consistency: either fix `.env` or recreate the database volume **after backing up** |
| `db` never reports healthy | slow disk, or MySQL ran out of memory | `docker compose logs db`; check free disk and RAM |
| Login "succeeds" but the user is not signed in | `SESSION_COOKIE_SECURE=1` while serving over plain HTTP | set it to `0` (or switch to HTTPS) |
| `/healthz` returns 503 | database unreachable | `docker compose logs db app`; verify the `DATABASE_URL` host is `db` |
| `flask db upgrade` fails | migration error, or the database was created outside migrations | back up first, then read the error; `flask init-db` alone can create the schema on an empty database |
| Permission denied writing to `/app/instance` | volume created by another UID | `docker compose run --rm --user root app chown -R 10001:10001 /app/instance` |
| Packages missing after a build | dependency install failed (network) | re-run `docker compose build --no-cache app` and read the output | 
| Everything is slow after a reboot | database still warming up | wait for both services to report `healthy` in `docker compose ps` |

Logs always go to standard output/error, so `docker compose logs` is the first
place to look.  Application errors are logged with a short reference; full
tracebacks never reach the browser in `production`.

---

## 10. Native installation (no Docker)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env            # SECRET_KEY, DATABASE_URL (mysql+pymysql://...)
export $(grep -v '^#' .env | xargs)

flask --app wsgi db upgrade
flask --app wsgi init-db
flask --app wsgi create-user --username admin --role admin

# development server: binds 127.0.0.1:5000, never debug on a public interface
python app.py

# production server
gunicorn --workers 3 --bind 127.0.0.1:8000 --forwarded-allow-ips 127.0.0.1 wsgi:app
# or, on Windows without Gunicorn:
waitress-serve --listen=127.0.0.1:8000 wsgi:app
```

Without Docker, `DATABASE_URL` and `SECRET_KEY` must still be present in
production, and backups become `flask backup` (portable JSON) or `mysqldump`
for MySQL.

---

## 11. Verification performed

Last verified deployment: Ubuntu 24.04.3 (12 cores, 15 GB), Docker 29.2.0,
Docker Compose v5.0.2, `mysql:8.0`, application published on host port 8090
(bound to `0.0.0.0`), database on the internal network only.  Every row below
was executed and its output observed on that machine; nothing is inferred from
the configuration on paper.

| Check | Result |
|---|---|
| `docker compose config --quiet` | ok |
| `docker compose build` | ok (three builds: initial, CLI fix, image hardening) |
| `docker compose up -d` — both containers report `healthy` | ok |
| `GET /healthz` | `200 {"database":true,"status":"ok"}` |
| `GET /login` / `GET /` | `200` / `302` to the sign-in page |
| `flask db upgrade` on MySQL 8 | applied `0001_initial_algerian_schema` |
| `flask init-db` (create + seed, never drops) | ok — `{'units': 13, 'categories': 7, 'warehouses': 1, 'brands': 0}`; a second run creates nothing |
| Schema size | 49 application tables + `alembic_version` = 50 objects in `information_schema` |
| Sign-in end to end through the real form: CSRF token from the page, wrong password → `401`, correct password → `302`, then 17 module pages | ok — all 17 pages return `200` |
| Application suite, `pytest -q` (Ubuntu 24.04.3, Python 3.12.3) | **194 passed in ~55 s** |
| Data survives `docker compose restart app` | ok (user and product counts unchanged) |
| Data survives an image rebuild + container recreation | ok |
| Data survives `docker compose down` + `up -d` (volumes kept) | ok |
| Backup + destructive restore drill | ok — checksums verified; a marker row planted *after* the dump was gone afterwards, a marker row present in the dump survived; 50 tables before and after; container healthy again, `/healthz` and `/login` back to 200 |
| Production refuses to boot without `SECRET_KEY`/`DATABASE_URL` | ok — exit code 1 with an explicit message; with both present it boots (`environment = production`) |
| No `.env` and no secret value inside the image or its build history | ok |
| Ports: only the application port published; nothing listens on host `3306` | ok |
| Security headers on `/login` | CSP without CDN, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy` |
| Container runs unprivileged | `uid=10001(appuser)`, `CapEff=0000000000000000`, `NoNewPrivs=1` |
| Only `/app/instance` and `/app/backups` are writable; application code is not | ok |
| tsdproxy publication — tailnet URL | `200` for `/healthz` and `/login` |
| tsdproxy publication — Tailscale Funnel (public internet) | `200 {"database":true,"status":"ok"}` fetched from a host outside the tailnet |

Not verified, and therefore not claimed: a real weighing scale, PostgreSQL,
MySQL 8.4+/MariaDB, point-in-time recovery, multi-host deployment, and browser
testing on real mobile devices.

Two observations worth knowing:

* When the application container is recreated, tsdproxy re-detects it and the
  published URL answers `502` for about a minute in between.  That is a property
  of the publication layer, not of the application.
* From the deployment host *itself*, `curl` to the Funnel URL may time out
  (its own egress towards Tailscale's ingress); public reachability was
  therefore checked from an external network.

---

## 12. Known limitations

* Single-host deployment only; no Kubernetes manifests or managed-database
  variant is provided.
* The Compose file pins `mysql:8.0`. MySQL 8.4+ and MariaDB are not tested.
* Backups are written to a Docker volume, not copied off-site automatically.
* `scripts/restore.sh` replaces the contents of the database from a dump; it is
  not a point-in-time (binary log) recovery mechanism.
* The weighing-device integration is manual only: the adapter boundary exists,
  but no serial/TCP scale driver is implemented.
